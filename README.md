# stadium-latency (Courtside)

Latency trading tool for Kalshi sports win markets. See `CLAUDE.md` for the design.

## Setup
```
# The venv lives off ~/Desktop: macOS hides .pth files there (CLAUDE.md §11).
/opt/homebrew/bin/python3.11 -m venv ~/.venvs/courtside && ln -s ~/.venvs/courtside .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env        # demo key id + path to the PEM in secrets/
.venv/bin/pytest -q
```

## Milestone 0 probe
```
.venv/bin/python scripts/probe.py --ticker <market>   # demo: read-only + 1-contract order checks
.venv/bin/python scripts/probe.py --readonly          # safe on prod (KALSHI_ENV=prod)
```
Reports are written to `data/probe/` (gitignored). Findings are summarized in CLAUDE.md §11.
