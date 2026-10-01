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


def run(
    settings: Settings,
    *,
    collect_api_data: bool = False,
    collect_logs_data: bool = False,
) -> int:
    if collect_api_data:
        collect_alpha(settings)
        collect_assets(settings)
    assets = resolve_assets(settings)
    if collect_logs_data:
        collect_logs(settings, assets)
    dividends = load_dividends(settings)
    events = decode_all_events(settings, assets)
    enricher = RpcEnricher(settings, collect=collect_logs_data)
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
    parser.add_argument(
        "--collect-api-data",
        action="store_true",
        help="fetch fresh Alpha Vantage and provider asset data",
    )
    parser.add_argument(
        "--collect-logs",
        action="store_true",
        help="fetch fresh Blockscout logs and RPC enrichment data",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        return run(
            settings,
            collect_api_data=bool(args.collect_api_data),
            collect_logs_data=bool(args.collect_logs),
        )
    except (ComparisonError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
