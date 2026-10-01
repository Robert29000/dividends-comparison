from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from web3 import Web3

from dividends_comparison import app, collection
from dividends_comparison.cache import (
    alpha_cache_path,
    asset_cache_path,
    atomic_write,
    log_cache_path,
    rpc_cache_path,
    write_json,
)
from dividends_comparison.collection import collect_logs, load_dividends, resolve_assets
from dividends_comparison.config import load_settings
from dividends_comparison.contracts import REALITY_ORACLE, abi_entry, load_abi
from dividends_comparison.enrichment import RpcEnricher, latest_block_before
from dividends_comparison.events import (
    decode_reality,
    decode_robinhood,
    decode_xstocks,
    scale_uint,
    timestamp_fields,
)
from dividends_comparison.models import Dividend, Settings
from dividends_comparison.reporting import build_report_rows, matching_candidates

REALITY_EVENT_ABI = abi_entry(load_abi("reality.json"), "ActionExecuted")
XSTOCKS_SCHEDULED_ABI = abi_entry(load_abi("xstocks.json"), "MultiplierScheduled")
XSTOCKS_OVERRIDDEN_ABI = abi_entry(
    load_abi("xstocks.json"), "MultiplierScheduleOverridden"
)
ROBINHOOD_EVENT_ABI = abi_entry(load_abi("robinhood.json"), "UIMultiplierUpdated")


def settings_for(root: Path, providers=("reality",)) -> Settings:
    provider_config = {
        "reality": {
            "enabled": True,
            "chain_id": 42161,
            "network": "ARB",
            "rpc_url_env": "ARBITRUM_RPC_URL",
            "asset_url": "https://example.invalid/reality",
            "log_address": REALITY_ORACLE,
        },
        "xstocks": {
            "enabled": True,
            "chain_id": 1,
            "network": "Ethereum",
            "rpc_url_env": "ETHEREUM_RPC_URL",
            "asset_url": "https://example.invalid/{symbol}x",
        },
        "robinhood": {
            "enabled": True,
            "chain_id": 4663,
            "network": "Robinhood Chain",
            "rpc_url_env": "ROBINHOOD_RPC_URL",
            "asset_url": "https://example.invalid/assets",
        },
    }
    return Settings(
        symbols=("AAPL",),
        providers=tuple(providers),
        from_date=date(2026, 7, 1),
        to_date=date(2026, 9, 30),
        timezone=ZoneInfo("America/New_York"),
        data_dir=root / "data",
        output_path=root / "output" / "report.csv",
        alpha_vantage_url="https://example.invalid/alpha",
        blockscout_url="https://example.invalid/{chain_id}/{address}",
        provider_config=provider_config,
    )


def event_topic(abi):
    signature = f"{abi['name']}({','.join(item['type'] for item in abi['inputs'])})"
    return Web3.keccak(text=signature).to_0x_hex()


def padded_address(address: str) -> str:
    return "0x" + "00" * 12 + address.removeprefix("0x").lower()


def csv_log(
    *,
    contract: str,
    block: int,
    index: int,
    tx_hash: str,
    data: str,
    topics: list[str],
) -> str:
    fields = [
        "TxHash",
        "Index",
        "BlockNumber",
        "BlockHash",
        "ContractAddress",
        "Data",
        "FirstTopic",
        "SecondTopic",
        "ThirdTopic",
        "FourthTopic",
    ]
    row = {
        "TxHash": tx_hash,
        "Index": index,
        "BlockNumber": block,
        "BlockHash": "0x" + "ab" * 32,
        "ContractAddress": contract,
        "Data": data,
    }
    for name, topic in zip(fields[6:], topics):
        row[name] = topic
    import io

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerow(row)
    return output.getvalue()


class ConfigurationTests(unittest.TestCase):
    def test_project_config_is_valid_and_ondo_is_disabled(self):
        settings = load_settings(Path("config.toml"))
        self.assertEqual(settings.symbols[0], "GOOGL")
        self.assertEqual(settings.providers, ("reality", "xstocks", "robinhood"))
        self.assertEqual(settings.from_date, date(2026, 7, 1))
        self.assertEqual(settings.to_date, date(2026, 9, 30))

    def test_cli_modes_are_mutually_exclusive_and_required(self):
        parser = app.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--collect", "--use-existing"])


