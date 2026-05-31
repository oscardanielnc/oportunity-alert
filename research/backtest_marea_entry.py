#!/usr/bin/env python3
"""
MAREA — ¿ENTRAR EN PULLBACK mejora vs ENTRAR AL OPEN?  + stop "rompe-tesis".

Motivación (Oscar, 2026-05-31): el sistema valida la SEÑAL al cierre y ejecuta al
OPEN del día siguiente. Oscar opera 24/5 en eToro y propone, en vez de pagar el open,
poner una orden LÍMITE en un pullback (EMA20 / retest del breakout / soporte 10d) para
entrar más barato. Intuición sana en reversión — PERO Marea es momentum/breakout, y los
MEJORES trades de momentum no hacen pullback: se van. Esperar el retroceso puede FILTRARTE
fuera de los runners (los que cargan el CAGR) y dentro de los nombres más débiles.

Esto NO se puede asumir: se mide. Este script compara, sobre el MISMO motor validado
(K=5, breakout 50d, chandelier 4×ATR, +macro QQQ, vol sizing), estos modos de ENTRADA:
  - open        : base validada (fill al open de D+1).
  - ema20       : límite en EMA20 (pullback a la media).
  - retest      : límite en el nivel de breakout (max cierre 50d previo) — retest.
  - support10   : límite en el mínimo de lows de 10 días (soporte reciente).
  - nearest     : el nivel más cercano por DEBAJO del cierre (lo que tocaría primero).
La orden límite vive `fill_window` días hábiles; si el precio no la toca, EXPIRA (trade
perdido). Fill realista: si abre por debajo del límite (gap), llena al open (mejor precio);
si no, al límite cuando el low del día lo alcanza.

Métricas clave por modo:
  - fill_rate  : % de señales que efectivamente se llenaron.
  - CAGR/MaxDD/Sharpe del portafolio (captura el coste de quedarte en cash o llenar peor).
  - runner cost: de las señales NO llenadas, retorno fwd 20d entrando al open de D+1
                 (cuánto upside te perdiste por esperar el pullback).

Sección 2: stop "rompe-tesis" (idea de Oscar) sobre la entrada base —
  chandelier solo  vs  chandelier + salir si cierre < soporte20 de la entrada.
  ¿Aporta sobre el chandelier o solo duplica?

Reusa el motor/datos validados. Comisión $1/lado. Sin slippage.
"""
import os, sys, json, statistics
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_portfolio_momentum import (
    fetch_daily, precompute, _close_on, build_calendar, monthly_stats, MOM_LOOKBACK,
)
from backtest_marea_oos import START, CAP, FEE, K, BREAKOUT, MULT, VOL_TARGET, UNIVERSE

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
FILL_WINDOW = 5     # días hábiles que vive la orden límite antes de expirar
SUP_STOP_N  = 20    # ventana del soporte estructural para el stop "rompe-tesis"
WARM = max(200, MOM_LOOKBACK, BREAKOUT) + 1


def precompute_ext(bars, breakout=BREAKOUT):
    """precompute() validado (sma/atr/bkh/mom) + EMA20, soporte 10d y soporte 20d."""
    base = precompute(bars, breakout)
    n = len(bars); closes = [b["c"] for b in bars]; lows = [b["l"] for b in bars]
    ema = [None]*n; sup10 = [None]*n; sup20 = [None]*n
    k = 2/(20+1); e = None
    for i in range(n):
        e = closes[i] if e is None else closes[i]*k + e*(1-k)
        if i >= 19: ema[i] = e
        if i >= 9:  sup10[i] = min(lows[i-9:i+1])
        if i >= SUP_STOP_N-1: sup20[i] = min(lows[i-SUP_STOP_N+1:i+1])
    base.update({"ema20": ema, "sup10": sup10, "sup20": sup20})
    return base


