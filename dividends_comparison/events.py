"""Blockscout log parsing and provider event decoding."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from hexbytes import HexBytes
from web3 import Web3

from .contracts import (
    DIVIDEND_ACTION_TYPE,
    EXECUTED_STATUS,
    REALITY_ORACLE,
    abi_entry,
    load_abi,
)
from .cache import log_cache_path
from .models import ComparisonError, Settings


def blockscout_rows(path: Path) -> list[dict[str, Any]]:
    try:
        handle = path.open(encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise ComparisonError(f"required cache file is missing: {path}") from exc
    result = []
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ComparisonError(f"empty Blockscout cache: {path}")
        for row in reader:
            if not row.get("TxHash"):
                continue
            topics = [
                HexBytes(row[name])
                for name in ("FirstTopic", "SecondTopic", "ThirdTopic", "FourthTopic")
                if row.get(name)
            ]
            try:
                result.append(
                    {
                        "address": Web3.to_checksum_address(row["ContractAddress"]),
                        "blockNumber": int(row["BlockNumber"]),
                        "transactionHash": HexBytes(row["TxHash"]),
                        "logIndex": int(row["Index"]),
                        "data": HexBytes(row.get("Data") or "0x"),
                        "topics": topics,
                        "blockHash": HexBytes(
                            row.get("BlockHash") or "0x" + "00" * 32
                        ),
                        "transactionIndex": 0,
                        "removed": False,
                    }
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ComparisonError(
                    f"invalid Blockscout row in {path}: {exc}"
                ) from exc
    return sorted(result, key=lambda row: (row["blockNumber"], row["logIndex"]))


def scale_uint(value: int, decimals: int = 18) -> str:
    if decimals < 0:
        raise ValueError("decimals must not be negative")
    if decimals == 0:
        return str(value)
    base = 10**decimals
    whole, fraction = divmod(value, base)
    if not fraction:
        return str(whole)
    return f"{whole}.{fraction:0{decimals}d}".rstrip("0")


def timestamp_fields(
    timestamp: int, configured_timezone: ZoneInfo
) -> tuple[str, str]:
    instant = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return (
        instant.isoformat().replace("+00:00", "Z"),
        instant.astimezone(configured_timezone).date().isoformat(),
    )


def event_decoder(
    contract_abi: list[dict[str, Any]], event_name: str
) -> tuple[Any, str]:
    abi = abi_entry(contract_abi, event_name)
    web3 = Web3()
    contract = web3.eth.contract(abi=contract_abi)
    event = getattr(contract.events, event_name)()
    signature = f"{abi['name']}({','.join(item['type'] for item in abi['inputs'])})"
    return event, Web3.keccak(text=signature).to_0x_hex().lower()


def decode_reality(
    settings: Settings,
    assets: Mapping[str, str | None],
) -> dict[str, list[dict[str, Any]]]:
    result = {symbol: [] for symbol in settings.symbols}
    reverse = {address: symbol for symbol, address in assets.items() if address}
    event, topic = event_decoder(load_abi("reality.json"), "ActionExecuted")
    web3 = Web3()
    for log in blockscout_rows(log_cache_path(settings, "reality", "oracle")):
        if str(log["address"]).lower() != str(
            settings.provider_config["reality"].get("log_address", REALITY_ORACLE)
        ).lower():
            raise ComparisonError("Reality log cache contains a non-oracle emitter")
        if not log["topics"] or Web3.to_hex(log["topics"][0]).lower() != topic:
            continue
        try:
            decoded = event.process_log(log)
            args = decoded["args"]
            token = str(args["token"]).lower()
            if (
                int(args["actionType"]) != DIVIDEND_ACTION_TYPE
                or int(args["status"]) != EXECUTED_STATUS
            ):
                continue
            payload_token, payout_token, amount = web3.codec.decode(
                ["address", "address", "uint256"], bytes(args["payload"])
            )
            if str(payload_token).lower() != token:
                raise ComparisonError(
                    "Reality payload token differs from indexed token in "
                    f"{Web3.to_hex(log['transactionHash'])}"
                )
            symbol = reverse.get(token)
            if symbol is None:
                continue
            effective_timestamp = int(args["effectiveAt"])
            event_time, event_date = timestamp_fields(
                effective_timestamp, settings.timezone
            )
            result[symbol].append(
                {
                    "provider": "reality",
                    "symbol": symbol,
                    "contract_address": token,
                    "event_name": "ActionExecuted",
                    "execution_at": event_time,
                    "activation_time": "",
                    "effective_timestamp": effective_timestamp,
                    "event_date": event_date,
                    "block_number": int(log["blockNumber"]),
                    "log_index": int(log["logIndex"]),
                    "transaction_hash": Web3.to_hex(
                        log["transactionHash"]
                    ).lower(),
                    "payment_per_share": scale_uint(int(amount)),
                    "payout_token": str(payout_token).lower(),
                    "previous_multiplier": "",
                    "proposed_multiplier": "",
                }
            )
        except ComparisonError:
            raise
        except Exception as exc:
            raise ComparisonError(f"cannot decode Reality log: {exc}") from exc
    return result


def decode_xstocks(
    settings: Settings,
    assets: Mapping[str, str | None],
) -> dict[str, list[dict[str, Any]]]:
    result = {symbol: [] for symbol in settings.symbols}
    abi = load_abi("xstocks.json")
    scheduled_event, scheduled_topic = event_decoder(abi, "MultiplierScheduled")
    overridden_event, overridden_topic = event_decoder(
        abi, "MultiplierScheduleOverridden"
    )
    for symbol, address in assets.items():
        if address is None:
            continue
        schedules: list[dict[str, Any]] = []
        path = log_cache_path(settings, "xstocks", symbol)
        for log in blockscout_rows(path):
            if str(log["address"]).lower() != address.lower():
                raise ComparisonError(
                    f"xStocks log cache for {symbol} contains a different emitter"
                )
            if not log["topics"]:
                continue
            topic = Web3.to_hex(log["topics"][0]).lower()
            try:
                if topic == scheduled_topic:
                    args = scheduled_event.process_log(log)["args"]
                    activation = int(args["activationTime"])
                    activation_time, event_date = timestamp_fields(
                        activation, settings.timezone
                    )
                    schedules.append(
                        {
                            "provider": "xstocks",
                            "symbol": symbol,
                            "contract_address": address,
                            "event_name": "MultiplierScheduled",
                            "execution_at": "",
                            "activation_time": activation_time,
                            "effective_timestamp": activation,
                            "event_date": event_date,
                            "block_number": int(log["blockNumber"]),
                            "log_index": int(log["logIndex"]),
                            "transaction_hash": Web3.to_hex(
                                log["transactionHash"]
                            ).lower(),
                            "payment_per_share": "",
                            "payout_token": "",
                            "previous_multiplier": "",
                            "proposed_multiplier": scale_uint(
                                int(args["newMultiplier"])
                            ),
                            "_proposed_raw": int(args["newMultiplier"]),
                            "_activation_raw": activation,
                            "_overridden": False,
                        }
                    )
                elif topic == overridden_topic:
                    args = overridden_event.process_log(log)["args"]
                    target = (
                        int(args["overriddenMultiplier"]),
                        int(args["overriddenActivationTime"]),
                    )
                    for candidate in reversed(schedules):
                        if not candidate["_overridden"] and (
                            candidate["_proposed_raw"],
                            candidate["_activation_raw"],
                        ) == target:
                            candidate["_overridden"] = True
                            break
            except Exception as exc:
                raise ComparisonError(
                    f"cannot decode xStocks log for {symbol}: {exc}"
                ) from exc
        result[symbol] = [item for item in schedules if not item["_overridden"]]
    return result


def decode_robinhood(
    settings: Settings,
    assets: Mapping[str, str | None],
) -> dict[str, list[dict[str, Any]]]:
    result = {symbol: [] for symbol in settings.symbols}
    event, expected_topic = event_decoder(
        load_abi("robinhood.json"), "UIMultiplierUpdated"
    )
    for symbol, address in assets.items():
        if address is None:
            continue
        for log in blockscout_rows(log_cache_path(settings, "robinhood", symbol)):
            if str(log["address"]).lower() != address.lower():
                raise ComparisonError(
                    f"Robinhood log cache for {symbol} contains a different emitter"
                )
            if (
                not log["topics"]
                or Web3.to_hex(log["topics"][0]).lower() != expected_topic
            ):
                continue
            try:
                args = event.process_log(log)["args"]
                activation = int(args["effectiveAtTimestamp"])
                activation_time, event_date = timestamp_fields(
                    activation, settings.timezone
                )
                result[symbol].append(
                    {
                        "provider": "robinhood",
                        "symbol": symbol,
                        "contract_address": address,
                        "event_name": "UIMultiplierUpdated",
                        "execution_at": "",
                        "activation_time": activation_time,
                        "effective_timestamp": activation,
                        "event_date": event_date,
                        "block_number": int(log["blockNumber"]),
                        "log_index": int(log["logIndex"]),
                        "transaction_hash": Web3.to_hex(
                            log["transactionHash"]
                        ).lower(),
                        "payment_per_share": "",
                        "payout_token": "",
                        "previous_multiplier": scale_uint(
                            int(args["oldMultiplier"])
                        ),
                        "proposed_multiplier": scale_uint(
                            int(args["newMultiplier"])
                        ),
                    }
                )
            except Exception as exc:
                raise ComparisonError(
                    f"cannot decode Robinhood log for {symbol}: {exc}"
                ) from exc
    return result


def decode_all_events(
    settings: Settings,
    assets: Mapping[str, Mapping[str, str | None]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    events: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if "reality" in settings.providers:
        events["reality"] = decode_reality(settings, assets["reality"])
    if "xstocks" in settings.providers:
        events["xstocks"] = decode_xstocks(settings, assets["xstocks"])
    if "robinhood" in settings.providers:
        events["robinhood"] = decode_robinhood(settings, assets["robinhood"])
    for provider_events in events.values():
        for symbol_events in provider_events.values():
            symbol_events.sort(
                key=lambda item: (item["block_number"], item["log_index"])
            )
    return events
