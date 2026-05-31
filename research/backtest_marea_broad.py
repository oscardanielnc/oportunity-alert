#!/usr/bin/env python3
"""
MAREA sobre el UNIVERSO ANCHO (reglas, cross-sector) — número honesto + diversificación.

A diferencia de backtest_marea_oos (30 tickers AI elegidos a mano = survivorship +
concentración temática), aquí usamos el universo REAL del piloto en vivo: top-N por
liquidez, NASDAQ/NYSE, todos los sectores, sin elegir ganadores.

Reporta: agregado vs QQQ, por-año (¿robusto en cada régimen?), concentración (top trades)
y qué tickers cargan las ganancias (¿diverso o todo AI?). Config piloto. Sin slippage.

NOTA survivorship: usar el universo de liquidez ACTUAL aún tiene sesgo leve (nombres hoy
líquidos no todos existían en 2022). Es MUCHO menos sesgado que elegir 30 a mano, pero
un test 100% limpio necesitaría liquidez point-in-time por período.
"""
import os, sys, json, statistics
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_portfolio_momentum import fetch_daily, precompute, build_calendar
from backtest_marea_oos import run_with_pnl, maxdd, START, CAP

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def main():
    uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]
    print(f"\n{'#'*94}\n  MAREA — UNIVERSO ANCHO (reglas, {len(uni)} tickers cross-sector) | desde 2022\n{'#'*94}")
    raw = {}
    for tk in uni + ["QQQ"]:
        b = fetch_daily(tk, START)
        if len(b) > 60: raw[tk] = b
    qqq = raw.pop("QQQ")
    data = {tk: {"bars": bars, "idx": {b["t"]: i for i, b in enumerate(bars)},
                 "pre": precompute(bars, 50)} for tk, bars in raw.items()}
    cal = build_calendar(data)
    idx_obj = {"bars": qqq, "idx": {b["t"]: i for i, b in enumerate(qqq)},
               "sma": precompute(qqq, 50)["sma"]}
    qc = {b["t"]: b["c"] for b in qqq}
    print(f"  Con datos suficientes desde 2022: {len(data)}/{len(uni)} tickers "
          f"(el resto IPO reciente — el motor solo opera cuando hay historia)")

    deq, trades = run_with_pnl(data, cal, idx_obj)
    final = deq[-1][1]; yrs = len(deq)/252
    cagr = (final/CAP)**(1/yrs)-1 if yrs > 0 else 0
    q0, q1 = qqq[0]["c"], qqq[-1]["c"]; qcagr = (q1/q0)**(1/yrs)-1
    print(f"\n  {len(trades)} trades | MAREA total {(final/CAP-1)*100:+.0f}% | CAGR {cagr*100:+.1f}% | MaxDD {maxdd(deq):+.1f}%")
    print(f"  QQQ benchmark: total {(q1/q0-1)*100:+.0f}% | CAGR {qcagr*100:+.1f}%")

    print(f"\n{'='*94}\n  POR AÑO (Marea vs QQQ)\n{'='*94}")
    print(f"  {'Año':<6} {'Marea':>9} {'QQQ':>8} {'Marea-QQQ':>10} {'MaxDD año':>10} {'Trades':>7}")
    by_year = defaultdict(list)
    for d, e in deq: by_year[d[:4]].append((d, e))
    for yr in sorted(by_year):
        ep = by_year[yr]; mret = (ep[-1][1]/ep[0][1]-1)*100
        ds = [d for d, _ in ep]; qd0, qd1 = qc.get(ds[0]), qc.get(ds[-1])
        qret = (qd1/qd0-1)*100 if qd0 and qd1 else 0
        ntr = sum(1 for t in trades if t["exit_date"][:4] == yr)
        flag = "✓" if mret > qret else " "
        print(f"  {yr:<6} {mret:>+8.1f}% {qret:>+7.1f}% {mret-qret:>+9.1f}% {flag} {maxdd(ep):>+9.1f}% {ntr:>7}")

    print(f"\n{'='*94}\n  CONCENTRACIÓN + DIVERSIDAD (¿pocos tickers / todo AI?)\n{'='*94}")
    ts = sorted(trades, key=lambda t: -t["pnl_usd"])
    tot = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    for lbl, n in [("top-1", 1), ("top-3", 3), ("top-5", 5), ("top-10", 10)]:
        share = sum(t["pnl_usd"] for t in ts[:n])/tot*100 if tot else 0
        print(f"  {lbl:<7} de ganancias = {share:>4.0f}%  ({', '.join(t['tk'] for t in ts[:n])})")
    wins = [t for t in trades if t["pnl_pct"] > 0]
    print(f"  WR {len(wins)/len(trades)*100:.0f}% | tickers distintos operados: {len(set(t['tk'] for t in trades))} | "
          f"ganadores distintos: {len(set(t['tk'] for t in wins))}")
    # contribución por ticker (top 12)
    bytk = defaultdict(float)
    for t in trades: bytk[t["tk"]] += t["pnl_usd"]
    print("  Top 12 contribuyentes ($): " + " | ".join(
        f"{tk} ${v:+.0f}" for tk, v in sorted(bytk.items(), key=lambda x: -x[1])[:12]))
    print(f"\n  Lectura: si bate a QQQ en varios años con ganancias REPARTIDAS en muchos tickers de")
    print(f"  distintos sectores (no 1-2 nombres AI), Marea es un momentum cross-sectional real.\n")


if __name__ == "__main__":
    main()
