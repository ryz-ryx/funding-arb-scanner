"""
PAPER TRADING ONLY -- this simulates the funding-rate/basis-capture strategy
with a virtual $10,000 balance. It never places a real order, never touches
a real exchange account, and no API keys are used or required (it only
reads public market data, same as scanner/live_scanner.py).

Runs as a LONG-LIVED LOOP (not a one-shot), checking every CHECK_INTERVAL_SECONDS
and checkpointing (git commit + push) its state periodically, so progress
survives even if the job is killed. It exits cleanly before MAX_JOB_SECONDS to
stay under GitHub Actions' hard 6-hour job limit; the workflow's cron
(every 6h) starts the next job, so the only gap is the few minutes between
one job's graceful exit and the next one's start -- GitHub Actions has no way
to run a single job forever, this is the closest continuous coverage gets on
that platform. True zero-gap execution needs an always-on server instead.

Strategy simulated: delta-neutral (long spot + short perp, equal notional).
Enter when a symbol's trailing 3-day annualized funding rate clears
ENTRY_THRESHOLD_PCT; hold continuously (continuous hold beat timed entry in
every historical backtest, see ../funding_arb/RESULTS.md); exit if the
trailing rate falls below EXIT_THRESHOLD_PCT. Round-trip cost (fees +
slippage) is deducted at open and close, same 0.51% assumption as
funding_arb/backtest.py.

Run: python paper_trader/run.py
"""

import datetime
import json
import os
import subprocess
import time

import ccxt

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Lives under docs/ so GitHub Pages (source: main/docs) can serve it directly
# to the dashboard -- this file is both the bot's working state and the
# dashboard's data source, deliberately, to avoid keeping two copies in sync.
STATE_PATH = os.path.join(REPO_ROOT, "docs", "data", "portfolio.json")

STARTING_CAPITAL = 10_000.0
MAX_POSITIONS = 5
POSITION_SIZE_PCT = 0.20        # of starting capital, per position
ENTRY_THRESHOLD_PCT = 15.0      # trailing 3d annualized %, to open
EXIT_THRESHOLD_PCT = 3.0        # trailing 3d annualized %, to close
ROUND_TRIP_COST_PCT = 0.51      # from funding_arb/backtest.py cost model (% of notional, full open+close cycle)
MIN_24H_VOLUME_USDT = 2_000_000
CANDIDATE_POOL = 60
TRAILING_INTERVALS = 9          # 3 days at 8h funding interval

CHECK_INTERVAL_SECONDS = 5 * 60       # re-check every 5 minutes
COMMIT_INTERVAL_SECONDS = 30 * 60     # checkpoint to git every 30 minutes
MAX_JOB_SECONDS = 5 * 3600 + 50 * 60  # exit at 5h50m, under the 6h Actions cap


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {
        "starting_capital": STARTING_CAPITAL,
        "cash": STARTING_CAPITAL,
        "positions": {},
        "closed_trades": [],
        "history": [],
    }


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def git_checkpoint(message):
    """Best-effort commit + push of the state file. Failure here (e.g. a
    concurrent push) should never crash the loop -- just try again next cycle."""
    try:
        subprocess.run(["git", "add", "docs/data/portfolio.json"], cwd=REPO_ROOT, check=True)
        result = subprocess.run(
            ["git", "diff", "--staged", "--quiet"], cwd=REPO_ROOT
        )
        if result.returncode == 0:
            return  # nothing changed
        subprocess.run(["git", "commit", "-m", message], cwd=REPO_ROOT, check=True)
        subprocess.run(["git", "push"], cwd=REPO_ROOT, check=True)
    except subprocess.CalledProcessError as e:
        print(f"  (checkpoint commit/push failed, will retry next cycle: {e})")


def trailing_annualized(exchange, symbol):
    try:
        hist = exchange.fetch_funding_rate_history(symbol, limit=TRAILING_INTERVALS)
    except Exception:
        return None
    rates = [h["fundingRate"] for h in hist if h.get("fundingRate") is not None]
    if not rates:
        return None
    avg_rate = sum(rates) / len(rates)
    return avg_rate * 3 * 365 * 100


def new_funding_since(exchange, symbol, since_ms):
    try:
        hist = exchange.fetch_funding_rate_history(symbol, since=since_ms, limit=50)
    except Exception:
        return 0.0, since_ms
    new = [h for h in hist if h["timestamp"] > since_ms and h.get("fundingRate") is not None]
    total = sum(h["fundingRate"] for h in new)
    latest_ts = max((h["timestamp"] for h in new), default=since_ms)
    return total, latest_ts


