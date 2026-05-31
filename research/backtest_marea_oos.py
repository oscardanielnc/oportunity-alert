#!/usr/bin/env python3
"""
VALIDACIÓN OUT-OF-SAMPLE de MAREA (momentum diario).

El backfill del piloto fue UNA ventana parabólica (jun25-may26, +228% pero 65% de
un ticker). Aquí probamos si el edge se sostiene AÑO POR AÑO y en distintos regímenes
(incluye el bear de 2022), y si depende de un solo ganador (concentración).

Config = la del piloto: K=5, breakout=50d, chandelier 4×ATR, +filtro macro QQQ, vol sizing.
Reusa precompute/fetch_daily de backtest_portfolio_momentum. Comisión $1/lado. Sin slippage.
"""
import os, sys, statistics
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_portfolio_momentum import (
    fetch_daily, precompute, _close_on, build_calendar, MOM_LOOKBACK,
)

START = "2022-01-01T00:00:00Z"
CAP = 10000.0
FEE = 1.0
K, BREAKOUT, MULT, VOL_TARGET = 5, 50, 4.0, 0.03
UNIVERSE = ["NVDA","AMD","MU","PLTR","IONQ","QBTS","APP","RDDT","AVGO","MSFT",
            "DELL","SMCI","TSLA","META","GOOGL","AMZN","AAPL","NFLX","COIN","MRVL",
            "ANET","CRWD","WDC","LITE","COHR","SNDK","ORCL","ARM","QCOM","TSM"]


def run_with_pnl(data, calendar, idx_obj):
    cash = CAP; positions = {}; daily_eq = []; trades = []
    warm = max(200, MOM_LOOKBACK, BREAKOUT) + 1
    for d in calendar:
        # salidas (chandelier)
        to_exit = []
        for tk, p in positions.items():
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None: continue
            p["hh"] = max(p["hh"], dd["bars"][i]["h"])
            a = dd["pre"]["atr"][i]
            if a is None: continue
            if dd["bars"][i]["c"] < p["hh"] - MULT*a and i+1 < len(dd["bars"]):
                to_exit.append((tk, i))
        for tk, i in to_exit:
            dd = data[tk]; xo = dd["bars"][i+1]["o"]; p = positions.pop(tk)
            cash += p["shares"]*xo - FEE
            pnl_usd = p["shares"]*(xo - p["entry"]) - 2*FEE
            trades.append({"tk": tk, "entry_date": p["edate"], "exit_date": dd["bars"][i+1]["t"],
                           "pnl_usd": pnl_usd, "pnl_pct": (xo/p["entry"]-1)*100})
        # entradas (macro + ranking + top-K + vol sizing)
        regime_ok = True
        ii = idx_obj["idx"].get(d)
        if ii is not None and idx_obj["sma"][ii] is not None:
            regime_ok = idx_obj["bars"][ii]["c"] > idx_obj["sma"][ii]
        free = K - len(positions)
        if free > 0 and regime_ok:
            cands = []
            for tk in data:
                if tk in positions: continue
                dd = data[tk]; i = dd["idx"].get(d)
                if i is None or i < warm or i+1 >= len(dd["bars"]): continue
                pre = dd["pre"]; c = dd["bars"][i]["c"]
                sma, bkh, m = pre["sma"][i], pre["bkh"][i], pre["mom"][i]
                if sma and bkh and m is not None and c > sma and c >= bkh:
                    cands.append((m, tk, i))
            cands.sort(reverse=True)
            equity_now = cash + sum(positions[t]["shares"]*_close_on(data[t], d) for t in positions)
            for m, tk, i in cands[:free]:
                dd = data[tk]; eo = dd["bars"][i+1]["o"]
                base = equity_now/K
                a = dd["pre"]["atr"][i]; atr_pct = (a/dd["bars"][i]["c"]) if a else VOL_TARGET
                base *= min(1.0, VOL_TARGET/atr_pct) if atr_pct > 0 else 1.0
                target = min(cash, base)
                if target <= FEE+1: continue
                sh = (target-FEE)/eo
                cash -= sh*eo + FEE
                positions[tk] = {"shares": sh, "entry": eo, "hh": dd["bars"][i+1]["h"],
                                 "edate": dd["bars"][i+1]["t"]}
        eq = cash + sum(positions[tk]["shares"]*_close_on(data[tk], d) for tk in positions)
        daily_eq.append((d, eq))
    return daily_eq, trades


