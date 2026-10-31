"""Editable dropdown option lists for the Import grid.

One ``data/si_options.json`` holds the flat lists for the constrained columns
(PG, WP, PRIOR, USTATUS, WO) plus the WO_TYPE -> WO_KEY hierarchy, seeded from
the workbook's lookup tables on first load. The hierarchy is the single source
for both the WO_TYPE column (its parents) and the WO_KEY column (a parent's
children). REV is not here: it follows the active week's register
(``revisions.get_revisions``).

Pure functions over the option dicts; the CRUD window reads and writes the file,
the grid reads the lists via ``get_options``.
"""

from __future__ import annotations

import json
from pathlib import Path

# The flat constrained columns, keyed by the row field they edit.
OPTION_KEYS = ("PG", "WP", "PRIOR", "USTATUS", "WO")

# Seed values from 2SAP_v2_2.xlsm: PG from the B3 validation, WP from the WP
# column, PRIOR from the Prior table, USTATUS from Tabel4 (STATUS_KEY), WO from
# the WO? flag.
_SEED = {
    "PG": ["WP1", "WP2", "WP3"],
    "WP": ["WP-G", "WP-M-E", "WP-M-J", "WP-M-N", "WP-M-W", "WP-S",
           "EX-ACT", "EX-BELT", "EX-BELTZ", "EX-KV", "EX-MBS", "EX-VERM"],
    "PRIOR": ["1", "2", "3", "4"],
    "USTATUS": ["10CR", "20WA", "30PR", "40WM", "50RS", "60RE", "70IE", "80EV"],
    "WO": ["0", "1"],
}

# WO_TYPE -> its WO_KEY children, from the workbook's dependent list
# (ZA03 -> REP, INS; ZA02 -> INS, LUB, CLE, SAF, OVR). Editable in the
# Manage-values window; it drives both the WO_TYPE and WO_KEY columns.
TREE_KEY = "WO_TYPE_CHILDREN"
DEFAULT_WO_TREE = {
    "ZA02": ["INS", "LUB", "CLE", "SAF", "OVR"],
    "ZA03": ["REP", "INS"],
}


_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "si_options.json"


def default_options() -> dict[str, list[str]]:
    return {k: list(v) for k, v in _SEED.items()}


def default_wo_tree() -> dict[str, list[str]]:
    return {k: list(v) for k, v in DEFAULT_WO_TREE.items()}


def _clean(values) -> list[str]:
    out: list[str] = []
    for v in values if isinstance(values, list) else []:
        text = str(v).strip()
        if text and text not in out:
            out.append(text)
    return out


def _load_raw() -> dict:
    if not _DATA_PATH.exists():
        return {}
    try:
        data = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_options() -> dict[str, list[str]]:
    """The saved flat lists, with every known key present and seeded when missing."""
    options = default_options()
    raw = _load_raw()
    for key in OPTION_KEYS:
        if key in raw:
            options[key] = _clean(raw[key])
    return options


def load_wo_tree() -> dict[str, list[str]]:
    """The WO_TYPE -> WO_KEY hierarchy, seeded with the workbook's defaults."""
    raw = _load_raw().get(TREE_KEY)
    if isinstance(raw, dict):
        return {str(k): _clean(v) for k, v in raw.items()}
    return default_wo_tree()


def save_all(options: dict, wo_tree: dict) -> None:
    data: dict = {k: _clean(options.get(k, [])) for k in OPTION_KEYS}
    data[TREE_KEY] = {str(k): _clean(v) for k, v in wo_tree.items()}
    _DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def save_options(options: dict) -> None:
    save_all(options, load_wo_tree())


def get_options(key: str) -> list[str]:
    return load_options().get(key, [])


def wo_type_options() -> list[str]:
    """The WO_TYPE column's values: the hierarchy's parents."""
    return list(load_wo_tree().keys())


def wo_key_options() -> list[str]:
    """The WO_KEY column's fallback values: every child, in first-seen order."""
    keys: list[str] = []
    for children in load_wo_tree().values():
        for child in children:
            if child not in keys:
                keys.append(child)
    return keys


def wo_keys_for(wo_type, all_keys: list[str]) -> list[str]:
    """The WO_KEYs a WO_TYPE owns; all of them when the type isn't in the tree."""
    children = load_wo_tree().get(str(wo_type).strip() if wo_type else "")
    return list(children) if children else list(all_keys)


def dropdown_lists(rev_options: list[str] | None = None) -> dict[str, list[str]]:
    """Every constrained column's picker list: the JSON lists, the WO hierarchy
    for WO_TYPE/WO_KEY, and the register for REV. Shared by the grid and the
    edit modal so the two can never offer different values."""
    lists = {key: list(values) for key, values in load_options().items()}
    lists["WO_TYPE"] = wo_type_options()
    lists["WO_KEY"] = wo_key_options()
    if rev_options is not None:
        lists["REV"] = list(rev_options)
    return lists


def add_wo_type(tree: dict, name: str) -> bool:
    text = str(name).strip()
    if not text or text in tree:
        return False
    tree[text] = []
    return True


def remove_wo_type(tree: dict, name: str) -> bool:
    if str(name) in tree:
        del tree[str(name)]
        return True
    return False


def rename_wo_type(tree: dict, old: str, new: str) -> bool:
    text = str(new).strip()
    if str(old) not in tree or not text or (text != str(old) and text in tree):
        return False
    rebuilt = {text if k == str(old) else k: v for k, v in tree.items()}
    tree.clear()
    tree.update(rebuilt)
    return True


def add_option(options: dict, key: str, value: str) -> bool:
    text = str(value).strip()
    if not text or text in options.get(key, []):
        return False
    options.setdefault(key, []).append(text)
    return True


def remove_option(options: dict, key: str, value: str) -> bool:
    if str(value) in options.get(key, []):
        options[key].remove(str(value))
        return True
    return False


def rename_option(options: dict, key: str, old: str, new: str) -> bool:
    text = str(new).strip()
    values = options.get(key, [])
    if str(old) not in values or not text or (text != str(old) and text in values):
        return False
    values[values.index(str(old))] = text
    return True


def file_path() -> Path:
    return _DATA_PATH
