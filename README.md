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

## Milestone 1 recorder
```
.venv/bin/python scripts/record.py games duke             # search open games
.venv/bin/python scripts/record.py record duke clemson    # record until Ctrl-C (--minutes N to stop)
.venv/bin/python scripts/replay.py data/recordings/<env>/<file>.jsonl.gz   # summary / --csv
```
Recordings hold every raw WS message with its local receive time, plus the NTP clock offset in the metadata.

## Milestone 2 engine (demo only)
```
.venv/bin/python scripts/trade.py <game words> --team <team> --size 1 --deadline 4000
#   u = ▲  d = ▼  f = FLATTEN  s = status  q = quit
.venv/bin/python scripts/trade.py <game words> --team <team> --size 1 --deadline 4000 --auto
```
The state machine is in `src/courtside/engine.py`, and its tests (every §6 transition and edge case) are in `tests/test_engine.py`.

## Milestone 3 phone app
```
.venv/bin/python scripts/serve.py --gen-token     # put the output in .env as COURTSIDE_TOKEN
.venv/bin/python scripts/serve.py --host 0.0.0.0  # then open http://<laptop-ip>:8000/#token=<token> on the phone
```
Use Share → Add to Home Screen to install it. New games start in PAPER (▲/▼ = MARK). LIVE needs a confirm and is enabled on demo only.
