#!/usr/bin/env python3
"""
MAREA — gestión de riesgo: ¿cap de tamaño + profit parcial mejoran el riesgo-ajustado?

Compara la Marea actual (deja correr 100%) contra variantes:
  - CAP X%: si una posición supera X% del equity, recorta el exceso (rebalance).
  - TRIM:   cuando una posición dobla (+100%), vende 1/3 (asegura ganancia), deja correr el resto.
Señal al cierre del día D, ejecución al OPEN de D+1 (sin lookahead), igual que el resto.

Métricas: CAGR, MaxDD, Sharpe, concentración (peso máx de 1 posición). Decide con datos:
mejor risk-adjusted (Sharpe↑ / MaxDD↓) sin matar el CAGR → vale; solo recortar upside → no.

Reusa backtest_marea_oos (config/universo) + backtest_portfolio_momentum (datos/métricas).
"""
import os, sys, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_portfolio_momentum import (
    fetch_daily, precompute, _close_on, build_calendar, monthly_stats, MOM_LOOKBACK,
)
from backtest_marea_oos import START, CAP, FEE, K, BREAKOUT, MULT, VOL_TARGET, UNIVERSE


def run_marea(data, cal, idx_obj, cap_pct=None, trim=False, trim_trig=1.0, trim_frac=1/3):
    cash = CAP; positions = {}; daily_eq = []; trades = []; maxw = []
    warm = max(200, MOM_LOOKBACK, BREAKOUT) + 1
    for d in cal:
        # ── salidas chandelier ──
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
            trades.append({"tk": tk, "pnl_pct": (xo/p["entry"]-1)*100})
        # ── entradas (macro + ranking + top-K + vol sizing) ──
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
            eqn = cash + sum(positions[t]["shares"]*_close_on(data[t], d) for t in positions)
            for m, tk, i in cands[:free]:
                dd = data[tk]; eo = dd["bars"][i+1]["o"]
                base = eqn/K
                a = dd["pre"]["atr"][i]; ap = (a/dd["bars"][i]["c"]) if a else VOL_TARGET
                base *= min(1.0, VOL_TARGET/ap) if ap > 0 else 1.0
                tgt = min(cash, base)
                if tgt <= FEE+1: continue
                sh = (tgt-FEE)/eo
                cash -= sh*eo + FEE
                positions[tk] = {"shares": sh, "entry": eo, "hh": dd["bars"][i+1]["h"]}
        # equity al cierre
        eq = cash + sum(positions[tk]["shares"]*_close_on(data[tk], d) for tk in positions)
        daily_eq.append((d, eq))
        # concentración: peso máximo de una posición
        if positions and eq > 0:
            maxw.append(max(positions[tk]["shares"]*_close_on(data[tk], d) for tk in positions)/eq)
        # ── CAP + TRIM (señal al cierre D, ejecuta open D+1) ──
        if (cap_pct or trim) and eq > 0:
            for tk, p in list(positions.items()):
                dd = data[tk]; i = dd["idx"].get(d)
                if i is None or i+1 >= len(dd["bars"]): continue
                c = dd["bars"][i]["c"]; no = dd["bars"][i+1]["o"]
                if trim and not p.get("trimmed") and c/p["entry"]-1 >= trim_trig:
                    s = p["shares"]*trim_frac
                    cash += s*no - FEE; p["shares"] -= s; p["trimmed"] = True
                val = p["shares"]*c
                if cap_pct and val > cap_pct*eq:
                    s = min((val - cap_pct*eq)/c, p["shares"]*0.99)
                    if s > 0:
                        cash += s*no - FEE; p["shares"] -= s
    return daily_eq, trades, maxw


def main():
    print(f"\n{'#'*96}\n  MAREA — CAP de tamaño + PROFIT PARCIAL  (vs base) | desde 2022\n{'#'*96}")
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

    variants = [
        ("BASE (deja correr 100%)",       dict()),
        ("CAP 30%",                       dict(cap_pct=0.30)),
        ("CAP 25%",                       dict(cap_pct=0.25)),
        ("CAP 20%",                       dict(cap_pct=0.20)),
        ("TRIM (dobla→vende 1/3)",        dict(trim=True)),
        ("CAP 25% + TRIM",                dict(cap_pct=0.25, trim=True)),
    ]
    print(f"\n  {'Variante':<28} {'Total%':>8} {'CAGR%':>7} {'MaxDD%':>8} {'Sharpe':>7} "
          f"{'PesoMáx':>8} {'PesoMedio':>10} {'Trades':>7}")
    print(f"  {'-'*92}")
    for label, kw in variants:
        deq, trades, maxw = run_marea(data, cal, idx_obj, **kw)
        s = monthly_stats(deq)
        pmax = max(maxw)*100 if maxw else 0
        pavg = statistics.mean(maxw)*100 if maxw else 0
        print(f"  {label:<28} {s['total']:>+7.0f}% {s['cagr']:>+6.1f}% {s['mdd']:>+7.1f}% "
              f"{s['sharpe']:>7.2f} {pmax:>7.0f}% {pavg:>9.0f}% {len(trades):>7}")
    # QQQ benchmark
    deqq = [(b["t"], CAP*b["c"]/qqq[0]["c"]) for b in qqq]
    sq = monthly_stats(deqq)
    print(f"  {'-'*92}")
    print(f"  {'QQQ buy&hold':<28} {sq['total']:>+7.0f}% {sq['cagr']:>+6.1f}% {sq['mdd']:>+7.1f}% {sq['sharpe']:>7.2f}")
    print(f"\n  Lectura: ¿alguna variante MEJORA Sharpe y/o reduce MaxDD sin destruir el CAGR?")
    print(f"  PesoMáx = mayor % que llegó a pesar 1 sola posición (concentración). Sin slippage.\n")


if __name__ == "__main__":
    main()
