"""
dcs_config.py — single place that resolves all file paths for the DCS Cockpit GUI.

To swap an image or add a new panel:
  - Put the new image in the panels_dir folder (default: "panel pics/")
  - Edit config/panel_categories.json to add/rename the panel key
  - Edit config/app_config.json to set a new cockpit_image if needed
  - Nothing else changes.
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# ── Config file locations (these never move) ──────────────────────────────────
CONFIG_DIR       = BASE_DIR / "config"
APP_CONFIG_FILE  = CONFIG_DIR / "app_config.json"
CATEGORIES_FILE  = CONFIG_DIR / "panel_categories.json"
LAYOUT_FILE      = CONFIG_DIR / "panel_layout.json"
CTRL_POS_FILE    = CONFIG_DIR / "control_positions.json"


def load_app_config() -> dict:
    """Load config/app_config.json, returning defaults if the file is missing."""
    defaults = {
        "cockpit_image": None,
        "panels_dir": "panel pics",
        "bios_file": "bios_defs/FA-18C_hornet.json",
        "aircraft": "FA-18C Hornet",
    }
    if APP_CONFIG_FILE.exists():
        with open(APP_CONFIG_FILE) as f:
            raw = json.load(f)
        # Merge, skipping comment keys
        for k, v in raw.items():
            if not k.startswith("_"):
                defaults[k] = v
    return defaults


def panels_dir() -> Path:
    return BASE_DIR / load_app_config()["panels_dir"]


def bios_file() -> Path:
    return BASE_DIR / load_app_config()["bios_file"]


def panel_filenames() -> list[str]:
    """
    Return the ordered list of panel image filenames from panel_categories.json.
    This is the single source of truth — no hardcoded list anywhere else.
    """
    if not CATEGORIES_FILE.exists():
        return []
    with open(CATEGORIES_FILE) as f:
        data = json.load(f)
    return [k for k in data if not k.startswith("_")]


def load_panel_categories() -> dict[str, list[str]]:
    """Return {panel_filename: [category, ...]} from panel_categories.json."""
    if not CATEGORIES_FILE.exists():
        return {}
    with open(CATEGORIES_FILE) as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, list)}


def all_panel_images() -> list[tuple[str, bool]]:
    """
    Return every panel image that should appear in the Layout Wizard.

    Each entry is (filename, is_categorized):
      - is_categorized=True  → already in panel_categories.json; main app uses it
      - is_categorized=False → image exists in panels_dir but has no category
                               assignment yet; main app ignores it until added

    The cockpit background image is excluded from this list.
    Images are returned in category-file order first, then alphabetically for new ones.
    """
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
    categorized = panel_filenames()           # ordered list from config
    categorized_lower = {f.lower() for f in categorized}

    # Resolve cockpit background so we can exclude it
    bg = resolve_cockpit_image()
    bg_name_lower = bg.name.lower() if bg else ""

    pdir = panels_dir()
    uncategorized = []
    if pdir.exists():
        for f in sorted(pdir.iterdir()):
            if f.suffix.lower() not in IMAGE_EXTS:
                continue
            if f.name.lower() == bg_name_lower:
                continue
            if f.name.lower() not in categorized_lower:
                uncategorized.append(f.name)

    return [(name, True) for name in categorized] + \
           [(name, False) for name in uncategorized]


def add_panel_to_categories(filename: str):
    """Add a new panel image with an empty category list to panel_categories.json."""
    if not CATEGORIES_FILE.exists():
        return
    with open(CATEGORIES_FILE) as f:
        data = json.load(f)
    if filename not in data:
        data[filename] = []
        with open(CATEGORIES_FILE, "w") as f:
            json.dump(data, f, indent=2)


def resolve_cockpit_image() -> Path | None:
    """
    Find the full cockpit background image.
    Priority:
      1. Explicit path in app_config.json  ("cockpit_image")
      2. Auto-detect: largest image in panels_dir not listed as a panel filename
    Returns None if nothing is found (caller should show a helpful error).
    """
    cfg = load_app_config()
    pdir = BASE_DIR / cfg["panels_dir"]

    # 1. Explicit
    if cfg.get("cockpit_image"):
        p = BASE_DIR / cfg["cockpit_image"]
        if p.exists():
            return p
        # Try inside panels_dir as a convenience
        p2 = pdir / cfg["cockpit_image"]
        if p2.exists():
            return p2

    # 2. Auto-detect: largest image not used as a panel
    panel_names_lower = {n.lower() for n in panel_filenames()}
    candidates = []
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}
    if pdir.exists():
        for f in pdir.iterdir():
            if f.suffix.lower() in image_exts and f.name.lower() not in panel_names_lower:
                try:
                    size = f.stat().st_size
                    candidates.append((size, f))
                except OSError:
                    pass
    # Also check the project root
    for f in BASE_DIR.iterdir():
        if f.suffix.lower() in image_exts:
            try:
                size = f.stat().st_size
                candidates.append((size, f))
            except OSError:
                pass

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    return None


def save_cockpit_image_to_config(rel_path: str):
    """Persist the chosen cockpit_image path back into app_config.json."""
    cfg = load_app_config()
    cfg["cockpit_image"] = rel_path
    CONFIG_DIR.mkdir(exist_ok=True)
    existing = {}
    if APP_CONFIG_FILE.exists():
        with open(APP_CONFIG_FILE) as f:
            existing = json.load(f)
    existing.update({k: v for k, v in cfg.items() if not k.startswith("_")})
    with open(APP_CONFIG_FILE, "w") as f:
        json.dump(existing, f, indent=2)


# ── Control overrides ─────────────────────────────────────────────────────────

OVERRIDES_FILE = CONFIG_DIR / "control_overrides.json"


def load_control_overrides() -> dict[str, str]:
    """
    Return {ctrl_id: panel_filename} for controls manually moved to a different
    panel than their category assignment.  Stored in config/control_overrides.json.
    """
    if not OVERRIDES_FILE.exists():
        return {}
    with open(OVERRIDES_FILE) as f:
        return json.load(f)


def save_control_override(ctrl_id: str, panel_filename: str):
    """Assign ctrl_id to panel_filename, overriding its category assignment."""
    overrides = load_control_overrides()
    overrides[ctrl_id] = panel_filename
    CONFIG_DIR.mkdir(exist_ok=True)
    with open(OVERRIDES_FILE, "w") as f:
        json.dump(overrides, f, indent=2, sort_keys=True)


def remove_control_override(ctrl_id: str):
    """Remove any override for ctrl_id (reverts to category assignment)."""
    overrides = load_control_overrides()
    if ctrl_id in overrides:
        del overrides[ctrl_id]
        with open(OVERRIDES_FILE, "w") as f:
            json.dump(overrides, f, indent=2, sort_keys=True)


# ── Excluded controls ─────────────────────────────────────────────────────────

def load_excluded_controls() -> set[str]:
    """
    Return the set of ctrl_ids that should never be shown on any panel.
    Stored as "_excluded": [...] in panel_categories.json.
    """
    if not CATEGORIES_FILE.exists():
        return set()
    with open(CATEGORIES_FILE) as f:
        data = json.load(f)
    return set(data.get("_excluded", []))


def save_excluded_control(ctrl_id: str):
    """Add ctrl_id to the _excluded list in panel_categories.json."""
    if not CATEGORIES_FILE.exists():
        return
    with open(CATEGORIES_FILE) as f:
        data = json.load(f)
    excluded = data.get("_excluded", [])
    if ctrl_id not in excluded:
        excluded.append(ctrl_id)
        excluded.sort()
    data["_excluded"] = excluded
    with open(CATEGORIES_FILE, "w") as f:
        json.dump(data, f, indent=2)


def unexclude_control(ctrl_id: str):
    """Remove ctrl_id from the _excluded list."""
    if not CATEGORIES_FILE.exists():
        return
    with open(CATEGORIES_FILE) as f:
        data = json.load(f)
    excluded = [x for x in data.get("_excluded", []) if x != ctrl_id]
    data["_excluded"] = excluded
    with open(CATEGORIES_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Display-type overrides ────────────────────────────────────────────────────

DISPLAY_OVERRIDES_FILE = CONFIG_DIR / "control_display_overrides.json"
# Allowed values stored per ctrl_id: "dot" | "rect"


def load_display_overrides() -> dict[str, str]:
    """Return {ctrl_id: "dot"|"rect"} manual visual-type overrides."""
    if not DISPLAY_OVERRIDES_FILE.exists():
        return {}
    with open(DISPLAY_OVERRIDES_FILE) as f:
        return json.load(f)


def save_display_override(ctrl_id: str, override: str):
    """Set a visual-type override for ctrl_id ("dot" or "rect")."""
    data = load_display_overrides()
    data[ctrl_id] = override
    CONFIG_DIR.mkdir(exist_ok=True)
    with open(DISPLAY_OVERRIDES_FILE, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def remove_display_override(ctrl_id: str):
    """Remove any visual-type override, reverting to the DCS-BIOS default."""
    data = load_display_overrides()
    if ctrl_id in data:
        del data[ctrl_id]
        with open(DISPLAY_OVERRIDES_FILE, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)


# ── Panel category editing ────────────────────────────────────────────────────

def assign_categories_to_panel(panel_filename: str, categories: list[str]):
    """
    Set the complete list of categories for panel_filename.
    Removes those categories from any other panel they currently belong to
    (each category can only belong to one panel at a time).
    """
    if not CATEGORIES_FILE.exists():
        return
    with open(CATEGORIES_FILE) as f:
        data = json.load(f)

    new_cats = set(categories)

    # Remove these categories from every other panel
    for key, val in data.items():
        if key.startswith("_") or not isinstance(val, list):
            continue
        if key != panel_filename:
            data[key] = [c for c in val if c not in new_cats]

    # Assign to target panel (preserve _comment keys)
    data[panel_filename] = sorted(categories)

    with open(CATEGORIES_FILE, "w") as f:
        json.dump(data, f, indent=2)
