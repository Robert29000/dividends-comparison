"""HTTP collection, asset resolution, and dividend input loading."""

from __future__ import annotations

import csv
import json
from datetime import datetime, time
from time import sleep
from typing import Any, Mapping

import requests

from .cache import (
    alpha_cache_path,
    asset_cache_path,
    atomic_write,
    log_cache_path,
    read_json,
    write_json,
)
from .config import parse_iso_date, require_env
from .contracts import REALITY_ORACLE
from .models import ComparisonError, Dividend, Settings


def http_get(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    timeout: int = 60,
    allow_not_found: bool = False,
) -> requests.Response:
    request_url = requests.Request("GET", url, params=params).prepare().url or url
    try:
        response = requests.get(url, params=params, timeout=timeout)
        if allow_not_found and response.status_code == 404:
            return response
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        if exc.request is not None and exc.request.url:
            request_url = exc.request.url
        raise ComparisonError(f"GET {request_url} failed: {exc}") from exc


def collect_alpha(settings: Settings) -> None:
    api_key = require_env("ALPHAVANTAGE_API_KEY")
    for index, symbol in enumerate(settings.symbols):
        if index:
            sleep(1.1)
        response = http_get(
            settings.alpha_vantage_url,
            params={
                "function": "DIVIDENDS",
                "symbol": symbol,
                "datatype": "csv",
                "apikey": api_key,
            },
        )
        text = response.text
        if text.lstrip().startswith("{"):
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                message = text[:300]
            if message == {}:
                message = "empty response, likely caused by API request throttling"
            raise ComparisonError(
                f"Alpha Vantage did not return dividend CSV for {symbol}: {message}"
            )
        fieldnames = next(csv.reader(text.splitlines()), [])
        required = {"ex_dividend_date", "payment_date", "amount"}
        if not required.issubset(fieldnames):
            raise ComparisonError(
                f"Alpha Vantage CSV for {symbol} is missing "
                f"{sorted(required - set(fieldnames))}"
            )
        atomic_write(alpha_cache_path(settings, symbol), text)


def collect_assets(settings: Settings) -> None:
    if "reality" in settings.providers:
        url = str(settings.provider_config["reality"]["asset_url"])
        for symbol in settings.symbols:
            response = http_get(url, params={"ticker": symbol})
            try:
                payload = response.json()
            except requests.JSONDecodeError as exc:
                raise ComparisonError(
                    f"Reality returned invalid JSON for {symbol}"
                ) from exc
            write_json(asset_cache_path(settings, "reality", symbol), payload)

    if "xstocks" in settings.providers:
        template = str(settings.provider_config["xstocks"]["asset_url"])
        for symbol in settings.symbols:
            response = http_get(
                template.format(symbol=symbol),
                allow_not_found=True,
            )
            if response.status_code == 404:
                write_json(asset_cache_path(settings, "xstocks", symbol), {})
                continue
            try:
                payload = response.json()
            except requests.JSONDecodeError as exc:
                raise ComparisonError(
                    f"xStocks returned invalid JSON for {symbol}"
                ) from exc
            write_json(asset_cache_path(settings, "xstocks", symbol), payload)

    if "robinhood" in settings.providers:
        url = str(settings.provider_config["robinhood"]["asset_url"])
        response = http_get(url)
        try:
            payload = response.json()
        except requests.JSONDecodeError as exc:
            raise ComparisonError("Robinhood returned invalid asset JSON") from exc
        write_json(asset_cache_path(settings, "robinhood"), payload)


