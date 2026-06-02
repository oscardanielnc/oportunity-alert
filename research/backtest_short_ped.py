#!/usr/bin/env python3
"""
SHORT-PED — espejo bajista del Post-Earnings Drift (E4 "Techo de Expectativas").

Tesis (PEAD bajista): tras una reacción NEGATIVA al earnings de un nombre estirado/sobre-
comprado, el castigo institucional sigue varios días (drift a la baja). Es la misma anomalía
que PED, lado short.

OBJETIVO (criterio de Oscar): NO importa la frecuencia. Importa que sea MEDIBLE, ESTABLE y de
ALTA PRECISION — "cuando se dan las condiciones, funciona si o si". Barremos condiciones; si
ALGUNA configuracion alcanza el bar de confiabilidad, esos son los criterios. Si NINGUNA lo
alcanza, no se usa.

Mecanica: entrada SHORT en Day+2 open; salida Day+5/6/7 close; STOP para acotar la cola de
squeeze. Costos eToro short: 0.3%/lado (0.6% RT) + carry overnight ~0.02%/dia.
Reusa super_backtest.fetch_all (Alpaca daily) + fetch_earnings_events (yfinance).
ASCII only. Uso: python -m research.backtest_short_ped [--fresh]
"""
import os, sys, time, json, statistics as st
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from super_backtest import fetch_all
from backtest_ped import fetch_earnings_events, UNIVERSE

WEEKS = 208            # ~4 anios (bear 2022 incluido)
COMM_RT = 0.6          # 0.3%/lado ida y vuelta
CARRY_DAY = 0.02       # % diario aprox de overnight para short CFD (placeholder conservador)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_short_ped_cache.json")


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]; losses = [abs(min(d, 0.0)) for d in deltas]
    ag = sum(gains[:n])/n; al = sum(losses[:n])/n
    for i in range(n, len(gains)):
        ag = (ag*(n-1)+gains[i])/n; al = (al*(n-1)+losses[i])/n
    if al == 0:
        return 100.0
    return 100 - 100/(1 + ag/al)


def build_events(ticker, bars, events):
    dates = [b["t"][:10] for b in bars]
    idx = {d: i for i, d in enumerate(dates)}
    out = []
    for ev in events:
        E, h = ev["date"], ev["hour"]
        if E not in idx:
            continue
        ei = idx[E]
        r = ei if h == "bmo" else ei + 1          # reaction day (Day+1)
        if r - 1 < 0 or r + 7 >= len(bars) or r < 55:
            continue
        pre = bars[r-1]["c"]
        if pre <= 0:
            continue
        closes_to_r = [b["c"] for b in bars[:r]]
        sma50 = sum(closes_to_r[-50:]) / 50
        rsi_pre = _rsi(closes_to_r[-30:])         # RSI justo antes del earnings
        day1_ret = (bars[r]["c"] - pre)/pre*100
        gap = (bars[r]["o"] - pre)/pre*100
        entry = bars[r+1]["o"]                     # Day+2 open
        if entry <= 0:
            continue
        # camino forward (highs para stop, closes para salida) Day+2..Day+7
        fwd = [{"h": bars[r+1+k]["h"], "c": bars[r+1+k]["c"]} for k in range(0, 7) if r+1+k < len(bars)]
        out.append({
            "ticker": ticker, "date": E,
            "day1_ret": round(day1_ret, 2), "gap": round(gap, 2),
            "rsi_pre": round(rsi_pre, 1),
            "ext_sma50": round((pre - sma50)/sma50*100, 1),   # % sobre SMA50 (extension/hype)
            "entry": entry, "fwd": fwd,
        })
    return out


def short_pnl(ev, exit_k, stop_pct):
    """P&L NETO de un short: entrada Day+2 open, salida en fwd[exit_k] close o stop.
    stop_pct=None => sin stop. Retorna % neto, o None si no hay datos."""
    entry = ev["entry"]; fwd = ev["fwd"]
    if exit_k >= len(fwd):
        return None
    hold = exit_k + 1
    # chequear stop (adverso = precio SUBE contra el short)
    if stop_pct is not None:
        stop_px = entry * (1 + stop_pct/100)
        for k in range(0, exit_k+1):
            if fwd[k]["h"] >= stop_px:
                carry = CARRY_DAY * (k+1)
                return -stop_pct - COMM_RT - carry        # perdida acotada
    exit_px = fwd[exit_k]["c"]
    gross = (entry - exit_px)/entry*100                   # short: gana si baja
    return gross - COMM_RT - CARRY_DAY*hold


