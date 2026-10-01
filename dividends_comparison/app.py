"""Application orchestration and command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .collection import (
    collect_alpha,
    collect_assets,
    collect_logs,
    load_dividends,
    resolve_assets,
)
from .config import load_settings
from .enrichment import RpcEnricher
from .events import decode_all_events
from .models import ComparisonError, Settings
from .reporting import build_report_rows, write_report


def run(settings: Settings, collect: bool) -> int:
    if collect:
        collect_alpha(settings)
        collect_assets(settings)
    assets = resolve_assets(settings)
    if collect:
        collect_logs(settings, assets)
    dividends = load_dividends(settings)
    events = decode_all_events(settings, assets)
    enricher = RpcEnricher(settings, collect=collect)
    rows = build_report_rows(settings, dividends, assets, events, enricher)
    enricher.save()
    count = write_report(settings, rows)
    print(f"wrote {count} rows to {settings.output_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare stock dividends with tokenized-stock corporate actions."
    )
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--collect",
        action="store_true",
        help="fetch fresh API, Blockscout, and RPC data before reporting",
    )
    mode.add_argument(
        "--use-existing",
        action="store_true",
        help="make no network calls and report only from cache files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        return run(settings, collect=bool(args.collect))
    except (ComparisonError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
