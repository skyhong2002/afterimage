"""Shared read/write for catalog/wallpapers.yaml.

Lists of scalars (size, palette, similar, ...) are written in one-line flow
style so diffs stay readable. Hand-edited fields are never touched here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "wallpapers.yaml"


class _Dumper(yaml.SafeDumper):
    pass


def _represent_list(dumper: yaml.SafeDumper, data: list):
    flow = all(isinstance(x, (str, int, float)) for x in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


_Dumper.add_representer(list, _represent_list)


def load() -> list[dict]:
    if not CATALOG.exists():
        return []
    return yaml.safe_load(CATALOG.read_text(encoding="utf-8")) or []


def dump(data: object) -> str:
    return yaml.dump(data, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100)


def save(entries: list[dict]) -> None:
    CATALOG.parent.mkdir(exist_ok=True)
    CATALOG.write_text(dump(entries), encoding="utf-8")