class DataTests(unittest.TestCase):
    def test_dividend_filter_is_inclusive_and_requires_payment_date(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(Path(temporary))
            path = alpha_cache_path(settings, "AAPL")
            path.parent.mkdir(parents=True)
            path.write_text(
                "ex_dividend_date,declaration_date,record_date,payment_date,amount\n"
                "2026-07-01,2026-06-01,2026-07-01,2026-07-10,0.25\n"
                "2026-08-01,2026-07-01,2026-08-01,None,0.25\n"
                "2026-09-30,2026-09-01,2026-09-30,2026-10-10,0.30\n"
                "2026-10-01,2026-09-01,2026-10-01,2026-10-10,0.30\n"
            )
            rows = load_dividends(settings)["AAPL"]
            self.assertEqual([item.ex_date for item in rows], [date(2026, 7, 1), date(2026, 9, 30)])

    def test_asset_resolution_uses_configured_networks(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(Path(temporary), ("reality", "xstocks", "robinhood"))
            write_json(
                asset_cache_path(settings, "reality", "AAPL"),
                {"data": [{"ticker": "AAPL", "tokens": [{"network": "ARB", "address": "0x" + "11" * 20}]}]},
            )
            write_json(
                asset_cache_path(settings, "xstocks", "AAPL"),
                {"deployments": [{"network": "Ethereum", "address": "0x" + "22" * 20}]},
            )
            write_json(
                asset_cache_path(settings, "robinhood"),
                {"assets": [{"tokenSymbol": "AAPL", "deployments": [{"chainId": 4663, "contractAddress": "0x" + "33" * 20}]}]},
            )
            resolved = resolve_assets(settings)
            self.assertEqual(resolved["reality"]["AAPL"], "0x" + "11" * 20)
            self.assertEqual(resolved["xstocks"]["AAPL"], "0x" + "22" * 20)
            self.assertEqual(resolved["robinhood"]["AAPL"], "0x" + "33" * 20)

    def test_exact_decimal_scaling(self):
        self.assertEqual(scale_uint(1), "0.000000000000000001")
        self.assertEqual(scale_uint(10**18), "1")
        self.assertEqual(scale_uint(1_250_000, 6), "1.25")

    def test_blockscout_api_key_is_a_query_parameter(self):
        class Response:
            status_code = 200
            text = (
                "TxHash,Index,BlockNumber,ContractAddress,Data,FirstTopic\n"
            )

            def raise_for_status(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(Path(temporary))
            calls = []

            def fake_get(url, **kwargs):
                calls.append((url, kwargs))
                return Response()

            with (
                patch(
                    "dividends_comparison.collection.require_env",
                    return_value="blockscout-key",
                ) as require_env,
                patch(
                    "dividends_comparison.collection.requests.get",
                    side_effect=fake_get,
                ),
            ):
                collect_logs(
                    settings,
                    {"reality": {"AAPL": "0x" + "11" * 20}},
                )
            require_env.assert_called_once_with("BLOCKSCOUT_API_KEY")
            request = calls[0][1]
            self.assertEqual(request["params"]["apikey"], "blockscout-key")
            self.assertNotIn("headers", request)


class DecodeTests(unittest.TestCase):
    def test_reality_executed_dividend_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(Path(temporary))
            token = "0x" + "12" * 20
            payout = "0x" + "34" * 20
            web3 = Web3()
            nonindexed = [
                item["type"]
                for item in REALITY_EVENT_ABI["inputs"]
                if not item["indexed"]
            ]
            data = web3.codec.encode(
                nonindexed,
                [
                    b"r" * 32,
                    1782864000,  # 2026-07-01 00:00:00 UTC
                    b"d" * 32,
                    "Distribution",
                    2,
                    2,
                    web3.codec.encode(["address", "address", "uint256"], [token, payout, 25 * 10**16]),
                ],
            ).hex()
            contents = csv_log(
                contract=REALITY_ORACLE,
                block=100,
                index=3,
                tx_hash="0x" + "56" * 32,
                data="0x" + data,
                topics=[
                    event_topic(REALITY_EVENT_ABI),
                    "0x" + "78" * 32,
                    padded_address(token),
                ],
            )
            path = log_cache_path(settings, "reality", "oracle")
            atomic_write(path, contents)
            decoded = decode_reality(settings, {"AAPL": token})["AAPL"]
            self.assertEqual(len(decoded), 1)
            self.assertEqual(decoded[0]["effective_timestamp"], 1782864000)
            self.assertEqual(decoded[0]["payment_per_share"], "0.25")
            self.assertEqual(decoded[0]["payout_token"], payout)
            self.assertEqual(decoded[0]["event_name"], "ActionExecuted")

    def test_xstocks_override_removes_stale_schedule(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(Path(temporary), ("xstocks",))
            token = "0x" + "12" * 20
            web3 = Web3()
            scheduled_data = "0x" + web3.codec.encode(
                ["uint256", "uint256"], [11 * 10**17, 1782864000]
            ).hex()
            override_data = "0x" + web3.codec.encode(
                ["uint256", "uint256", "uint256", "uint256"],
                [11 * 10**17, 1782864000, 12 * 10**17, 1782950400],
            ).hex()
            first = csv_log(
                contract=token,
                block=100,
                index=1,
                tx_hash="0x" + "11" * 32,
                data=scheduled_data,
                topics=[event_topic(XSTOCKS_SCHEDULED_ABI)],
            )
            second_lines = csv_log(
                contract=token,
                block=101,
                index=2,
                tx_hash="0x" + "22" * 32,
                data=override_data,
                topics=[event_topic(XSTOCKS_OVERRIDDEN_ABI)],
            ).splitlines()
            path = log_cache_path(settings, "xstocks", "AAPL")
            atomic_write(path, first.rstrip() + "\n" + second_lines[1] + "\n")
            self.assertEqual(decode_xstocks(settings, {"AAPL": token})["AAPL"], [])

    def test_robinhood_multiplier_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(Path(temporary), ("robinhood",))
            token = "0x" + "12" * 20
            data = "0x" + Web3().codec.encode(
                ["uint256", "uint256", "uint256"],
                [10**18, 1005 * 10**15, 1782864000],
            ).hex()
            path = log_cache_path(settings, "robinhood", "AAPL")
            atomic_write(
                path,
                csv_log(
                    contract=token,
                    block=100,
                    index=1,
                    tx_hash="0x" + "11" * 32,
                    data=data,
                    topics=[event_topic(ROBINHOOD_EVENT_ABI)],
                ),
            )
            decoded = decode_robinhood(settings, {"AAPL": token})["AAPL"][0]
            self.assertEqual(decoded["effective_timestamp"], 1782864000)
            self.assertEqual(decoded["previous_multiplier"], "1")
            self.assertEqual(decoded["proposed_multiplier"], "1.005")


class MatchingTests(unittest.TestCase):
    def candidate(self, day: str, block: int = 1):
        return {"event_date": day, "block_number": block, "log_index": 0}

    def test_matching_requires_the_exact_event_date(self):
        candidates = [
            self.candidate("2026-07-08", 1),
            self.candidate("2026-07-09", 2),
            self.candidate("2026-07-10", 3),
            self.candidate("2026-07-11", 4),
            self.candidate("2026-07-12", 5),
        ]
        matched = matching_candidates(candidates, date(2026, 7, 10))
        self.assertEqual([item["block_number"] for item in matched], [3])
        self.assertEqual([item["candidate_index"] for item in matched], [1])

    def test_timestamp_uses_new_york_calendar_date(self):
        _instant, day = timestamp_fields(
            1782871200, ZoneInfo("America/New_York")
        )
        self.assertEqual(day, "2026-06-30")

    def test_multiple_provider_candidates_expand_cartesian_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(Path(temporary), ("xstocks", "robinhood"))
            dividend = Dividend(
                "AAPL", date(2026, 7, 1), "", "", "2026-07-10", "0.25"
            )
            base = {
                "symbol": "AAPL",
                "contract_address": "0x" + "11" * 20,
                "event_name": "event",
                "execution_at": "",
                "activation_time": "2026-07-01T00:00:00Z",
                "event_date": "2026-07-01",
                "transaction_hash": "0x" + "22" * 32,
                "payment_per_share": "",
                "payout_token": "",
                "previous_multiplier": "1",
                "proposed_multiplier": "1.1",
                "log_index": 0,
            }
            events = {}
            for provider in settings.providers:
                events[provider] = {"AAPL": [dict(base, provider=provider, block_number=1), dict(base, provider=provider, block_number=2)]}

            class Enricher:
                def get(self, provider, symbol, event):
                    return {"block_date": "2026-07-01T00:00:00Z", "total_supply": "10", "total_supply_snapshot_block": 9}

            rows = build_report_rows(
                settings,
                {"AAPL": [dividend]},
                {provider: {"AAPL": "0x" + "11" * 20} for provider in settings.providers},
                events,
                Enricher(),
            )
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row["xstocks_candidate_count"] == 2 for row in rows))
            self.assertTrue(all(row["robinhood_candidate_count"] == 2 for row in rows))


class RpcTests(unittest.TestCase):
    def test_latest_block_is_strictly_before_cutoff(self):
        class Eth:
            block_number = 4

            def get_block(self, number):
                return {"timestamp": [10, 20, 30, 40, 50][number]}

        class Client:
            eth = Eth()

        self.assertEqual(latest_block_before(Client(), 40), 2)
        self.assertEqual(latest_block_before(Client(), 41), 3)
        self.assertEqual(latest_block_before(Client(), 100), 4)

    def test_supply_snapshot_uses_event_timestamp_not_ex_date_midnight(self):
        class HistoricalCall:
            def __init__(self, value, calls):
                self.value = value
                self.calls = calls

            def call(self, *, block_identifier):
                self.calls.append(block_identifier)
                return self.value

        class Functions:
            def __init__(self, calls):
                self.calls = calls

            def decimals(self):
                return HistoricalCall(6, self.calls)

            def totalSupply(self):
                return HistoricalCall(1_250_000, self.calls)

        class Eth:
            block_number = 4

            def __init__(self):
                self.supply_calls = []

            def get_block(self, number):
                return {"timestamp": [10, 20, 30, 40, 50][number]}

            def contract(self, *, address, abi):
                return type(
                    "Contract", (), {"functions": Functions(self.supply_calls)}
                )()

        class Client:
            eth = Eth()

        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(Path(temporary))
            enricher = RpcEnricher(settings, collect=True)
            enricher._client = lambda provider: Client()
            result = enricher.get(
                "reality",
                "AAPL",
                {
                    "block_number": 4,
                    "log_index": 0,
                    "effective_timestamp": 40,
                    "contract_address": "0x" + "11" * 20,
                },
            )
            self.assertEqual(result["total_supply_snapshot_block"], 2)
            self.assertEqual(result["total_supply"], "1.25")
            self.assertEqual(Client.eth.supply_calls, [2, 2])


class OfflineModeTests(unittest.TestCase):
    def test_use_existing_builds_report_without_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(Path(temporary))
            write_json(
                asset_cache_path(settings, "reality", "AAPL"),
                {"data": [{"ticker": "AAPL", "tokens": [{"network": "ARB", "address": "0x" + "11" * 20}]}]},
            )
            alpha = alpha_cache_path(settings, "AAPL")
            atomic_write(
                alpha,
                "ex_dividend_date,declaration_date,record_date,payment_date,amount\n"
                "2026-07-01,2026-06-01,2026-07-01,2026-07-10,0.25\n",
            )
            atomic_write(
                log_cache_path(settings, "reality", "oracle"),
                "TxHash,Index,BlockNumber,BlockHash,ContractAddress,Data,FirstTopic,SecondTopic,ThirdTopic,FourthTopic\n",
            )
            write_json(rpc_cache_path(settings), {})
            with patch.object(
                collection.requests,
                "get",
                side_effect=AssertionError("network attempted"),
            ):
                self.assertEqual(app.run(settings, collect=False), 0)
            with settings.output_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["reality_match_status"], "no_match")


if __name__ == "__main__":
    unittest.main()
