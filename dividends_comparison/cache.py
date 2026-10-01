"""Atomic cache IO and cache path conventions."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import ComparisonError, Settings


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ComparisonError(f"required cache file is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError(f"cannot read JSON cache {path}: {exc}") from exc


def date_suffix(settings: Settings) -> str:
    return f"{settings.from_date.isoformat()}_{settings.to_date.isoformat()}"


def alpha_cache_path(settings: Settings, symbol: str) -> Path:
    return settings.data_dir / "nasdaq" / f"{symbol}_{date_suffix(settings)}.csv"


def asset_cache_path(settings: Settings, provider: str, symbol: str = "assets") -> Path:
    return settings.data_dir / "assets" / provider / f"{symbol}.json"


def log_cache_path(settings: Settings, provider: str, name: str) -> Path:
    return settings.data_dir / "logs" / provider / f"{name}_{date_suffix(settings)}.csv"


def rpc_cache_path(settings: Settings) -> Path:
    return settings.data_dir / "rpc" / f"enrichment_{date_suffix(settings)}.json"