def stats(pnls):
    if len(pnls) < 1:
        return None
    wins = sum(1 for p in pnls if p > 0)
    worst10 = sorted(pnls)[:max(1, len(pnls)//10)]
    return {
        "n": len(pnls), "win": round(100*wins/len(pnls), 1),
        "mean": round(st.mean(pnls), 2), "median": round(st.median(pnls), 2),
        "worst": round(min(pnls), 2), "worst10_avg": round(st.mean(worst10), 2),
    }


def load_or_build():
    if os.path.exists(CACHE) and "--fresh" not in sys.argv:
        evs = json.load(open(CACHE, encoding="utf-8"))
        print(f"  (cache: {len(evs)} eventos earnings — usar --fresh para recomputar)")
        return evs
    now = datetime.now(timezone.utc)
    start = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sd = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%d")
    allev = []
    for tk in UNIVERSE:
        bars = fetch_all(tk, "1Day", start, limit=2000)
        if len(bars) < 60:
            print(f"  {tk:<6} sin datos"); continue
        evs = fetch_earnings_events(tk, sd, sd)
        time.sleep(0.15)
        e = build_events(tk, bars, evs)
        allev += e
        print(f"  {tk:<6} {len(evs)} earnings -> {len(e)} eventos")
    json.dump(allev, open(CACHE, "w", encoding="utf-8"))
    return allev


# ── Definicion de configuraciones a barrer ────────────────────────────────────
def matches(ev, react_max, rsi_min, ext_min):
    return (ev["day1_ret"] <= react_max
            and ev["rsi_pre"] >= rsi_min
            and ev["ext_sma50"] >= ext_min)


def run():
    print(f"\n{'#'*96}\n  SHORT-PED — espejo bajista de PED (E4) | {WEEKS} sem | {len(UNIVERSE)} nombres\n"
          f"  Criterio: ALTA PRECISION + ESTABILIDAD (no frecuencia). Barrido de condiciones.\n{'#'*96}")
    allev = load_or_build()
    print(f"  Total eventos earnings: {len(allev)}")

    # split temporal para estabilidad (mitad 1 vs mitad 2 por fecha)
    allev_sorted = sorted(allev, key=lambda e: e["date"])
    mid = len(allev_sorted)//2
    cutoff = allev_sorted[mid]["date"] if allev_sorted else "2024-01-01"

    # Barrido: reaccion Day+1, RSI pre, extension sobre SMA50, exit horizon, stop
    react_opts = [-3, -5, -8]
    rsi_opts   = [0, 65, 72]
    ext_opts   = [0, 10, 25]
    exit_opts  = [(3, "D+5"), (4, "D+6"), (5, "D+7")]
    stop_opts  = [None, 8, 12]

    print(f"\n{'cond (react/rsi/ext)':<22}{'exit':>5}{'stop':>6}{'n':>5}{'win%':>6}{'mean':>7}{'med':>7}{'worst':>8}{'w10avg':>8}  estable?")
    print("-"*96)

    winners = []
    for rmax in react_opts:
        for rsi_m in rsi_opts:
            for ext_m in ext_opts:
                sub = [e for e in allev if matches(e, rmax, rsi_m, ext_m)]
                if len(sub) < 12:
                    continue
                for ek, elab in exit_opts:
                    for stop in stop_opts:
                        pnls = [p for p in (short_pnl(e, ek, stop) for e in sub) if p is not None]
                        s = stats(pnls)
                        if not s or s["n"] < 12:
                            continue
                        # estabilidad: misma config en ambas mitades temporales
                        h1 = [p for p in (short_pnl(e, ek, stop) for e in sub if e["date"] < cutoff) if p is not None]
                        h2 = [p for p in (short_pnl(e, ek, stop) for e in sub if e["date"] >= cutoff) if p is not None]
                        s1, s2 = stats(h1), stats(h2)
                        stable = (s1 and s2 and s1["n"] >= 5 and s2["n"] >= 5
                                  and s1["mean"] > 0 and s2["mean"] > 0
                                  and s1["win"] >= 60 and s2["win"] >= 60)
                        # bar de confiabilidad
                        good = (s["win"] >= 70 and s["mean"] > 0 and s["median"] > 0 and stable)
                        cond = f"r<={rmax} rsi>={rsi_m} x>={ext_m}"
                        st_flag = "SI" if stable else "."
                        mark = "  <== PASA" if good else ""
                        print(f"{cond:<22}{elab:>5}{str(stop):>6}{s['n']:>5}{s['win']:>6}"
                              f"{s['mean']:>+7.1f}{s['median']:>+7.1f}{s['worst']:>+8.1f}{s['worst10_avg']:>+8.1f}  {st_flag}{mark}")
                        if good:
                            winners.append((cond, elab, stop, s, s1, s2))

    print("\n" + "="*96)
    if winners:
        print(f"  CONFIGURACIONES QUE PASAN EL BAR (win>=70%, mean&median>0, estable ambas mitades): {len(winners)}")
        for cond, elab, stop, s, s1, s2 in winners:
            print(f"   * {cond} | {elab} stop={stop} -> n={s['n']} win={s['win']}% "
                  f"mean{s['mean']:+.1f}% (H1 {s1['win']}%/{s1['mean']:+.1f} | H2 {s2['win']}%/{s2['mean']:+.1f}) worst10avg{s['worst10_avg']:+.1f}%")
    else:
        print("  NINGUNA configuracion alcanza el bar de confiabilidad -> SHORT-PED NO se adopta.")
    print("="*96 + "\n")


if __name__ == "__main__":
    run()
