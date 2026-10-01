"""Configuration parsing and validation."""

from __future__ import annotations

import os
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

from .models import ComparisonError, Settings


def parse_iso_date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ComparisonError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc


def load_settings(path: Path) -> Settings:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ComparisonError(f"config file is missing: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ComparisonError(f"cannot read config {path}: {exc}") from exc

    try:
        run = raw["run"]
        paths = raw["paths"]
        services = raw["services"]
        provider_config = raw["providers"]
        symbols = tuple(str(item).upper() for item in run["symbols"])
        providers = tuple(str(item).lower() for item in run["providers"])
        from_date = parse_iso_date(run["from_date"], "run.from_date")
        to_date = parse_iso_date(run["to_date"], "run.to_date")
    except (KeyError, TypeError) as exc:
        raise ComparisonError(f"invalid config structure: {exc}") from exc

    if not symbols or len(set(symbols)) != len(symbols):
        raise ComparisonError("run.symbols must contain unique symbols")
    if not providers or len(set(providers)) != len(providers):
        raise ComparisonError("run.providers must contain unique providers")
    if from_date > to_date:
        raise ComparisonError("run.from_date must not be after run.to_date")
    unknown = set(providers) - {"reality", "xstocks", "robinhood"}
    if unknown:
        raise ComparisonError(f"unsupported providers: {sorted(unknown)}")

    for provider in providers:
        if provider not in provider_config:
            raise ComparisonError(f"provider {provider!r} has no config section")

    return Settings(
        symbols=symbols,
        providers=providers,
        from_date=from_date,
        to_date=to_date,
        data_dir=Path(str(paths["data"])),
        output_dir=Path(str(paths["output"])),
        alpha_vantage_url=str(services["alpha_vantage_url"]),
        blockscout_url=str(services["blockscout_url"]),
        provider_config={key: dict(value) for key, value in provider_config.items()},
    )


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ComparisonError(f"environment variable {name} is required")
    return value
