"""Write independent Nasdaq and blockchain-provider CSV datasets."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .cache import atomic_write
from .models import Dividend, Settings

NASDAQ_FIELDS = (
    "symbol",
    "ex_dividend_date",
    "declaration_date",
    "record_date",
    "payment_date",
    "amount",
)

PROVIDER_FIELDS = (
    "symbol",
    "contract_address",
    "event_name",
    "effective_timestamp",
    "effective_at_iso",
    "effective_date",
    "block_number",
    "block_timestamp",
    "block_at_iso",
    "log_index",
    "transaction_hash",
    "total_supply",
    "total_supply_snapshot_block",
    "payment_per_share",
    "payout_token",
    "previous_multiplier",
    "proposed_multiplier",
)


class Enricher(Protocol):
    def get(
        self,
        provider: str,
        symbol: str,
        event: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


def output_path(settings: Settings, source: str) -> Path:
    return settings.output_dir / f"{source}.csv"


def build_nasdaq_rows(
    settings: Settings,
    dividends: Mapping[str, list[Dividend]],
) -> list[dict[str, Any]]:
    return [
        {
            "symbol": symbol,
            "ex_dividend_date": dividend.ex_date.isoformat(),
            "declaration_date": dividend.declaration_date,
            "record_date": dividend.record_date,
            "payment_date": dividend.payment_date,
            "amount": dividend.amount,
        }
        for symbol in settings.symbols
        for dividend in dividends[symbol]
    ]


def build_provider_rows(
    settings: Settings,
    provider: str,
    events: Mapping[str, list[dict[str, Any]]],
    enricher: Enricher,
) -> list[dict[str, Any]]:
    rows = []
    for symbol in settings.symbols:
        for event in events[symbol]:
            row = dict(event)
            row.update(enricher.get(provider, symbol, event))
            rows.append(row)
    return rows


def write_report(
    settings: Settings,
    source: str,
    fieldnames: Iterable[str],
    rows: Iterable[Mapping[str, Any]],
) -> int:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(fieldnames),
        extrasaction="ignore",
    )
    writer.writeheader()
    count = 0
    for row in rows:
        writer.writerow(row)
        count += 1
    atomic_write(output_path(settings, source), buffer.getvalue())
    return count
