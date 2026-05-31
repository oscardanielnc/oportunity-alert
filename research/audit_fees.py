import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
AUDITORIA HONESTA — Watcher v3 NETO de comisiones eToro.

Reutiliza el motor de produccion (super_backtest.simulate) y el modelo de
comisiones (backtest_perticker_v2.etoro_fee_analysis) para responder UNA pregunta:

  ¿Sobrevive la estrategia a las comisiones reales de eToro?
    LONG : $1 abrir + $1 cerrar = $2 fijos por round-trip
    SHORT: 0.15% + 0.15% = 0.30% del monto por round-trip

Reporta por ticker y por timeframe: gross vs net a varios tamanos de posicion,
y el tamano de posicion minimo (break-even) para que cada ticker sea rentable.
"""
import os, sys
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from super_backtest import fetch_all, simulate
from backtest_perticker_v2 import etoro_fee_analysis, ETORO_POSITION_SIZES

TICKERS = ["NVDA", "AMD", "MU", "PLTR", "IONQ", "QBTS", "APP", "RDDT", "AVGO", "MSFT"]

# (tf, htf, weeks, max_scans, adverse, warmup) — params v3 calibrados del spec
TFS = {
    "1m": ("1Min", "5Min",  2, 20, 0.5, 30),
    "5m": ("5Min", "15Min", 6, 12, 0.0, 30),
}


def run():
    now = datetime.now(timezone.utc)
    print(f"\n{'='*92}")
    print(f"  AUDITORIA NETA DE COMISIONES — Watcher v3   ({now.strftime('%Y-%m-%d %H:%M')} UTC)")
    print(f"  LONG: $2 fijo/trade  |  SHORT: 0.30%/trade  |  tamanos posicion: {ETORO_POSITION_SIZES}")
    print(f"{'='*92}")

    grand = {}  # tf -> list of all trades

    for tf_key, (tf, htf, weeks, max_scans, adverse, warmup) in TFS.items():
        start = (now - timedelta(weeks=weeks)).strftime("%Y-%m-%dT%H:%M:%SZ")
        htf_start = (now - timedelta(weeks=weeks + 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        all_trades = []

        print(f"\n{'-'*92}")
        print(f"  [{tf_key}]  {weeks} semanas  |  time-stop {max_scans} scans / adv>={adverse}%")
        print(f"  {'Ticker':<6} {'Trades':>6} {'L/S':>7} {'GrossAvg':>9} {'NET@500':>9} "
              f"{'NET@1k':>8} {'NET@2k':>8} {'NET@5k':>8} {'BreakEven$':>11} {'Short':>9}")
        print(f"  {'-'*88}")

        for ticker in TICKERS:
            bars = fetch_all(ticker, tf, start, limit=8000)
            htf_bars = fetch_all(ticker, htf, htf_start, limit=3000)
            for b in bars:     b["ticker"] = ticker
            for b in htf_bars: b["ticker"] = ticker
            if len(bars) < warmup + 5:
                print(f"  {ticker:<6}  sin datos ({len(bars)})")
                continue

            trades = simulate(bars, htf_bars, max_scans, adverse, warmup)
            all_trades += trades
            if not trades:
                print(f"  {ticker:<6} {0:>6}")
                continue

            fa = etoro_fee_analysis(trades)
            n_long = sum(1 for t in trades if t["side"] == "LONG")
            n_short = sum(1 for t in trades if t["side"] == "SHORT")

            L = fa.get("long", {})
            nbs = L.get("net_by_position_size", {})
            def net(sz): return nbs.get(str(sz), {}).get("net_avg_pnl", float("nan"))
            g_avg = L.get("gross_avg_pnl", float("nan"))
            be = L.get("breakeven_position_usd", None)
            be_s = f"${be}" if be else "n/a(<=0)"

            S = fa.get("short", {})
            short_s = (f"{S.get('net_avg_pnl','-'):>+.3f}%" if S else "  --  ")

            print(f"  {ticker:<6} {len(trades):>6} {f'{n_long}/{n_short}':>7} "
                  f"{g_avg:>+8.3f}% {net(500):>+8.3f}% {net(1000):>+7.3f}% "
                  f"{net(2000):>+7.3f}% {net(5000):>+7.3f}% {be_s:>11} {short_s:>9}")

        grand[tf_key] = all_trades

        # Agregado del TF
        fa = etoro_fee_analysis(all_trades)
        L = fa.get("long", {}); S = fa.get("short", {})
        nbs = L.get("net_by_position_size", {})
        print(f"  {'-'*88}")
        print(f"  AGREGADO {tf_key}: {len(all_trades)} trades | "
              f"GrossAvg LONG {L.get('gross_avg_pnl','?')}% | "
              f"NET@1k {nbs.get('1000',{}).get('net_avg_pnl','?')}% | "
              f"NET@5k {nbs.get('5000',{}).get('net_avg_pnl','?')}% | "
              f"BreakEven ${L.get('breakeven_position_usd','?')}")
        if S:
            print(f"             SHORT gross {S['gross_avg_pnl']}% -> net {S['net_avg_pnl']}% "
                  f"({'VIABLE' if S['viable'] else 'NO VIABLE'})")

    print(f"\n{'='*92}")
    print("  Lectura: NET@X = ganancia promedio por trade tras comisiones con posicion de $X.")
    print("  Si NET es negativo, ese tamano de posicion PIERDE dinero en promedio por trade.")
    print("  No incluye spread ni slippage (la realidad es algo peor que esto).")
    print(f"{'='*92}\n")


if __name__ == "__main__":
    run()
