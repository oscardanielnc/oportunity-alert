#!/usr/bin/env python3
"""
MAREA — FILTRO DE INVALIDACIÓN EN LA ENTRADA  ("tesis rota").

Motivación (Oscar, 2026-05-31): el sistema valida la SEÑAL al cierre y compra al OPEN
del día siguiente, SIN una línea en la arena. Si el open abre muy por debajo del nivel
del breakout/soporte, ¿es "entrada barata sana" o "tesis rompiéndose"? Hoy entramos a
ciegas. Idea: VETAR la entrada si el open abre por debajo de un nivel de invalidación.

A diferencia del pullback overnight (no medible con diarias), esto SÍ se mide: el veto
depende solo del open de D+1 vs un nivel conocido al cierre de D.

Compara, sobre el MISMO motor validado (K=5, breakout 50d, chandelier 4×ATR, +macro QQQ,
vol sizing, fee $1/lado), el modo base `open` contra varios vetos:
  - breakout : vetar si open < nivel de breakout (max cierre 50d previo) -> breakout perdido
  - support10: vetar si open < mínimo de lows 10d -> perdió soporte reciente
  - gapN     : vetar si open < cierre_señal × (1 - N%) -> gap-down fuerte

Diagnóstico CLAVE (la lección): de las entradas VETADAS, ¿qué habrían rendido si entrabas
al open igual (fwd 20d)? Si la mediana es negativa y hay pocos runners -> el veto te salva
de perdedores (bueno). Si son ganadores -> el veto te saca de runners (malo).

Reusa motor/datos validados. Comisión $1/lado. Sin slippage.
"""
import os, sys, statistics
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_portfolio_momentum import (
    fetch_daily, precompute, _close_on, build_calendar, monthly_stats, MOM_LOOKBACK,
)
from backtest_marea_oos import START, CAP, FEE, K, BREAKOUT, MULT, VOL_TARGET, UNIVERSE
from backtest_marea_entry import precompute_ext, _load_universe

WARM = max(200, MOM_LOOKBACK, BREAKOUT) + 1


def _veto_level(pre, i, c, mode, gap_pct):
    """Nivel por debajo del cual el open invalida la entrada. None => sin veto (base)."""
    if mode == "breakout":  return pre["bkh"][i]
    if mode == "support10": return pre["sup10"][i]
    if mode == "gap":       return c * (1 - gap_pct)
    return None


def run(data, cal, idx_obj, veto_mode=None, gap_pct=0.0):
    """Motor base (entrada=open, salida=chandelier 4×ATR) + veto de invalidación opcional."""
    cash = CAP; positions = {}; daily_eq = []; trades = []
    n_signals = 0; n_entered = 0; n_vetoed = 0; vetoed_fwd = []

    for d in cal:
        # ── salidas: chandelier 4×ATR ────────────────────────────────────────────
        to_exit = []
        for tk, p in positions.items():
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None: continue
            b = dd["bars"][i]; p["hh"] = max(p["hh"], b["h"])
            a = dd["pre"]["atr"][i]
            if a is None: continue
            if b["c"] < p["hh"] - MULT*a and i+1 < len(dd["bars"]):
                to_exit.append((tk, i))
        for tk, i in to_exit:
            dd = data[tk]; xo = dd["bars"][i+1]["o"]; p = positions.pop(tk)
            cash += p["shares"]*xo - FEE
            trades.append({"tk": tk, "pnl_pct": (xo/p["entry"]-1)*100,
                           "pnl_usd": p["shares"]*(xo-p["entry"]) - 2*FEE,
                           "entry_date": p["edate"], "exit_date": dd["bars"][i+1]["t"]})

        # ── entradas: macro + ranking + top-K, con veto de invalidación ───────────
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
                if i is None or i < WARM or i+1 >= len(dd["bars"]): continue
                pre = dd["pre"]; c = dd["bars"][i]["c"]
                sma, bkh, m = pre["sma"][i], pre["bkh"][i], pre["mom"][i]
                if sma and bkh and m is not None and c > sma and c >= bkh:
                    cands.append((m, tk, i))
            cands.sort(reverse=True)
            equity_now = cash + sum(positions[t]["shares"]*_close_on(data[t], d) for t in positions)
            for m, tk, i in cands[:free]:
                dd = data[tk]; b = dd["bars"][i]; c = b["c"]
                eo = dd["bars"][i+1]["o"]
                n_signals += 1
                lvl = _veto_level(dd["pre"], i, c, veto_mode, gap_pct)
                if lvl is not None and eo < lvl:                    # tesis rota al open -> veto
                    n_vetoed += 1
                    hz = min(i+1+20, len(dd["bars"])-1)             # fwd20 entrando al open igual
                    vetoed_fwd.append((dd["bars"][hz]["c"]/eo - 1)*100)
                    continue
                a = dd["pre"]["atr"][i]; ap = (a/c) if a else VOL_TARGET
                base = equity_now/K * (min(1.0, VOL_TARGET/ap) if ap > 0 else 1.0)
                tgt = min(cash, base)
                if tgt <= FEE+1: continue
                sh = (tgt-FEE)/eo
                cash -= sh*eo + FEE
                positions[tk] = {"shares": sh, "entry": eo, "hh": dd["bars"][i+1]["h"],
                                 "edate": dd["bars"][i+1]["t"]}
                n_entered += 1

        eq = cash + sum(positions[tk]["shares"]*_close_on(data[tk], d) for tk in positions)
        daily_eq.append((d, eq))

    stats = {
        "n_signals": n_signals, "n_entered": n_entered, "n_vetoed": n_vetoed,
        "veto_rate": (n_vetoed/n_signals*100) if n_signals else 0,
        "vetoed_median": statistics.median(vetoed_fwd) if vetoed_fwd else 0,
        "vetoed_runners": sum(1 for r in vetoed_fwd if r > 15),
        "vetoed_losers": sum(1 for r in vetoed_fwd if r < -10),
        "n_vetoed_fwd": len(vetoed_fwd),
    }
    return daily_eq, trades, stats


