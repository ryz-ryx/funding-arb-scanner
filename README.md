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

## What this does NOT do
- **It never places an order.** No exchange API keys are configured or
  required — this only reads Bybit's public market-data endpoints.
- **It never touches any account, balance, or position.**
- It is analysis/monitoring only. Deciding whether to act on any result is
  a decision for a human, made and executed outside this repo.

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
