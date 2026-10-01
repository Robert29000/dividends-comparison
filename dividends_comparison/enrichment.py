"""Historical RPC lookups for event blocks and supply snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from web3 import Web3

from .cache import read_json, rpc_cache_path, write_json
from .config import require_env
from .contracts import load_abi
from .events import scale_uint
from .models import ComparisonError, Settings


def enrichment_key(
    provider: str,
    symbol: str,
    event: Mapping[str, Any],
) -> str:
    # v2 prevents reuse of the old ex-date-midnight supply snapshots.
    return "|".join(
        (
            "v2",
            provider,
            symbol,
            str(event["block_number"]),
            str(event["log_index"]),
            str(event["effective_timestamp"]),
        )
    )


def latest_block_before(web3: Web3, timestamp: int) -> int:
    """Find the greatest block whose timestamp is strictly below ``timestamp``.

    If the effective timestamp is still in the future, the current chain head is
    returned. Equal-timestamp blocks are deliberately excluded so the supply is
    the state immediately before activation.
    """
    head = int(web3.eth.block_number)
    genesis_timestamp = int(web3.eth.get_block(0)["timestamp"])
    if genesis_timestamp >= timestamp:
        raise ComparisonError("target supply snapshot predates chain genesis")
    if int(web3.eth.get_block(head)["timestamp"]) < timestamp:
        return head

    low, high = 0, head
    while low + 1 < high:
        midpoint = (low + high) // 2
        if int(web3.eth.get_block(midpoint)["timestamp"]) < timestamp:
            low = midpoint
        else:
            high = midpoint
    return low


class RpcEnricher:
    def __init__(self, settings: Settings, collect: bool) -> None:
        self.settings = settings
        self.collect = collect
        self.path = rpc_cache_path(settings)
        self.records: dict[str, dict[str, Any]] = (
            {} if collect else read_json(self.path)
        )
        self.clients: dict[str, Web3] = {}
        self.snapshot_blocks: dict[tuple[str, int], int] = {}

    def _client(self, provider: str) -> Web3:
        if provider not in self.clients:
            config = self.settings.provider_config[provider]
            env_name = str(config["rpc_url_env"])
            client = Web3(
                Web3.HTTPProvider(
                    require_env(env_name), request_kwargs={"timeout": 45}
                )
            )
            try:
                chain_id = int(client.eth.chain_id)
            except Exception as exc:
                raise ComparisonError(
                    f"{provider} RPC chain lookup failed: {exc}"
                ) from exc
            expected = int(config["chain_id"])
            if chain_id != expected:
                raise ComparisonError(
                    f"{provider} RPC chain ID is {chain_id}; expected {expected}"
                )
            self.clients[provider] = client
        return self.clients[provider]

    def get(
        self,
        provider: str,
        symbol: str,
        event: Mapping[str, Any],
    ) -> dict[str, Any]:
        key = enrichment_key(provider, symbol, event)
        if key in self.records:
            return self.records[key]
        if not self.collect:
            raise ComparisonError(f"required RPC cache entry is missing: {key}")

        client = self._client(provider)
        try:
            block = client.eth.get_block(int(event["block_number"]))
            block_date = datetime.fromtimestamp(
                int(block["timestamp"]), tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")

            cutoff_timestamp = int(event["effective_timestamp"])
            snapshot_cache_key = (provider, cutoff_timestamp)
            if snapshot_cache_key not in self.snapshot_blocks:
                self.snapshot_blocks[snapshot_cache_key] = latest_block_before(
                    client, cutoff_timestamp
                )
            snapshot_block = self.snapshot_blocks[snapshot_cache_key]

            abi = load_abi("xstocks.json" if provider == "xstocks" else "erc20.json")
            contract = client.eth.contract(
                address=Web3.to_checksum_address(str(event["contract_address"])),
                abi=abi,
            )
            decimals = int(
                contract.functions.decimals().call(block_identifier=snapshot_block)
            )
            supply = int(
                contract.functions.totalSupply().call(
                    block_identifier=snapshot_block
                )
            )
            record: dict[str, Any] = {
                "block_date": block_date,
                "total_supply": scale_uint(supply, decimals),
                "total_supply_snapshot_block": snapshot_block,
            }
            if provider == "xstocks":
                previous_block = int(event["block_number"]) - 1
                if previous_block < 0:
                    raise ComparisonError("xStocks event cannot be in block zero")
                previous = int(
                    contract.functions.multiplier().call(
                        block_identifier=previous_block
                    )
                )
                record["previous_multiplier"] = scale_uint(previous)
        except ComparisonError:
            raise
        except Exception as exc:
            raise ComparisonError(
                f"historical RPC enrichment failed for {provider}/{symbol} "
                f"at block {event['block_number']}: {exc}"
            ) from exc
        self.records[key] = record
        write_json(self.path, self.records)
        return record

    def save(self) -> None:
        if self.collect:
            write_json(self.path, self.records)
