"""
TRIO — Sector map (Phase 7)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Loads config/sectors.yaml into a frozen {symbol -> sector} dict. Unknown
symbols map to "Unknown". Pure helpers; no network.
"""
from pathlib import Path
from typing import Any, Dict
from typing import Set

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SECTOR_PATH = PROJECT_ROOT / "config" / "sectors.yaml"

_cache: Dict[str, str] = {}


def _load_map(path: Path = DEFAULT_SECTOR_PATH) -> Dict[str, str]:
    if _cache:
        return dict(_cache)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        mapping = data.get("mapping") or {}
        for k, v in mapping.items():
            _cache[str(k).strip()] = str(v).strip()
    except Exception:
        pass
    return dict(_cache)


def sector_for(symbol: str) -> str:
    m = _load_map()
    return m.get(str(symbol).strip(), "Unknown")


def all_sectors() -> Set[str]:
    return set(_load_map().values()) | {"Unknown"}
