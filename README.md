# stadium-latency (Courtside)

Latency trading tool for Kalshi sports win markets. See `CLAUDE.md` for the design.

## Setup
```
/opt/homebrew/bin/python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env        # add the demo key id and path to the private key
.venv/bin/pytest -q
```

## Milestone 0 probe
```
.venv/bin/python scripts/probe.py              # demo: read-only checks + 1-contract order checks
.venv/bin/python scripts/probe.py --readonly   # safe on prod (KALSHI_ENV=prod)
```
Reports are written to `data/probe/`.
