from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

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
from dividends_comparison.collection import (
    collect_logs,
    event_topics,
    load_dividends,
    resolve_assets,
)
from dividends_comparison.config import load_settings
from dividends_comparison.contracts import REALITY_ORACLE, abi_entry, load_abi
from dividends_comparison.enrichment import RpcEnricher, latest_block_before
from dividends_comparison.events import (
    blockscout_rows,
    decode_reality,
    decode_robinhood,
    decode_xstocks,
    scale_uint,
    timestamp_fields,
)
from dividends_comparison.models import ComparisonError, Dividend, Settings
from dividends_comparison.reporting import (
    build_nasdaq_rows,
    build_provider_rows,
    output_path,
)

REALITY_EVENT_ABI = abi_entry(load_abi("reality.json"), "ActionExecuted")
XSTOCKS_SCHEDULED_ABI = abi_entry(load_abi("xstocks.json"), "MultiplierScheduled")
XSTOCKS_OVERRIDDEN_ABI = abi_entry(
    load_abi("xstocks.json"), "MultiplierScheduleOverridden"
)
ROBINHOOD_EVENT_ABI = abi_entry(load_abi("robinhood.json"), "UIMultiplierUpdated")


def settings_for(root: Path, providers=("reality",)) -> Settings:
    provider_config = {
        "reality": {
            "chain_id": 42161,
            "network": "ARB",
            "rpc_url_env": "ARBITRUM_RPC_URL",
            "asset_url": "https://example.invalid/reality",
            "log_address": REALITY_ORACLE,
        },
        "xstocks": {
            "chain_id": 1,
            "network": "Ethereum",
            "rpc_url_env": "ETHEREUM_RPC_URL",
            "asset_url": "https://example.invalid/{symbol}x",
        },
        "robinhood": {
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
        data_dir=root / "data",
        output_dir=root / "output",
        alpha_vantage_url="https://example.invalid/alpha",
        blockscout_url="https://example.invalid/v2/api",
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
    timestamp: int = 1782860000,
) -> str:
    fields = [
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
    ]
    row = {
        "TxHash": tx_hash,
        "Index": index,
        "BlockNumber": block,
        "BlockHash": "0x" + "ab" * 32,
        "Timestamp": timestamp,
        "ContractAddress": contract,
        "Data": data,
    }
    for name, topic in zip(
        ("FirstTopic", "SecondTopic", "ThirdTopic", "FourthTopic"),
        topics,
    ):
        row[name] = topic
    import io

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerow(row)
    return output.getvalue()


class ConfigurationTests(unittest.TestCase):
    def test_project_config_has_the_supported_providers(self):
        settings = load_settings(Path("config.toml"))
        self.assertEqual(settings.symbols[0], "GOOGL")
        self.assertEqual(settings.providers, ("reality", "xstocks", "robinhood"))
        self.assertEqual(settings.from_date, date(2026, 7, 1))
        self.assertEqual(settings.to_date, date(2026, 9, 30))

    def test_cli_collection_stages_are_independent(self):
        parser = app.build_parser()
        cached = parser.parse_args([])
        self.assertFalse(cached.collect_api_data)
        self.assertFalse(cached.collect_logs)
        both = parser.parse_args(["--collect-api-data", "--collect-logs"])
        self.assertTrue(both.collect_api_data)
        self.assertTrue(both.collect_logs)


class DataTests(unittest.TestCase):
    def test_http_error_includes_encoded_query_parameters(self):
        with patch.object(
            collection.requests,
            "get",
            side_effect=collection.requests.ConnectionError("offline"),
        ):
            with self.assertRaises(ComparisonError) as raised:
                collection.http_get(
                    "https://example.invalid/query",
                    params={"function": "DIVIDENDS", "symbol": "AAPL"},
                )
        self.assertIn(
            "https://example.invalid/query?function=DIVIDENDS&symbol=AAPL",
            str(raised.exception),
        )

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
        topic = event_topics("reality")[0]

        class Response:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "status": "1",
                    "message": "OK",
                    "result": [
                        {
                            "address": REALITY_ORACLE,
                            "topics": [topic],
                            "data": "0x",
                            "blockNumber": "0x64",
                            "blockHash": "0x" + "ab" * 32,
                            "timeStamp": "0x6a6e7100",
                            "logIndex": "0x3",
                            "transactionHash": "0x" + "56" * 32,
                        }
                    ],
                }

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
            self.assertEqual(len(calls), 1)
            for _url, request in calls:
                params = request["params"]
                self.assertEqual(params["apikey"], "blockscout-key")
                self.assertEqual(params["chain_id"], 42161)
                self.assertEqual(params["module"], "logs")
                self.assertEqual(params["action"], "getLogs")
                self.assertEqual(params["topic0"], topic)
                self.assertEqual(params["fromBlock"], 0)
                self.assertEqual(params["toBlock"], "latest")
                self.assertNotIn("headers", request)
            with log_cache_path(settings, "reality", "oracle").open(
                newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["BlockNumber"], "100")
            self.assertEqual(rows[0]["Index"], "3")
            self.assertEqual(rows[0]["Timestamp"], "1785622784")


