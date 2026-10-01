"""Dividend/event matching and wide CSV report generation."""

from __future__ import annotations

import csv
import io
import itertools
from datetime import date
from typing import Any, Iterable, Mapping, Protocol

from .cache import atomic_write
from .models import Dividend, Settings

BASE_FIELDS = (
    "symbol",
    "ex_date",
    "declaration_date",
    "record_date",
    "payment_date",
    "dividend_amount",
)

PROVIDER_FIELDS = (
    "match_status",
    "contract_address",
    "event_name",
    "execution_at",
    "activation_time",
    "event_date",
    "candidate_index",
    "candidate_count",
    "block_number",
    "block_date",
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


def matching_candidates(
    candidates: Iterable[dict[str, Any]], ex_date: date
) -> list[dict[str, Any]]:
    matched = [
        dict(candidate)
        for candidate in candidates
        if date.fromisoformat(str(candidate["event_date"])) == ex_date
    ]
    matched.sort(key=lambda item: (item["block_number"], item["log_index"]))
    count = len(matched)
    for index, candidate in enumerate(matched, start=1):
        candidate["candidate_index"] = index
        candidate["candidate_count"] = count
    return matched


def provider_columns(
    provider: str,
    candidate: Mapping[str, Any] | None,
    address: str | None,
    enrichment: Mapping[str, Any] | None,
) -> dict[str, Any]:
    values = {f"{provider}_{field}": "" for field in PROVIDER_FIELDS}
    if address is None:
        values[f"{provider}_match_status"] = "asset_not_found"
        return values
    if candidate is None:
        values[f"{provider}_match_status"] = "no_match"
        values[f"{provider}_contract_address"] = address
        return values
    values[f"{provider}_match_status"] = "matched"
    for field in PROVIDER_FIELDS:
        if field != "match_status" and field in candidate:
            values[f"{provider}_{field}"] = candidate[field]
    if enrichment:
        for field, value in enrichment.items():
            values[f"{provider}_{field}"] = value
    return values


def build_report_rows(
    settings: Settings,
    dividends: Mapping[str, list[Dividend]],
    assets: Mapping[str, Mapping[str, str | None]],
    events: Mapping[str, Mapping[str, list[dict[str, Any]]]],
    enricher: Enricher,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol in settings.symbols:
        for dividend in dividends[symbol]:
            by_provider: dict[str, list[dict[str, Any] | None]] = {}
            for provider in settings.providers:
                matches = matching_candidates(
                    events[provider][symbol],
                    dividend.ex_date,
                )
                by_provider[provider] = matches or [None]

            for combination in itertools.product(
                *(by_provider[provider] for provider in settings.providers)
            ):
                row: dict[str, Any] = {
                    "symbol": symbol,
                    "ex_date": dividend.ex_date.isoformat(),
                    "declaration_date": dividend.declaration_date,
                    "record_date": dividend.record_date,
                    "payment_date": dividend.payment_date,
                    "dividend_amount": dividend.amount,
                }
                for provider, candidate in zip(settings.providers, combination):
                    enrichment = None
                    if candidate is not None:
                        enrichment = enricher.get(provider, symbol, candidate)
                    row.update(
                        provider_columns(
                            provider,
                            candidate,
                            assets[provider][symbol],
                            enrichment,
                        )
                    )
                rows.append(row)
    return rows


def write_report(settings: Settings, rows: Iterable[Mapping[str, Any]]) -> int:
    fieldnames = list(BASE_FIELDS) + [
        f"{provider}_{field}"
        for provider in settings.providers
        for field in PROVIDER_FIELDS
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    count = 0
    for row in rows:
        writer.writerow(row)
        count += 1
    atomic_write(settings.output_path, buffer.getvalue())
    return count