def maxdd(eq_pairs):
    peak = -1e9; mdd = 0.0
    for _, e in eq_pairs:
        peak = max(peak, e); mdd = min(mdd, e/peak-1)
    return mdd*100


def main():
    print(f"\n{'#'*92}\n  MAREA OUT-OF-SAMPLE — K{K} brk{BREAKOUT} chand{MULT} +macro +vol | desde 2022\n{'#'*92}")
    raw = {}
    for tk in UNIVERSE + ["QQQ"]:
        b = fetch_daily(tk, START)
        if len(b) > 60: raw[tk] = b
    qqq = raw.pop("QQQ")
    data = {tk: {"bars": bars, "idx": {b["t"]: i for i, b in enumerate(bars)},
                 "pre": precompute(bars, BREAKOUT)} for tk, bars in raw.items()}
    cal = build_calendar(data)
    idx_obj = {"bars": qqq, "idx": {b["t"]: i for i, b in enumerate(qqq)},
               "sma": precompute(qqq, 50)["sma"]}
    qc = {b["t"]: b["c"] for b in qqq}

    deq, trades = run_with_pnl(data, cal, idx_obj)
    final = deq[-1][1]
    yrs = len(deq)/252
    cagr = (final/CAP)**(1/yrs)-1 if yrs > 0 else 0
    print(f"\n  Universo {len(data)} | {len(cal)} días | {len(trades)} trades cerrados")
    print(f"  MAREA total {(final/CAP-1)*100:+.0f}% | CAGR {cagr*100:+.1f}% | MaxDD {maxdd(deq):+.1f}%")
    q0, q1 = qqq[0]["c"], qqq[-1]["c"]
    qcagr = (q1/q0)**(1/yrs)-1
    print(f"  QQQ   total {(q1/q0-1)*100:+.0f}% | CAGR {qcagr*100:+.1f}% (benchmark)")

    # ── POR AÑO (vs QQQ) ──
    print(f"\n{'='*92}\n  POR AÑO — ¿robusto en cada régimen?  (Marea vs QQQ)\n{'='*92}")
    print(f"  {'Año':<6} {'Marea ret':>10} {'QQQ ret':>9} {'Marea-QQQ':>10} {'MaxDD año':>10} {'Trades':>7}")
    by_year = defaultdict(list)
    for d, e in deq: by_year[d[:4]].append((d, e))
    for yr in sorted(by_year):
        ep = by_year[yr]
        mret = (ep[-1][1]/ep[0][1]-1)*100
        ds = [d for d, _ in ep]
        qd0 = qc.get(ds[0]); qd1 = qc.get(ds[-1])
        qret = (qd1/qd0-1)*100 if qd0 and qd1 else 0
        dd = maxdd(ep)
        ntr = sum(1 for t in trades if t["exit_date"][:4] == yr)
        flag = "✓" if mret > qret else " "
        print(f"  {yr:<6} {mret:>+9.1f}% {qret:>+8.1f}% {mret-qret:>+9.1f}% {flag} {dd:>+9.1f}% {ntr:>7}")

    # ── CONCENTRACIÓN (¿un-winner como WDC?) ──
    print(f"\n{'='*92}\n  CONCENTRACIÓN — ¿el resultado cuelga de pocos trades?\n{'='*92}")
    tr_sorted = sorted(trades, key=lambda t: -t["pnl_usd"])
    tot_gain = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    for label, n in [("top-1", 1), ("top-3", 3), ("top-5", 5)]:
        share = sum(t["pnl_usd"] for t in tr_sorted[:n]) / tot_gain * 100 if tot_gain else 0
        print(f"  {label:<7} de ganancias = {share:>5.0f}%  ({', '.join(t['tk'] for t in tr_sorted[:n])})")
    wins = [t for t in trades if t["pnl_pct"] > 0]
    print(f"  WR {len(wins)/len(trades)*100:.0f}% | mejor +{max(t['pnl_pct'] for t in trades):.0f}% | "
          f"peor {min(t['pnl_pct'] for t in trades):+.0f}% | trades positivos cargan ${tot_gain:+.0f}")
    print(f"\n  Lectura: si Marea bate a QQQ en MÚLTIPLES años (no solo el bull) y la concentración")
    print(f"  no es extrema, el edge es robusto. Si todo cuelga de 2023-25 / 1 ticker, es beta de ventana.\n")


if __name__ == "__main__":
    main()