def resolve_assets(settings: Settings) -> dict[str, dict[str, str | None]]:
    resolved: dict[str, dict[str, str | None]] = {
        provider: {} for provider in settings.providers
    }
    if "reality" in settings.providers:
        network = str(settings.provider_config["reality"]["network"])
        for symbol in settings.symbols:
            payload = read_json(asset_cache_path(settings, "reality", symbol))
            matches = [
                str(token["address"]).lower()
                for asset in payload.get("data", [])
                if str(asset.get("ticker", "")).upper() == symbol
                for token in asset.get("tokens", [])
                if token.get("network") == network and token.get("address")
            ]
            if len(matches) > 1:
                raise ComparisonError(
                    f"Reality returned multiple {network} deployments for {symbol}"
                )
            resolved["reality"][symbol] = matches[0] if matches else None

    if "xstocks" in settings.providers:
        network = str(settings.provider_config["xstocks"]["network"])
        for symbol in settings.symbols:
            payload = read_json(asset_cache_path(settings, "xstocks", symbol))
            matches = [
                str(item["address"]).lower()
                for item in payload.get("deployments", [])
                if item.get("network") == network and item.get("address")
            ]
            if len(matches) > 1:
                raise ComparisonError(
                    f"xStocks returned multiple {network} deployments for {symbol}"
                )
            resolved["xstocks"][symbol] = matches[0] if matches else None

    if "robinhood" in settings.providers:
        chain_id = int(settings.provider_config["robinhood"]["chain_id"])
        payload = read_json(asset_cache_path(settings, "robinhood"))
        for symbol in settings.symbols:
            matches = [
                str(deployment["contractAddress"]).lower()
                for asset in payload.get("assets", [])
                if str(asset.get("tokenSymbol", "")).upper() == symbol
                for deployment in asset.get("deployments", [])
                if int(deployment.get("chainId", -1)) == chain_id
                and deployment.get("contractAddress")
            ]
            if len(matches) > 1:
                raise ComparisonError(
                    f"Robinhood returned multiple chain {chain_id} deployments "
                    f"for {symbol}"
                )
            resolved["robinhood"][symbol] = matches[0] if matches else None
    return resolved


def range_parameters(settings: Settings) -> tuple[str, str]:
    start = datetime.combine(settings.from_date, time.min, tzinfo=settings.timezone)
    end = datetime.combine(settings.to_date, time.max, tzinfo=settings.timezone)
    return start.isoformat(), end.isoformat()


def collect_logs(
    settings: Settings,
    assets: Mapping[str, Mapping[str, str | None]],
) -> None:
    api_key = require_env("BLOCKSCOUT_API_KEY")
    from_period, to_period = range_parameters(settings)

    for provider in settings.providers:
        config = settings.provider_config[provider]
        chain_id = int(config["chain_id"])
        targets: list[tuple[str, str]] = []
        if provider == "reality":
            targets.append(("oracle", str(config.get("log_address", REALITY_ORACLE))))
        else:
            targets.extend(
                (symbol, address)
                for symbol, address in assets[provider].items()
                if address is not None
            )
        for name, address in targets:
            url = settings.blockscout_url.format(chain_id=chain_id, address=address)
            response = http_get(
                url,
                params={
                    "from_period": from_period,
                    "to_period": to_period,
                    "apikey": api_key,
                },
                timeout=120,
            )
            header = next(csv.reader(response.text.splitlines()), [])
            required = {
                "TxHash",
                "Index",
                "BlockNumber",
                "ContractAddress",
                "Data",
                "FirstTopic",
            }
            if not required.issubset(header):
                raise ComparisonError(
                    f"Blockscout CSV for {provider}/{name} is missing "
                    f"{sorted(required - set(header))}"
                )
            atomic_write(log_cache_path(settings, provider, name), response.text)


def load_dividends(settings: Settings) -> dict[str, list[Dividend]]:
    result: dict[str, list[Dividend]] = {}
    for symbol in settings.symbols:
        path = alpha_cache_path(settings, symbol)
        try:
            handle = path.open(encoding="utf-8-sig", newline="")
        except FileNotFoundError as exc:
            raise ComparisonError(f"required cache file is missing: {path}") from exc
        with handle:
            rows = csv.DictReader(handle)
            if rows.fieldnames is None:
                raise ComparisonError(f"empty Alpha Vantage cache: {path}")
            dividends = []
            for row in rows:
                ex_date = parse_iso_date(
                    row.get("ex_dividend_date", ""), "ex_dividend_date"
                )
                payment = str(row.get("payment_date", "")).strip()
                if not (settings.from_date <= ex_date <= settings.to_date):
                    continue
                if not payment or payment.lower() in {"none", "null"}:
                    continue
                dividends.append(
                    Dividend(
                        symbol=symbol,
                        ex_date=ex_date,
                        declaration_date=str(row.get("declaration_date", "")),
                        record_date=str(row.get("record_date", "")),
                        payment_date=payment,
                        amount=str(row.get("amount", "")),
                    )
                )
        result[symbol] = sorted(dividends, key=lambda item: item.ex_date)
    return result