def _limit_level(pre, i, mode, c):
    """Nivel de la orden límite (pullback) para un modo dado. None => no aplicable."""
    if mode == "ema20":   return pre["ema20"][i]
    if mode == "retest":  return pre["bkh"][i]
    if mode == "support10": return pre["sup10"][i]
    if mode == "nearest":
        cands = [pre["ema20"][i], pre["bkh"][i], pre["sup10"][i]]
        below = [v for v in cands if v is not None and v < c]
        if below: return max(below)              # el más cercano por debajo del cierre
        return c * 0.995                          # ya extendido: límite leve bajo el cierre
    return None


def run(data, cal, idx_obj, entry_mode="open", fill_window=FILL_WINDOW,
        exit_mode="chandelier"):
    """
    entry_mode: open|ema20|retest|support10|nearest
    exit_mode : chandelier | support_break (chandelier + cierre < soporte20 de entrada)
    Devuelve (daily_eq, trades, stats_dict).
    """
    cash = CAP; positions = {}; pending = []; daily_eq = []; trades = []
    n_signals = 0; n_filled = 0; missed_fwd = []

    for d in cal:
        # ── 1) salidas ───────────────────────────────────────────────────────────
        to_exit = []
        for tk, p in positions.items():
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None: continue
            b = dd["bars"][i]
            p["hh"] = max(p["hh"], b["h"])
            a = dd["pre"]["atr"][i]
            if a is None: continue
            hit = b["c"] < p["hh"] - MULT*a
            if exit_mode == "support_break" and p.get("sup_stop"):
                hit = hit or (b["c"] < p["sup_stop"])
            if hit and i+1 < len(dd["bars"]):
                to_exit.append((tk, i))
        for tk, i in to_exit:
            dd = data[tk]; xo = dd["bars"][i+1]["o"]; p = positions.pop(tk)
            cash += p["shares"]*xo - FEE
            trades.append({"tk": tk, "pnl_pct": (xo/p["entry"]-1)*100,
                           "pnl_usd": p["shares"]*(xo-p["entry"]) - 2*FEE,
                           "entry_date": p["edate"], "exit_date": dd["bars"][i+1]["t"]})

        # ── 2) fills de órdenes límite pendientes (solo modos pullback) ───────────
        if entry_mode != "open" and pending:
            equity_now = cash + sum(positions[t]["shares"]*_close_on(data[t], d) for t in positions)
            still = []
            for o in sorted(pending, key=lambda x: x["signal_i_cal"]):
                tk = o["tk"]; dd = data[tk]; i = dd["idx"].get(d)
                if i is None:
                    still.append(o); continue
                if i > o["signal_i"] + fill_window:                 # expiró: trade perdido
                    si = o["signal_i"]
                    if si+1 < len(dd["bars"]):
                        eo = dd["bars"][si+1]["o"]
                        hz = min(si+20, len(dd["bars"])-1)
                        missed_fwd.append((dd["bars"][hz]["c"]/eo - 1)*100)
                    continue
                if len(positions) >= K or tk in positions:
                    still.append(o); continue
                b = dd["bars"][i]
                if b["l"] <= o["limit"]:                            # tocó el límite -> fill
                    fill = min(b["o"], o["limit"])                  # gap-down llena al open
                    a = dd["pre"]["atr"][i]; ap = (a/b["c"]) if a else VOL_TARGET
                    base = equity_now/K * (min(1.0, VOL_TARGET/ap) if ap > 0 else 1.0)
                    tgt = min(cash, base)
                    if tgt <= FEE+1:
                        still.append(o); continue
                    sh = (tgt-FEE)/fill
                    cash -= sh*fill + FEE
                    positions[tk] = {"shares": sh, "entry": fill, "hh": b["h"],
                                     "edate": b["t"], "sup_stop": o["sup_stop"]}
                    n_filled += 1
                else:
                    still.append(o)
            pending = still

        # ── 3) entradas nuevas (macro + ranking + top-K + vol sizing) ─────────────
        regime_ok = True
        ii = idx_obj["idx"].get(d)
        if ii is not None and idx_obj["sma"][ii] is not None:
            regime_ok = idx_obj["bars"][ii]["c"] > idx_obj["sma"][ii]
        pend_tks = {o["tk"] for o in pending}
        free = K - len(positions) - (len(pending) if entry_mode != "open" else 0)
        if free > 0 and regime_ok:
            cands = []
            for tk in data:
                if tk in positions or tk in pend_tks: continue
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
                n_signals += 1
                sup_stop = dd["pre"]["sup20"][i]
                if entry_mode == "open":
                    eo = dd["bars"][i+1]["o"]
                    a = dd["pre"]["atr"][i]; ap = (a/c) if a else VOL_TARGET
                    base = equity_now/K * (min(1.0, VOL_TARGET/ap) if ap > 0 else 1.0)
                    tgt = min(cash, base)
                    if tgt <= FEE+1: continue
                    sh = (tgt-FEE)/eo
                    cash -= sh*eo + FEE
                    positions[tk] = {"shares": sh, "entry": eo, "hh": dd["bars"][i+1]["h"],
                                     "edate": dd["bars"][i+1]["t"], "sup_stop": sup_stop}
                    n_filled += 1
                else:
                    lim = _limit_level(dd["pre"], i, entry_mode, c)
                    if lim is None: continue
                    pending.append({"tk": tk, "limit": lim, "signal_i": i,
                                    "signal_i_cal": cal.index(d) if d in cal else i,
                                    "mom": m, "sup_stop": sup_stop})

        eq = cash + sum(positions[tk]["shares"]*_close_on(data[tk], d) for tk in positions)
        daily_eq.append((d, eq))

    stats = {
        "fill_rate": (n_filled/n_signals*100) if n_signals else 0,
        "n_signals": n_signals, "n_filled": n_filled,
        "missed_median": statistics.median(missed_fwd) if missed_fwd else 0,
        "missed_runners": sum(1 for r in missed_fwd if r > 15),
        "n_missed": len(missed_fwd),
    }
    return daily_eq, trades, stats


