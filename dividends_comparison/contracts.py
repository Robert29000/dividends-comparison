"""Load contract ABIs from the repository's JSON files."""

from __future__ import annotations

import json
import sysconfig
from functools import cache
from pathlib import Path
from typing import Any

from .models import ComparisonError

SOURCE_ABI_DIRECTORY = Path(__file__).resolve().parent.parent / "abis"
INSTALLED_ABI_DIRECTORY = (
    Path(sysconfig.get_path("data")) / "share" / "dividends-comparison" / "abis"
)

REALITY_ORACLE = "0xd01f8aa971a3f4d13d52299bdd30225b1d7f40f1"
DIVIDEND_ACTION_TYPE = 2
EXECUTED_STATUS = 2


@cache
def load_abi(filename: str) -> list[dict[str, Any]]:
    source_path = SOURCE_ABI_DIRECTORY / filename
    path = source_path if source_path.is_file() else INSTALLED_ABI_DIRECTORY / filename
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ComparisonError(f"ABI file is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError(f"cannot read ABI file {path}: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ComparisonError(f"ABI file must contain a JSON array of objects: {path}")
    return value


def abi_entry(abi: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [item for item in abi if item.get("name") == name]
    if len(matches) != 1:
        raise ComparisonError(f"ABI must contain exactly one {name!r} entry")
    return matches[0]
