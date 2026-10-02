"""HTTP collection, asset resolution, and dividend input loading."""

from __future__ import annotations

import csv
import io
import json
from time import sleep
from typing import Any, Mapping

import requests
from web3 import Web3

from .cache import (
    alpha_cache_path,
    asset_cache_path,
    atomic_write,
    log_cache_path,
    read_json,
    write_json,
)
from .models import ComparisonError, Dividend, Settings
from .utils import (
    REALITY_ORACLE,
    abi_entry,
    load_abi,
    parse_iso_date,
    require_env,
)


def http_get(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    timeout: int = 60,
    allow_not_found: bool = False,
    retries: int = 0,
) -> requests.Response:
    request_url = requests.Request("GET", url, params=params).prepare().url or url
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            if response.status_code == 429 and attempt < retries:
                try:
                    delay = float(response.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    delay = min(2**attempt, 10)
                sleep(max(delay, 1))
                continue
            if allow_not_found and response.status_code == 404:
                return response
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if exc.request is not None and exc.request.url:
                request_url = exc.request.url
            raise ComparisonError(f"GET {request_url} failed: {exc}") from exc
    raise AssertionError("unreachable")


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


def event_topics(provider: str) -> tuple[str, ...]:
    definitions = {
        "reality": ("reality.json", ("ActionExecuted",)),
        "xstocks": (
            "xstocks.json",
            ("MultiplierScheduled", "MultiplierScheduleOverridden"),
        ),
        "robinhood": ("robinhood.json", ("UIMultiplierUpdated",)),
    }
    filename, event_names = definitions[provider]
    abi = load_abi(filename)
    topics = []
    for event_name in event_names:
        event = abi_entry(abi, event_name)
        signature = f"{event_name}({','.join(item['type'] for item in event['inputs'])})"
        topics.append(Web3.keccak(text=signature).to_0x_hex().lower())
    return tuple(topics)


def blockscout_log_rows(
    url: str,
    chain_id: int,
    address: str,
    api_key: str,
    topic: str,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "chain_id": chain_id,
        "module": "logs",
        "action": "getLogs",
        "address": address,
        "topic0": topic,
        "fromBlock": 0,
        "toBlock": "latest",
        "apikey": api_key,
    }
    response = http_get(url, params=params, timeout=120, retries=5)
    try:
        payload = response.json()
    except requests.JSONDecodeError as exc:
        raise ComparisonError(f"Blockscout returned invalid log JSON: {url}") from exc
    items = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        message = payload.get("message", payload) if isinstance(payload, dict) else payload
        raise ComparisonError(f"Blockscout log response is invalid: {message}")

    rows = []
    for item in items:
        try:
            topics = list(item["topics"])
            block_timestamp = int(str(item["timeStamp"]), 0)
            row = {
                "SchemaVersion": 2,
                "TxHash": item["transactionHash"],
                "Index": int(str(item["logIndex"]), 0),
                "BlockNumber": int(str(item["blockNumber"]), 0),
                "BlockHash": item.get("blockHash", ""),
                "Timestamp": block_timestamp,
                "ContractAddress": item["address"],
                "Data": item["data"],
                "FirstTopic": topics[0] if topics else "",
                "SecondTopic": topics[1] if len(topics) > 1 else "",
                "ThirdTopic": topics[2] if len(topics) > 2 else "",
                "FourthTopic": topics[3] if len(topics) > 3 else "",
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ComparisonError(f"invalid Blockscout log item: {exc}") from exc
        rows.append(row)
    return rows


def collect_logs(
    settings: Settings,
    assets: Mapping[str, Mapping[str, str | None]],
) -> None:
    api_key = require_env("BLOCKSCOUT_API_KEY")
    fieldnames = (
        "SchemaVersion",
        "TxHash",
        "Index",
        "BlockNumber",
        "BlockHash",
        "Timestamp",
        "ContractAddress",
        "Data",
        "FirstTopic",
        "SecondTopic",
        "ThirdTopic",
        "FourthTopic",
    )

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
            url = settings.blockscout_url
            collected = []
            for topic in event_topics(provider):
                collected.extend(
                    blockscout_log_rows(
                        url,
                        chain_id,
                        address,
                        api_key,
                        topic,
                    )
                )
                sleep(0.5)
            unique = {
                (str(row["TxHash"]).lower(), int(row["Index"])): row
                for row in collected
            }
            ordered = sorted(
                unique.values(),
                key=lambda row: (int(row["BlockNumber"]), int(row["Index"])),
            )
            buffer = io.StringIO(newline="")
            writer = csv.DictWriter(buffer, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(ordered)
            atomic_write(log_cache_path(settings, provider, name), buffer.getvalue())


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
