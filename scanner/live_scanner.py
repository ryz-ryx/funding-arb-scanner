"""
Read-only funding-rate scanner across all Bybit USDT perpetuals.

Ranks every listed perpetual by current + trailing (3-day average) funding
rate, annualized, so real sustained opportunities are visible rather than
single-snapshot noise. Designed to run unattended on a schedule (see
.github/workflows/scan.yml) and log its output -- it never places an order
and never touches any account; it only reads public market data.

Run: python scanner/live_scanner.py
"""

import ccxt

MIN_24H_VOLUME_USDT = 2_000_000
CANDIDATE_POOL = 60
TOP_N = 40
TRAILING_INTERVALS = 9  # 3 days at 8h funding interval


def trailing_annualized(exchange, symbol):
    try:
        hist = exchange.fetch_funding_rate_history(symbol, limit=TRAILING_INTERVALS)
    except Exception:
        return None, None
    if not hist:
        return None, None
    rates = [h["fundingRate"] for h in hist if h.get("fundingRate") is not None]
    if not rates:
        return None, None
    avg_rate = sum(rates) / len(rates)
    return avg_rate * 3 * 365 * 100, len(rates)


def scan():
    # OKX, not Bybit: Bybit's CloudFront distribution 403-blocks GitHub
    # Actions' US-region runner IPs entirely ("blocked from your country").
    # OKX has no such geo-block and exposes the same funding-rate data.
    exchange = ccxt.okx({"options": {"defaultType": "swap"}})
    markets = exchange.load_markets()
    perp_symbols = [
        m["symbol"] for m in markets.values()
        if m.get("swap") and m.get("quote") == "USDT" and m.get("settle") == "USDT"
    ]

    snapshot = []
    tickers = exchange.fetch_tickers(perp_symbols)
    for symbol in perp_symbols:
        ticker = tickers.get(symbol)
        if not ticker or not ticker.get("last"):
            continue
        # OKX's own quoteVolume field is unreliable for swaps via ccxt;
        # info.volCcy24h is base-currency 24h volume -- convert to USDT.
        vol_ccy24h = ticker.get("info", {}).get("volCcy24h")
        if vol_ccy24h is None:
            continue
        usd_volume = float(vol_ccy24h) * ticker["last"]
        if usd_volume < MIN_24H_VOLUME_USDT:
            continue
        try:
            fr = exchange.fetch_funding_rate(symbol)
        except Exception:
            continue
        rate = fr.get("fundingRate")
        if rate is None:
            continue
        interval_hours = 8
        try:
            interval_hours = int(str(fr.get("interval", "8h")).replace("h", ""))
        except (TypeError, ValueError):
            pass
        intervals_per_day = 24 / interval_hours
        annualized = rate * intervals_per_day * 365
        snapshot.append(
            {
                "symbol": symbol,
                "annualized_pct": annualized * 100,
                "24h_volume_usdt": usd_volume,
            }
        )

    snapshot.sort(key=lambda r: abs(r["annualized_pct"]), reverse=True)
    candidates = snapshot[:CANDIDATE_POOL]

    for r in candidates:
        trailing_pct, n = trailing_annualized(exchange, r["symbol"])
        r["trailing_3d_annualized_pct"] = trailing_pct
        r["trailing_intervals_used"] = n

    candidates = [r for r in candidates if r["trailing_3d_annualized_pct"] is not None]
    candidates.sort(key=lambda r: abs(r["trailing_3d_annualized_pct"]), reverse=True)
    return candidates[:TOP_N]


if __name__ == "__main__":
    import datetime
    import json
    import os
    import sys

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    now = now_dt.strftime("%Y-%m-%d %H:%M UTC")
    print(f"## Scan: {now}\n")
    print(
        f"Min 24h volume ${MIN_24H_VOLUME_USDT:,.0f} | trailing "
        f"{TRAILING_INTERVALS} intervals on top {CANDIDATE_POOL} candidates\n"
    )
    top = scan()
    print("| Symbol | Trailing 3d Annualized % | 24h Volume USDT |")
    print("|---|---:|---:|")
    for r in top:
        print(
            f"| {r['symbol']} | {r['trailing_3d_annualized_pct']:.2f}% | "
            f"{r['24h_volume_usdt']:,.0f} |"
        )
    print(
        "\n_Read-only scan. No orders are placed. Trailing3d Ann.% is the "
        "average of the last ~3 days of funding intervals, annualized -- "
        "filters out one-off spikes. Cross-check any candidate with a full "
        "historical backtest before considering a position._"
    )

    # Machine-readable output for the web dashboard (docs/data/latest_scan.json).
    out_path = sys.argv[1] if len(sys.argv) > 1 else None
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(
                {
                    "generated_at": now_dt.isoformat(),
                    "min_24h_volume_usdt": MIN_24H_VOLUME_USDT,
                    "trailing_intervals": TRAILING_INTERVALS,
                    "results": [
                        {
                            "symbol": r["symbol"],
                            "trailing_3d_annualized_pct": r["trailing_3d_annualized_pct"],
                            "24h_volume_usdt": r["24h_volume_usdt"],
                        }
                        for r in top
                    ],
                },
                f,
                indent=2,
            )
