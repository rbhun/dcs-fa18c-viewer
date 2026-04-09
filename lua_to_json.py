#!/usr/bin/env python3
"""
Convert DCS-BIOS Lua module definitions to the JSON format used by cockpit_gui.

Source repository: https://github.com/DCS-Skunkworks/dcs-bios

The DCS-BIOS Skunkworks project publishes Lua source, not pre-built JSON.
This module parses the Lua ``define*`` calls, simulates the memory allocator,
and emits the ``{ category: { identifier: control_def } }`` JSON structure
expected by the cockpit GUI.

Usage as CLI::

    python lua_to_json.py  path/to/FA-18C_hornet.lua  [output.json]

Import as library::

    from lua_to_json import lua_to_json
    json_dict = lua_to_json(lua_text)
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Memory allocator simulation
# ---------------------------------------------------------------------------

class _MemoryMap:
    """Simulates the DCS-BIOS MemoryMap/MemoryMapEntry/StringAllocation logic.

    * Integers scan from *base_address* for the first word with enough free bits.
    * String characters append at *last_address*; the first character of each
      string must start in an empty word (bit 0).
    """

    def __init__(self, base: int) -> None:
        self._base = base
        self._last = base
        # addr -> allocated_bits  (0..16)
        self._entries: dict[int, int] = {base: 0}

    def _ensure(self, addr: int) -> int:
        if addr not in self._entries:
            self._entries[addr] = 0
            if addr > self._last:
                self._last = addr
        return self._entries[addr]

    @staticmethod
    def _bits_for(max_val: int) -> int:
        if max_val <= 0:
            return 1
        b = math.ceil(math.log2(max_val + 1))
        return min(b, 16)

    def alloc_int(self, max_val: int) -> dict:
        bits = self._bits_for(max_val)
        addr = self._base
        while True:
            used = self._ensure(addr)
            if 16 - used >= bits:
                break
            addr += 2

        shift = self._entries[addr]
        mask = ((1 << bits) - 1) << shift
        self._entries[addr] += bits
        if addr > self._last:
            self._last = addr
        return {
            "address": addr,
            "mask": mask,
            "shift_by": shift,
            "max_value": min(max_val, (1 << bits) - 1),
        }

    def alloc_string(self, max_length: int) -> dict:
        chars: list[dict] = []
        for i in range(max_length):
            first = i == 0
            addr = self._last
            while True:
                used = self._ensure(addr)
                ok_bits = 16 - used >= 8
                if first:
                    ok_str = used == 0
                else:
                    ok_str = used == 0 or used == 8
                if ok_bits and ok_str:
                    break
                addr += 2
            shift = self._entries[addr]
            self._entries[addr] += 8
            if addr > self._last:
                self._last = addr
            if i == 0:
                chars.append({"address": addr, "shift": shift})
            else:
                chars.append({"address": addr, "shift": shift})

        start_addr = chars[0]["address"] if chars else self._last
        return {"address": start_addr, "max_length": max_length}


# ---------------------------------------------------------------------------
# Lua argument splitter
# ---------------------------------------------------------------------------

def _split_args(s: str) -> list[str]:
    """Split a Lua argument list on top-level commas.

    Handles nested ``{}``, ``()``, ``function … end`` blocks, ``"strings"``,
    and ``--`` line comments.
    """
    args: list[str] = []
    buf: list[str] = []
    brace = paren = func = 0
    in_str = False
    esc = False
    i = 0
    n = len(s)

    while i < n:
        ch = s[i]

        if esc:
            buf.append(ch); esc = False; i += 1; continue

        if in_str:
            buf.append(ch)
            if ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            i += 1; continue

        # line comment
        if ch == '-' and i + 1 < n and s[i + 1] == '-':
            while i < n and s[i] != '\n':
                i += 1
            continue

        if ch == '"':
            in_str = True; buf.append(ch)
        elif ch == '{':
            brace += 1; buf.append(ch)
        elif ch == '}':
            brace -= 1; buf.append(ch)
        elif ch == '(':
            paren += 1; buf.append(ch)
        elif ch == ')':
            paren -= 1; buf.append(ch)
        else:
            # check keywords at word boundary
            def _at_word(pos: int, kw: str) -> bool:
                end = pos + len(kw)
                if s[pos:end] != kw:
                    return False
                if pos > 0 and (s[pos - 1].isalnum() or s[pos - 1] == '_'):
                    return False
                if end < n and (s[end].isalnum() or s[end] == '_'):
                    return False
                return True

            if _at_word(i, 'function'):
                func += 1; buf.append('function'); i += 8; continue
            if _at_word(i, 'end') and func > 0:
                func -= 1; buf.append('end'); i += 3; continue

            if ch == ',' and brace == 0 and paren == 0 and func == 0:
                args.append(''.join(buf).strip()); buf = []
                i += 1; continue
            buf.append(ch)

        i += 1

    tail = ''.join(buf).strip()
    if tail:
        args.append(tail)
    return args


def _parse_str(tok: str) -> str:
    tok = tok.strip()
    if tok.startswith('"') and tok.endswith('"'):
        return tok[1:-1]
    return tok


def _parse_num(tok: str) -> float:
    tok = tok.strip()
    if tok.startswith('0x') or tok.startswith('0X'):
        return float(int(tok, 16))
    return float(tok)


def _parse_range(tok: str) -> tuple[float, float]:
    """Parse ``{ -1, 1 }`` → (-1.0, 1.0)."""
    m = re.search(r'([-\d.eE+]+)\s*,\s*([-\d.eE+]+)', tok.strip())
    if not m:
        return 0.0, 1.0
    return float(m.group(1)), float(m.group(2))


# ---------------------------------------------------------------------------
# Per-type handlers
# ---------------------------------------------------------------------------

def _int_output(alloc: dict, desc: str = "") -> dict:
    return {
        "address": alloc["address"],
        "description": desc,
        "mask": alloc["mask"],
        "max_value": alloc["max_value"],
        "shift_by": alloc["shift_by"],
        "suffix": "",
        "type": "integer",
    }


def _str_output(alloc: dict, desc: str = "") -> dict:
    return {
        "address": alloc["address"],
        "description": desc,
        "max_length": alloc["max_length"],
        "suffix": "",
        "type": "string",
    }


def _inputs_toggle(max_val: int = 1) -> list[dict]:
    return [
        {"description": "switch to previous or next state", "interface": "fixed_step"},
        {"description": "set position", "interface": "set_state", "max_value": max_val},
        {"argument": "TOGGLE", "description": "Toggle switch state", "interface": "action"},
    ]


def _inputs_set(max_val: int) -> list[dict]:
    return [
        {"description": "switch to previous or next state", "interface": "fixed_step"},
        {"description": "set position", "interface": "set_state", "max_value": max_val},
    ]


def _inputs_pot() -> list[dict]:
    return [
        {"description": "turn the knob", "interface": "variable_step"},
        {"description": "set value", "interface": "set_state", "max_value": 65535},
    ]


# Type → (allocation lambda, control builder lambda)
# The allocation lambda receives (mem, args) and returns alloc dict.
# The control builder receives (identifier, args, alloc) and returns partial control dict.

_HANDLERS: dict[str, Any] = {}


def _register(name: str):
    def decorator(fn):
        _HANDLERS[name] = fn
        return fn
    return decorator


@_register("definePushButton")
def _h_push(mem: _MemoryMap, args: list[str]):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(1)
    return ident, cat, {
        "category": cat, "control_type": "selector", "description": desc,
        "identifier": ident,
        "inputs": _inputs_toggle(1),
        "outputs": [_int_output(alloc, "selector position")],
        "physical_variant": "push_button",
        "momentary_positions": "last",
    }


@_register("defineToggleSwitch")
def _h_toggle(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(1)
    return ident, cat, {
        "category": cat, "control_type": "selector", "description": desc,
        "identifier": ident,
        "inputs": _inputs_toggle(1),
        "outputs": [_int_output(alloc, "selector position")],
        "physical_variant": "toggle_switch",
    }


@_register("define3PosTumb")
def _h_3pos(mem, args):
    ident = _parse_str(args[0])
    cat, desc = _parse_str(args[4]), _parse_str(args[5])
    alloc = mem.alloc_int(2)
    return ident, cat, {
        "category": cat, "control_type": "selector", "description": desc,
        "identifier": ident,
        "inputs": _inputs_set(2),
        "outputs": [_int_output(alloc, "selector position")],
        "physical_variant": "toggle_switch",
    }


@_register("definePotentiometer")
def _h_pot(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(65535)
    return ident, cat, {
        "category": cat, "control_type": "analog", "description": desc,
        "identifier": ident,
        "inputs": _inputs_pot(),
        "outputs": [_int_output(alloc, "gauge position")],
    }


@_register("defineFloat")
def _h_float(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(65535)
    return ident, cat, {
        "category": cat, "control_type": "analog", "description": desc,
        "identifier": ident,
        "inputs": [],
        "outputs": [_int_output(alloc, desc)],
    }


@_register("defineRotary")
def _h_rotary(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(65535)
    return ident, cat, {
        "category": cat, "control_type": "analog", "description": desc,
        "identifier": ident,
        "inputs": [{"description": "turn", "interface": "variable_step"}],
        "outputs": [_int_output(alloc, desc)],
    }


@_register("defineString")
def _h_string(mem, args):
    ident = _parse_str(args[0])
    # args[1] is the getter function; args[2] is max_length
    max_len = int(_parse_num(args[2]))
    cat = _parse_str(args[3])
    desc = _parse_str(args[4])
    alloc = mem.alloc_string(max_len)
    return ident, cat, {
        "category": cat, "control_type": "display", "description": desc,
        "identifier": ident,
        "inputs": [],
        "outputs": [_str_output(alloc, desc)],
    }


@_register("defineIndicatorLight")
def _h_led(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(1)
    return ident, cat, {
        "category": cat, "control_type": "led", "description": desc,
        "identifier": ident,
        "inputs": [],
        "outputs": [_int_output(alloc, "0 if light is off, 1 if light is on")],
    }


@_register("defineIndicatorLightInverted")
def _h_led_inv(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(1)
    return ident, cat, {
        "category": cat, "control_type": "led", "description": desc,
        "identifier": ident,
        "inputs": [],
        "outputs": [_int_output(alloc, "1 if light is off, 0 if light is on")],
    }


@_register("defineTumb")
def _h_tumb(mem, args):
    ident = _parse_str(args[0])
    step = _parse_num(args[4])
    lo, hi = _parse_range(args[5])
    num_pos = round((hi - lo) / step) + 1
    max_val = max(num_pos - 1, 1)
    cat = _parse_str(args[8])
    desc = _parse_str(args[9])
    alloc = mem.alloc_int(max_val)
    return ident, cat, {
        "category": cat, "control_type": "selector", "description": desc,
        "identifier": ident,
        "inputs": _inputs_set(max_val),
        "outputs": [_int_output(alloc, "selector position")],
    }


@_register("defineRockerSwitch")
def _h_rocker(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[7])
    desc = _parse_str(args[8])
    alloc = mem.alloc_int(2)
    return ident, cat, {
        "category": cat, "control_type": "selector", "description": desc,
        "identifier": ident,
        "inputs": _inputs_set(2),
        "outputs": [_int_output(alloc, "selector position")],
    }


@_register("defineIntegerFromGetter")
def _h_int_getter(mem, args):
    ident = _parse_str(args[0])
    # args[1] is getter function
    max_val = int(_parse_num(args[2]))
    cat = _parse_str(args[3])
    desc = _parse_str(args[4])
    alloc = mem.alloc_int(max_val)
    return ident, cat, {
        "category": cat, "control_type": "metadata", "description": desc,
        "identifier": ident,
        "inputs": [],
        "outputs": [_int_output(alloc, desc)],
    }


@_register("defineFixedStepInput")
def _h_fixstep(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    return ident, cat, {
        "category": cat, "control_type": "selector", "description": desc,
        "identifier": ident,
        "inputs": [{"description": desc, "interface": "fixed_step"}],
        "outputs": [],
    }


@_register("defineFloatFromDrawArgument")
def _h_float_draw(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(65535)
    return ident, cat, {
        "category": cat, "control_type": "metadata", "description": desc,
        "identifier": ident,
        "inputs": [],
        "outputs": [_int_output(alloc, desc)],
    }


@_register("defineBitFromDrawArgument")
def _h_bit_draw(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(1)
    return ident, cat, {
        "category": cat, "control_type": "led", "description": desc,
        "identifier": ident,
        "inputs": [],
        "outputs": [_int_output(alloc, desc)],
    }


@_register("defineEjectionHandleSwitch")
def _h_eject(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(1)
    return ident, cat, {
        "category": cat, "control_type": "selector", "description": desc,
        "identifier": ident,
        "inputs": _inputs_toggle(1),
        "outputs": [_int_output(alloc, "selector position")],
    }


@_register("defineEmergencyParkingBrake")
def _h_epb(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(2)
    return ident, cat, {
        "category": cat, "control_type": "selector", "description": desc,
        "identifier": ident,
        "inputs": _inputs_set(2),
        "outputs": [_int_output(alloc, "switch position")],
    }


@_register("defineMissionComputerSwitch")
def _h_mc(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[-2])
    desc = _parse_str(args[-1])
    alloc = mem.alloc_int(2)
    return ident, cat, {
        "category": cat, "control_type": "selector", "description": desc,
        "identifier": ident,
        "inputs": _inputs_set(2),
        "outputs": [_int_output(alloc, "switch position")],
    }


@_register("defineElectricallyHeldSwitch")
def _h_ehsw(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[4])
    desc = _parse_str(args[5])
    alloc = mem.alloc_int(1)
    return ident, cat, {
        "category": cat, "control_type": "selector", "description": desc,
        "identifier": ident,
        "inputs": _inputs_toggle(1),
        "outputs": [_int_output(alloc, "selector position")],
        "physical_variant": "toggle_switch",
    }


@_register("defineElectricallyHeld3PosTumb")
def _h_eh3p(mem, args):
    ident = _parse_str(args[0])
    cat = _parse_str(args[5])
    desc = _parse_str(args[6])
    alloc = mem.alloc_int(2)
    return ident, cat, {
        "category": cat, "control_type": "selector", "description": desc,
        "identifier": ident,
        "inputs": _inputs_set(2),
        "outputs": [_int_output(alloc, "selector position")],
        "physical_variant": "toggle_switch",
    }


@_register("defineReadWriteRadio")
def _h_rw_radio(mem, args):
    ident = _parse_str(args[0])
    max_len = int(_parse_num(args[2]))
    desc = _parse_str(args[5])
    cat = "Radio Frequencies"
    alloc = mem.alloc_string(max_len)
    return ident, cat, {
        "category": cat, "control_type": "radio", "description": desc,
        "identifier": ident,
        "inputs": [{"description": "The frequency to set", "interface": "set_string"}],
        "outputs": [_str_output(alloc, "current frequency")],
    }


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

_RE_BLOCK_OPEN = re.compile(r'\b(function|if|do)\b')
_RE_BLOCK_CLOSE = re.compile(r'\bend\b')


def _strip_lua_strings(line: str) -> str:
    """Remove string literals and ``--`` comments so keyword counting is safe."""
    out: list[str] = []
    in_str = False
    esc = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if esc:
            esc = False
            i += 1
            continue
        if in_str:
            if ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == '-' and i + 1 < n and line[i + 1] == '-':
            break
        out.append(ch)
        i += 1
    return ''.join(out)


def _block_delta(line: str) -> int:
    """Net change in Lua block nesting depth for *line*."""
    clean = _strip_lua_strings(line)
    return len(_RE_BLOCK_OPEN.findall(clean)) - len(_RE_BLOCK_CLOSE.findall(clean))


def _extract_calls(lua: str, var_name: str) -> list[tuple[str, str]]:
    """Return list of (define_type, full_call_text) for each invocation."""
    prefix = f"{var_name}:define"
    func_def_prefix = f"function {var_name}:define"

    lines = lua.split('\n')
    calls: list[tuple[str, str]] = []
    accum: str | None = None
    paren = 0
    func_depth = 0
    in_func_def = False
    func_def_depth = 0

    for line in lines:
        stripped = line.strip()

        # --- accumulating a multi-line define* call ---
        if accum is not None:
            accum += '\n' + line
            for ch in line:
                if ch == '(':
                    paren += 1
                elif ch == ')':
                    paren -= 1
            func_depth += _block_delta(line)
            if paren <= 0 and func_depth <= 0:
                m = re.match(rf'{re.escape(var_name)}:(define\w+)', accum.strip())
                if m:
                    calls.append((m.group(1), accum.strip()))
                accum = None
            continue

        if not stripped or stripped.startswith('--'):
            continue

        # --- inside a method definition on the module class ---
        if stripped.startswith(func_def_prefix):
            in_func_def = True
            func_def_depth = _block_delta(stripped)
            if func_def_depth <= 0:
                in_func_def = False
            continue
        if in_func_def:
            func_def_depth += _block_delta(stripped)
            if func_def_depth <= 0:
                in_func_def = False
            continue

        # --- actual define* calls ---
        if stripped.startswith(prefix) and not stripped.startswith(func_def_prefix):
            accum = line
            paren = 0
            func_depth = 0
            for ch in line:
                if ch == '(':
                    paren += 1
                elif ch == ')':
                    paren -= 1
            func_depth += _block_delta(line)
            if paren <= 0 and func_depth <= 0:
                m = re.match(rf'{re.escape(var_name)}:(define\w+)', accum.strip())
                if m:
                    calls.append((m.group(1), accum.strip()))
                accum = None

    return calls


def lua_to_json(lua_text: str) -> dict:
    """Parse a DCS-BIOS Lua module file and return the JSON-compatible dict.

    Returns ``{ "Category": { "IDENT": { … }, … }, … }``
    """
    # Strip optional markdown headers from uploaded files
    lua = lua_text
    for hdr in ('Source URL:', 'Title:'):
        while True:
            idx = lua.find(hdr)
            if idx == -1:
                break
            end = lua.find('\n', idx)
            lua = lua[:idx] + lua[end + 1:] if end != -1 else lua[:idx]

    # Find module variable name and base address
    m = re.search(
        r'local\s+(\w+)\s*=\s*Module:new\(\s*"[^"]*"\s*,\s*(0x[0-9a-fA-F]+)',
        lua,
    )
    if not m:
        raise ValueError("Could not find Module:new() in Lua source")
    var_name = m.group(1)
    base_addr = int(m.group(2), 16)

    mem = _MemoryMap(base_addr)
    result: dict[str, dict] = {}

    calls = _extract_calls(lua, var_name)
    skipped: list[str] = []

    for define_type, call_text in calls:
        handler = _HANDLERS.get(define_type)
        if handler is None:
            skipped.append(define_type)
            continue

        # Extract the inner arguments between outermost parens
        open_idx = call_text.index('(')
        close_idx = call_text.rindex(')')
        inner = call_text[open_idx + 1:close_idx]

        try:
            args = _split_args(inner)
            ident, cat, ctrl = handler(mem, args)
        except Exception as exc:
            print(f"  WARNING: failed to parse {define_type}: {exc}",
                  file=sys.stderr)
            continue

        if ident is None:
            continue

        if cat not in result:
            result[cat] = {}
        result[cat][ident] = ctrl

    if skipped:
        unique = sorted(set(skipped))
        print(f"  NOTE: skipped unknown define types: {', '.join(unique)}",
              file=sys.stderr)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input.lua> [output.json]", file=sys.stderr)
        sys.exit(1)

    lua_path = Path(sys.argv[1])
    lua_text = lua_path.read_text(encoding="utf-8", errors="replace")

    result = lua_to_json(lua_text)

    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else lua_path.with_suffix('.json')
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n',
                        encoding='utf-8')

    n_cats = len(result)
    n_ctrls = sum(len(v) for v in result.values())
    print(f"Wrote {n_ctrls} controls in {n_cats} categories → {out_path}")


if __name__ == "__main__":
    main()