def _load_universe():
    p = os.path.join(DATA_DIR, "pilot_universe.json")
    if os.path.exists(p):
        try:
            return json.load(open(p))["tickers"]
        except Exception:
            pass
    return UNIVERSE


def main():
    uni = _load_universe()
    print(f"\n{'#'*100}")
    print(f"  MAREA — ENTRADA: open vs pullback  |  {len(uni)} tickers  |  desde 2022  "
          f"|  fill_window={FILL_WINDOW}d")
    print(f"{'#'*100}")
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

    # ── SECCIÓN 1: modos de ENTRADA (salida = chandelier validado) ───────────────
    print(f"{'='*100}")
    print(f"  1) MODO DE ENTRADA  (salida = chandelier 4×ATR, sin cambios)")
    print(f"{'='*100}")
    print(f"  {'Modo':<12} {'Fill%':>6} {'Señales':>8} {'Llenas':>7} {'Total%':>8} {'CAGR%':>7} "
          f"{'MaxDD%':>8} {'Sharpe':>7} {'Trades':>7} | runners perdidos")
    print(f"  {'-'*98}")
    for mode in ["open", "nearest", "ema20", "retest", "support10"]:
        deq, trades, st = run(data, cal, idx_obj, entry_mode=mode)
        s = monthly_stats(deq)
        miss = (f"{st['missed_runners']}/{st['n_missed']} >+15% "
                f"(mediana fwd20 {st['missed_median']:+.0f}%)") if mode != "open" else "—"
        print(f"  {mode:<12} {st['fill_rate']:>5.0f}% {st['n_signals']:>8} {st['n_filled']:>7} "
              f"{s['total']:>+7.0f}% {s['cagr']:>+6.1f}% {s['mdd']:>+7.1f}% {s['sharpe']:>7.2f} "
              f"{len(trades):>7} | {miss}")
    q0, q1 = qqq[0]["c"], qqq[-1]["c"]; yrs = len(cal)/252
    print(f"  {'-'*98}")
    print(f"  {'QQQ b&h':<12} {'':>6} {'':>8} {'':>7} {(q1/q0-1)*100:>+7.0f}% "
          f"{((q1/q0)**(1/yrs)-1)*100:>+6.1f}%")
    print(f"\n  Lectura: si los modos pullback bajan CAGR y/o pierden runners (fwd20 alto en las")
    print(f"  no-llenadas), esperar el retroceso te saca de los ganadores -> NO adoptar como regla.")

    # ── SECCIÓN 2: stop "rompe-tesis" sobre la entrada base ──────────────────────
    print(f"\n{'='*100}")
    print(f"  2) STOP 'ROMPE-TESIS' (entrada=open)  —  chandelier solo vs +cierre<soporte{SUP_STOP_N}d de entrada")
    print(f"{'='*100}")
    print(f"  {'Salida':<22} {'Total%':>8} {'CAGR%':>7} {'MaxDD%':>8} {'Sharpe':>7} {'Trades':>7}")
    print(f"  {'-'*70}")
    for ex, label in [("chandelier", "chandelier (base)"),
                      ("support_break", f"+ rompe-soporte{SUP_STOP_N}")]:
        deq, trades, st = run(data, cal, idx_obj, entry_mode="open", exit_mode=ex)
        s = monthly_stats(deq)
        print(f"  {label:<22} {s['total']:>+7.0f}% {s['cagr']:>+6.1f}% {s['mdd']:>+7.1f}% "
              f"{s['sharpe']:>7.2f} {len(trades):>7}")
    print(f"\n  Lectura: si +rompe-soporte MEJORA MaxDD/Sharpe sin matar CAGR -> vale como seguro.")
    print(f"  Si solo recorta CAGR (sale igual o antes que el chandelier) -> el chandelier ya basta.")

    # ── SECCIÓN 3: robustez de 'nearest' (¿edge real o ruido?) ───────────────────
    def _by_year(deq):
        yr = defaultdict(list)
        for d, e in deq: yr[d[:4]].append(e)
        return {y: (v[-1]/v[0]-1)*100 for y, v in sorted(yr.items())}

    print(f"\n{'='*100}")
    print(f"  3) ROBUSTEZ de 'nearest' vs 'open'  —  ¿la ventaja se sostiene año por año y por ventana?")
    print(f"{'='*100}")
    base_deq, _, _ = run(data, cal, idx_obj, entry_mode="open")
    near_deq, _, _ = run(data, cal, idx_obj, entry_mode="nearest")
    by_base, by_near = _by_year(base_deq), _by_year(near_deq)
    print(f"  {'Año':<6} {'open ret%':>10} {'nearest ret%':>13} {'nearest-open':>13}")
    print(f"  {'-'*44}")
    for y in sorted(by_base):
        b, n = by_base[y], by_near.get(y, 0)
        flag = "  WIN" if n > b else ("  lose" if n < b else "")
        print(f"  {y:<6} {b:>+9.1f}% {n:>+12.1f}% {n-b:>+12.1f}{flag}")
    print(f"\n  Sensibilidad de 'nearest' al fill_window (días que vive la orden límite):")
    print(f"  {'Ventana':<10} {'Fill%':>6} {'CAGR%':>7} {'MaxDD%':>8} {'Sharpe':>7}")
    print(f"  {'-'*42}")
    for fw in (3, 5, 10, 20):
        deq, trades, st = run(data, cal, idx_obj, entry_mode="nearest", fill_window=fw)
        s = monthly_stats(deq)
        print(f"  {str(fw)+'d':<10} {st['fill_rate']:>5.0f}% {s['cagr']:>+6.1f}% "
              f"{s['mdd']:>+7.1f}% {s['sharpe']:>7.2f}")
    print(f"\n  Lectura: si 'nearest' gana en la MAYORÍA de años y es estable entre ventanas, el edge")
    print(f"  es real (vale Fase 2). Si depende de 1 año o de una ventana puntual, es ruido.\n")


if __name__ == "__main__":
    main()
