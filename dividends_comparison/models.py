"""Shared domain models and user-facing errors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


class ComparisonError(RuntimeError):
    """A user-facing collection, configuration, or cache error."""


@dataclass(frozen=True)
class Settings:
    symbols: tuple[str, ...]
    providers: tuple[str, ...]
    from_date: date
    to_date: date
    data_dir: Path
    output_dir: Path
    alpha_vantage_url: str
    blockscout_url: str
    provider_config: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class Dividend:
    symbol: str
    ex_date: date
    declaration_date: str
    record_date: str
    payment_date: str
    amount: str