def _by_year(deq):
    yr = defaultdict(list)
    for d, e in deq: yr[d[:4]].append(e)
    return {y: (v[-1]/v[0]-1)*100 for y, v in sorted(yr.items())}


def main():
    uni = _load_universe()
    print(f"\n{'#'*104}")
    print(f"  MAREA — FILTRO DE INVALIDACIÓN EN LA ENTRADA  |  {len(uni)} tickers  |  desde {START}")
    print(f"{'#'*104}")
    raw = {}
    for tk in sorted(set(uni) | {"QQQ"}):
        b = fetch_daily(tk, START)
        if len(b) > 60: raw[tk] = b
    qqq = raw.pop("QQQ")
    data = {tk: {"bars": bars, "idx": {b["t"]: i for i, b in enumerate(bars)},
                 "pre": precompute_ext(bars)} for tk, bars in raw.items()}
    cal = build_calendar(data)
    idx_obj = {"bars": qqq, "idx": {b["t"]: i for i, b in enumerate(qqq)},
               "sma": precompute(qqq, 50)["sma"]}
    print(f"  Con historia suficiente: {len(data)}/{len(uni)} tickers | {len(cal)} días\n")

    modes = [("base (sin veto)", None, 0.0),
             ("veto breakout",   "breakout", 0.0),
             ("veto support10",  "support10", 0.0),
             ("veto gap -2%",    "gap", 0.02),
             ("veto gap -4%",    "gap", 0.04),
             ("veto gap -6%",    "gap", 0.06)]

    print(f"{'='*104}")
    print(f"  RESULTADO POR MODO DE VETO  (entrada=open, salida=chandelier 4×ATR)")
    print(f"{'='*104}")
    print(f"  {'Modo':<18} {'Entr':>5} {'Veto':>5} {'Veto%':>6} {'Total%':>8} {'CAGR%':>7} "
          f"{'MaxDD%':>8} {'Sharpe':>7} {'Trades':>7} | vetadas fwd20d")
    print(f"  {'-'*102}")
    results = {}
    for label, mode, gap in modes:
        deq, trades, st = run(data, cal, idx_obj, veto_mode=mode, gap_pct=gap)
        s = monthly_stats(deq); results[label] = deq
        diag = (f"med {st['vetoed_median']:+.0f}% · {st['vetoed_runners']} run>+15% / "
                f"{st['vetoed_losers']} perd<-10% (n={st['n_vetoed_fwd']})") if mode else "—"
        print(f"  {label:<18} {st['n_entered']:>5} {st['n_vetoed']:>5} {st['veto_rate']:>5.0f}% "
              f"{s['total']:>+7.0f}% {s['cagr']:>+6.1f}% {s['mdd']:>+7.1f}% {s['sharpe']:>7.2f} "
              f"{len(trades):>7} | {diag}")
    q0, q1 = qqq[0]["c"], qqq[-1]["c"]; yrs = len(cal)/252
    print(f"  {'-'*102}")
    print(f"  {'QQQ b&h':<18} {'':>5} {'':>5} {'':>6} {(q1/q0-1)*100:>+7.0f}% "
          f"{((q1/q0)**(1/yrs)-1)*100:>+6.1f}%")
    print(f"\n  Lectura: si un veto SUBE CAGR/Sharpe o BAJA MaxDD sin matar el total, y sus vetadas")
    print(f"  rinden NEGATIVO fwd20 (más perdedores que runners) -> el filtro te salva. Si recorta")
    print(f"  el total y sus vetadas eran runners -> estabas vetando ganadores -> NO adoptar.")

    # ── robustez por año del mejor candidato conceptual (breakout) vs base ───────
    print(f"\n{'='*104}")
    print(f"  ROBUSTEZ AÑO A AÑO — veto breakout vs base  (¿se sostiene o depende de 1 año?)")
    print(f"{'='*104}")
    by_base = _by_year(results["base (sin veto)"])
    by_brk  = _by_year(results["veto breakout"])
    print(f"  {'Año':<6} {'base ret%':>10} {'veto brk ret%':>14} {'dif':>9}")
    print(f"  {'-'*42}")
    for y in sorted(by_base):
        b, n = by_base[y], by_brk.get(y, 0)
        flag = "  mejor" if n > b else ("  peor" if n < b else "")
        print(f"  {y:<6} {b:>+9.1f}% {n:>+13.1f}% {n-b:>+8.1f}{flag}")
    print()


if __name__ == "__main__":
    main()
