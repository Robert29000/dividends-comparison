# Dividend comparison

A small Python CLI that connects ordinary-stock dividend events from Alpha
Vantage to corporate-action events emitted by Reality Finance, xStocks, and
Robinhood Stock Tokens.

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

Collect fresh API/log/RPC data and create the report:

```sh
compare-dividends --config config.toml --collect
```

Rebuild the report without making any network calls:

```sh
compare-dividends --config config.toml --use-existing
```

`--collect` replaces cache files for the configured date range. Raw Alpha
Vantage CSV files are stored in `data/nasdaq/`, raw Blockscout files in
`data/logs/`, provider metadata in `data/assets/`, and historical RPC results
in `data/rpc/`. The final wide table is
`output/dividend_comparison.csv`.

For every matched provider event, the report finds the latest block whose
timestamp is strictly earlier than the event's on-chain effective/activation
timestamp. It stores that block in `*_total_supply_snapshot_block` and reads
`totalSupply()` at that historical block into `*_total_supply`. If activation
is still in the future, the current chain head and current supply are used.
Archive-capable RPC endpoints are therefore required for past events.

Dates are inclusive. Provider events match when their effective/activation
date, interpreted in the configured timezone, equals the ex-dividend date. If
a provider has several matching events, the output contains every
cross-provider candidate combination.

The implementation is divided into logical modules under
`dividends_comparison/`: configuration, collection, event decoding, RPC
enrichment, reporting, and CLI orchestration. Contract ABIs are standard JSON
files under `abis/`.

## Test

```sh
python -m unittest discover -v
```