class DecodeTests(unittest.TestCase):
    def test_outdated_log_cache_requires_recollection(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.csv"
            path.write_text(
                "TxHash,Index,BlockNumber,BlockHash,ContractAddress,Data,"
                "FirstTopic,SecondTopic,ThirdTopic,FourthTopic\n"
            )
            with self.assertRaisesRegex(
                ComparisonError,
                "outdated Blockscout cache.*recollect logs",
            ):
                blockscout_rows(path)

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
            self.assertEqual(decoded[0]["effective_at_iso"], "2026-07-01T00:00:00Z")
            self.assertEqual(decoded[0]["contract_address"], token)
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


class ReportingTests(unittest.TestCase):
    def test_timestamp_uses_utc_calendar_date(self):
        instant, day = timestamp_fields(1782871200)
        self.assertEqual(instant, "2026-07-01T02:00:00Z")
        self.assertEqual(day, "2026-07-01")

    def test_nasdaq_and_provider_rows_are_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(Path(temporary), ("xstocks",))
            dividend = Dividend(
                "AAPL", date(2026, 7, 1), "", "", "2026-07-10", "0.25"
            )
            event = {
                "symbol": "AAPL",
                "contract_address": "0x" + "11" * 20,
                "event_name": "event",
                "effective_timestamp": 1782864000,
                "effective_at_iso": "2026-07-01T00:00:00Z",
                "effective_date": "2026-07-01",
                "block_number": 1,
                "transaction_hash": "0x" + "22" * 32,
                "payment_per_share": "",
                "payout_token": "",
                "previous_multiplier": "1",
                "proposed_multiplier": "1.1",
                "log_index": 0,
            }

            class Enricher:
                def get(self, provider, symbol, event):
                    return {
                        "block_timestamp": 1782860000,
                        "block_at_iso": "2026-06-30T22:53:20Z",
                        "total_supply": "10",
                        "total_supply_snapshot_block": 9,
                    }

            nasdaq_rows = build_nasdaq_rows(
                settings,
                {"AAPL": [dividend]},
            )
            provider_rows = build_provider_rows(
                settings,
                "xstocks",
                {"AAPL": [event]},
                Enricher(),
            )
            self.assertEqual(nasdaq_rows[0]["ex_dividend_date"], "2026-07-01")
            self.assertNotIn("event_name", nasdaq_rows[0])
            self.assertEqual(provider_rows[0]["event_name"], "event")
            self.assertNotIn("ex_dividend_date", provider_rows[0])


class RpcTests(unittest.TestCase):
    def test_outdated_rpc_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(Path(temporary))
            write_json(
                rpc_cache_path(settings),
                {
                    "v2|reality|AAPL|4|0|40": {
                        "block_date": "1970-01-01T00:00:50Z",
                        "total_supply": "1.25",
                        "total_supply_snapshot_block": 2,
                    }
                },
            )
            enricher = RpcEnricher(settings, collect=False)
            with self.assertRaisesRegex(
                ComparisonError,
                r"required RPC cache entry is missing: v3\|",
            ):
                enricher.get(
                    "reality",
                    "AAPL",
                    {
                        "block_number": 4,
                        "log_index": 0,
                        "effective_timestamp": 40,
                    },
                )

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
                    "block_timestamp": 50,
                    "block_at_iso": "1970-01-01T00:00:50Z",
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
                "TxHash,Index,BlockNumber,BlockHash,Timestamp,ContractAddress,"
                "Data,FirstTopic,SecondTopic,ThirdTopic,FourthTopic\n",
            )
            write_json(rpc_cache_path(settings), {})
            with patch.object(
                collection.requests,
                "get",
                side_effect=AssertionError("network attempted"),
            ):
                self.assertEqual(app.run(settings), 0)
            with output_path(settings, "nasdaq").open(newline="") as handle:
                nasdaq_rows = list(csv.DictReader(handle))
            with output_path(settings, "reality").open(newline="") as handle:
                reality_rows = list(csv.DictReader(handle))
            self.assertEqual(nasdaq_rows[0]["symbol"], "AAPL")
            self.assertEqual(reality_rows, [])


if __name__ == "__main__":
    unittest.main()
