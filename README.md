# Dividend data collection

A small Python CLI that collects ordinary-stock dividends from Alpha Vantage
and corporate-action events from Reality Finance, xStocks, and Robinhood Stock
Tokens. Each source is written independently; the tool does not match Nasdaq
dividends to blockchain events.

## Setup

Python 3.11 or newer and archive-capable RPC endpoints are required.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Fill `.env`, then load it into the shell. The Alpha Vantage `demo` key only
serves sample symbols and cannot collect the five symbols in `config.toml`.
Blockscout's unified API requires an API key, sent through its `apikey` query
parameter.

```sh
set -a
. ./.env
set +a
```

## Run

Collect fresh third-party API data, blockchain logs/RPC data, and create the
report:

```sh
compare-dividends --config config.toml --collect-api-data --collect-logs
```

The collection stages can also run independently. Both commands still process
the available caches and rebuild the report:

```sh
compare-dividends --config config.toml --collect-api-data
compare-dividends --config config.toml --collect-logs
```

With no collection flag, the report is rebuilt without network calls:

```sh
compare-dividends --config config.toml
```

Offline mode requires caches produced by the current schema. Log caches without
the raw `Timestamp` column and pre-`v3` RPC entries are rejected with an
instruction to rerun `--collect-logs`; incomplete legacy timestamps are never
written to output.

Collection replaces the corresponding cache files. Raw Alpha Vantage CSV files
are stored in `data/nasdaq/`, topic-filtered Blockscout log files in
`data/logs/`, provider metadata in `data/assets/`, and historical RPC results
in `data/rpc/`. The output directory contains four independent tables:

- `output/nasdaq.csv`
- `output/reality.csv`
- `output/xstocks.csv`
- `output/robinhood.csv`

Blockscout logs are fetched through its `module=logs&action=getLogs` API with
the contract address and `topic0` filters. Reality Finance collects only the
`ActionExecuted` event. The indexed rToken topic is decoded and checked against
both the event argument and the asset API address.

The configured date range applies to the UTC timestamp of the emitted log. For
every collected provider event, the tool finds the latest block whose
timestamp is strictly earlier than the event's on-chain effective/activation
timestamp. It stores that block in `total_supply_snapshot_block` and reads
`totalSupply()` at that historical block into `total_supply`. If activation
is still in the future, the current chain head and current supply are used.
Archive-capable RPC endpoints are therefore required for past events.

Dates are inclusive and on-chain timestamps are interpreted in UTC. Provider
tables include the raw Unix `effective_timestamp`, its readable
`effective_at_iso` value, and the effective UTC date. They also contain the
event block's raw timestamp and ISO value.

The implementation is divided into logical modules under
`dividends_comparison/`: configuration, collection, event decoding, RPC
enrichment, reporting, and CLI orchestration. Contract ABIs are standard JSON
files under `abis/`.

## Test

```sh
python -m unittest discover -v
```
