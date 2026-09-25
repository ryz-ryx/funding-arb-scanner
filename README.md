# Funding Rate Scanner (Bybit)

Read-only scanner that ranks Bybit USDT perpetual swaps by funding rate
(current + trailing 3-day average, annualized), runs on a schedule via
GitHub Actions, and logs results to [`SCAN_LOG.md`](SCAN_LOG.md).

## What this does
- Every 6 hours (and on-demand via the Actions tab), a GitHub-hosted runner
  fetches public funding-rate data from Bybit for all liquid USDT perpetuals.
- It ranks them by **trailing 3-day average annualized funding rate** (not
  just the latest snapshot, which is noisy) and appends the top 40 to
  `SCAN_LOG.md`, committed back to this repo.

## Paper trading loop
`.github/workflows/paper_trade.yml` runs `paper_trader/run.py` on the same
schedule, simulating the funding-capture strategy with a virtual $10,000
balance: it opens a simulated position when a symbol's trailing funding
clears a threshold, accrues real historical funding into a fake P&L, and
closes when the rate fades. State lives in `docs/data/portfolio.json`, shown
on the dashboard below. GitHub Actions caps a single job at 6 hours, so the
job loops internally for ~5h50m, checkpointing to git every 30 minutes, then
exits and lets the next scheduled run pick up where it left off — a few
minutes of gap every 6 hours, not a true always-on process (that would need
a persistent server, not GitHub Actions).

## What this does NOT do
- **It never places a real order.** No exchange API keys are configured or
  required anywhere in this repo — everything reads public market-data
  endpoints only.
- **It never touches any real account, balance, or position.** The paper
  trading loop's "$10,000" and P&L are entirely simulated numbers in a JSON
  file, not connected to any exchange account.
- It is analysis/simulation only. Deciding whether to act on any result with
  real capital is a decision for a human, made and executed outside this repo.

## Background
This scanner is one output of a larger research project into whether a
funding-rate/basis-arbitrage strategy (long spot + short perpetual,
collecting the funding payment) can be profitable after realistic fees.
Backtests on historical funding data found some perpetuals with sustained
double-digit annualized net yield after costs, alongside majors (BTC/ETH)
showing only ~1.5-2%/yr. This scanner exists to keep watching for where
that opportunity currently sits, since it moves over time.

## Running it yourself
```bash
pip install -r requirements.txt
python scanner/live_scanner.py
```