def scan_candidates(exchange):
    markets = exchange.load_markets()
    perp_symbols = [
        m["symbol"] for m in markets.values()
        if m.get("swap") and m.get("quote") == "USDT" and m.get("settle") == "USDT"
    ]
    tickers = exchange.fetch_tickers(perp_symbols)
    snapshot = []
    for symbol in perp_symbols:
        ticker = tickers.get(symbol)
        if not ticker or not ticker.get("last"):
            continue
        vol_ccy24h = ticker.get("info", {}).get("volCcy24h")
        if vol_ccy24h is None:
            continue
        usd_volume = float(vol_ccy24h) * ticker["last"]
        if usd_volume < MIN_24H_VOLUME_USDT:
            continue
        fr = ticker.get("info", {}).get("fundingRate")
        try:
            snapshot_rate = abs(float(fr)) if fr is not None else 0
        except (TypeError, ValueError):
            snapshot_rate = 0
        snapshot.append({"symbol": symbol, "snapshot_rate": snapshot_rate, "usd_volume": usd_volume})

    snapshot.sort(key=lambda r: r["snapshot_rate"], reverse=True)
    candidates = snapshot[:CANDIDATE_POOL]
    for c in candidates:
        c["trailing_pct"] = trailing_annualized(exchange, c["symbol"])
    return [c for c in candidates if c["trailing_pct"] is not None]


def run_once(exchange, state, now):
    now_ms = int(now.timestamp() * 1000)
    candidates_by_symbol = {c["symbol"]: c for c in scan_candidates(exchange)}

    for symbol in list(state["positions"].keys()):
        pos = state["positions"][symbol]
        new_funding_rate_sum, latest_ts = new_funding_since(exchange, symbol, pos["last_checked_ms"])
        pos["funding_collected"] += pos["notional"] * new_funding_rate_sum
        pos["last_checked_ms"] = max(latest_ts, pos["last_checked_ms"])

        current = candidates_by_symbol.get(symbol)
        trailing = current["trailing_pct"] if current else None
        should_exit = trailing is None or abs(trailing) < EXIT_THRESHOLD_PCT or trailing < 0

        if should_exit:
            close_cost = pos["notional"] * (ROUND_TRIP_COST_PCT / 100) / 2
            realized_pnl = pos["funding_collected"] - close_cost
            state["cash"] += pos["notional"] + realized_pnl
            state["closed_trades"].append({
                "symbol": symbol,
                "entry_time": pos["entry_time"],
                "exit_time": now.isoformat(),
                "notional": pos["notional"],
                "funding_collected": pos["funding_collected"],
                "close_cost": close_cost,
                "realized_pnl": realized_pnl,
            })
            del state["positions"][symbol]

    open_slots = MAX_POSITIONS - len(state["positions"])
    if open_slots > 0:
        ranked = sorted(
            (c for c in candidates_by_symbol.values() if c["symbol"] not in state["positions"]),
            key=lambda c: c["trailing_pct"], reverse=True,
        )
        for c in ranked:
            if open_slots <= 0:
                break
            if c["trailing_pct"] < ENTRY_THRESHOLD_PCT:
                continue
            notional = state["starting_capital"] * POSITION_SIZE_PCT
            if notional > state["cash"]:
                continue
            open_cost = notional * (ROUND_TRIP_COST_PCT / 100) / 2
            state["cash"] -= notional + open_cost
            state["positions"][c["symbol"]] = {
                "entry_time": now.isoformat(),
                "entry_trailing_pct": c["trailing_pct"],
                "notional": notional,
                "funding_collected": -open_cost,
                "last_checked_ms": now_ms,
            }
            open_slots -= 1

    equity = state["cash"] + sum(
        p["notional"] + p["funding_collected"] for p in state["positions"].values()
    )
    state["history"].append({"timestamp": now.isoformat(), "equity": equity})
    state["history"] = state["history"][-2000:]
    return equity


def main():
    exchange = ccxt.okx({"options": {"defaultType": "swap"}})
    state = load_state()
    start_time = time.monotonic()
    last_commit_time = 0.0

    print(f"Paper trading loop starting. Checking every {CHECK_INTERVAL_SECONDS}s, "
          f"checkpointing every {COMMIT_INTERVAL_SECONDS}s, "
          f"exiting after {MAX_JOB_SECONDS}s to stay under the Actions job limit.")

    while time.monotonic() - start_time < MAX_JOB_SECONDS:
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            equity = run_once(exchange, state, now)
            print(f"[{now.isoformat()}] equity=${equity:,.2f} "
                  f"open={list(state['positions'].keys())} cash=${state['cash']:,.2f}")
        except Exception as e:
            print(f"[{now.isoformat()}] iteration error (continuing loop): {e}")

        save_state(state)

        elapsed_since_commit = time.monotonic() - last_commit_time
        if elapsed_since_commit >= COMMIT_INTERVAL_SECONDS:
            git_checkpoint(f"Paper trader checkpoint: {now.isoformat()}")
            last_commit_time = time.monotonic()

        time.sleep(CHECK_INTERVAL_SECONDS)

    save_state(state)
    git_checkpoint(f"Paper trader final checkpoint before job exit: "
                    f"{datetime.datetime.now(datetime.timezone.utc).isoformat()}")
    print("Reached max job runtime -- exiting cleanly. "
          "The next scheduled workflow run will resume from the saved state.")


if __name__ == "__main__":
    main()
