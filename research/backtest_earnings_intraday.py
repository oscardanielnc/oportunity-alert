#!/usr/bin/env python3
"""
EARNINGS INTRADIA — ¿algun patron intradia de earnings (tipo "El Cañonazo") es CONFIABLE
y reproducible neto de fees? Mismo bar que el resto: alta precision + estabilidad, NO frecuencia.

El Cañonazo (E1): tras un beat fuerte, el dia de reaccion hace "resaca" (dip) y RECUPERA hacia
el cierre / al dia siguiente. Nucleo MECANICO medible con barras diarias (OHLC):
  - reaction-day open->close (LONG): comprar al open del dia del cañonazo, vender al close
  - Day+2 open->close (LONG): comprar el "suelo" del Day+2, vender mismo dia
  - reaction-day open->close (SHORT) en MISS fuerte: el cañonazo bajista

Trades de 1 dia (o overnight) -> fee eToro 0.6% ida/vuelta, sin carry. Condicionado a la
fuerza del beat/miss + extension. Reusa fetch_all (Alpaca) + fetch_earnings_events (yfinance).
ASCII only. Uso: python -m research.backtest_earnings_intraday [--fresh]
"""
import os, sys, time, json, statistics as st
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from super_backtest import fetch_all
from backtest_ped import fetch_earnings_events, UNIVERSE

WEEKS = 208
COMM_RT = 0.6
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_earn_intraday_cache.json")


def build_events(ticker, bars, events):
    dates = [b["t"][:10] for b in bars]
    idx = {d: i for i, d in enumerate(dates)}
    out = []
    for ev in events:
        E, h = ev["date"], ev["hour"]
        if E not in idx:
            continue
        ei = idx[E]
        r = ei if h == "bmo" else ei + 1      # reaction day (Day+1)
        if r - 1 < 1 or r + 2 >= len(bars):
            continue
        pre = bars[r-1]["c"]
        if pre <= 0 or bars[r]["o"] <= 0 or bars[r+1]["o"] <= 0:
            continue
        day1_ret = (bars[r]["c"] - pre)/pre*100              # reaccion (close vs pre)
        out.append({
            "ticker": ticker, "date": E, "day1_ret": round(day1_ret, 2),
            "gap": round((bars[r]["o"] - pre)/pre*100, 2),
            "react_oc": round((bars[r]["c"] - bars[r]["o"])/bars[r]["o"]*100, 2),  # reaction open->close
            "react_lc": round((bars[r]["c"] - bars[r]["l"])/bars[r]["l"]*100, 2),  # low->close (rebote max posible)
            "d2_oc": round((bars[r+1]["c"] - bars[r+1]["o"])/bars[r+1]["o"]*100, 2),  # Day+2 open->close
        })
    return out


def stats(pnls):
    if len(pnls) < 1:
        return None
    wins = sum(1 for p in pnls if p > 0)
    worst10 = sorted(pnls)[:max(1, len(pnls)//10)]
    return {"n": len(pnls), "win": round(100*wins/len(pnls), 1),
            "mean": round(st.mean(pnls), 2), "median": round(st.median(pnls), 2),
            "worst": round(min(pnls), 2), "worst10": round(st.mean(worst10), 2)}


def load_or_build():
    if os.path.exists(CACHE) and "--fresh" not in sys.argv:
        return json.load(open(CACHE, encoding="utf-8"))
    now = datetime.now(timezone.utc)
    start = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sd = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%d")
    allev = []
    for tk in UNIVERSE:
        bars = fetch_all(tk, "1Day", start, limit=2000)
        if len(bars) < 60:
            continue
        evs = fetch_earnings_events(tk, sd, sd)
        time.sleep(0.15)
        allev += build_events(tk, bars, evs)
        print(f"  {tk:<6} {len(evs)} earnings")
    json.dump(allev, open(CACHE, "w", encoding="utf-8"))
    return allev


def evaluate(name, trades_pnl, allev_sorted, cutoff, sub_filter, field, side=1):
    """trades_pnl: lista de pnl netos. Estabilidad por mitades. Imprime + bar."""
    sub = [e for e in allev_sorted if sub_filter(e)]
    if len(sub) < 12:
        print(f"  {name:<46} n={len(sub):<3} (insuficiente)"); return False
    pnl = [side*e[field] - COMM_RT for e in sub]
    s = stats(pnl)
    h1 = [side*e[field]-COMM_RT for e in sub if e["date"] < cutoff]
    h2 = [side*e[field]-COMM_RT for e in sub if e["date"] >= cutoff]
    s1, s2 = stats(h1), stats(h2)
    stable = (s1 and s2 and s1["n"] >= 5 and s2["n"] >= 5 and s1["mean"] > 0 and s2["mean"] > 0
              and s1["win"] >= 60 and s2["win"] >= 60)
    good = s["win"] >= 70 and s["mean"] > 0 and s["median"] > 0 and stable
    print(f"  {name:<46} n={s['n']:<3} win={s['win']:>5}%  mean{s['mean']:>+6.2f}  "
          f"med{s['median']:>+6.2f}  worst{s['worst']:>+7.1f}  estable={'SI' if stable else '.'}"
          f"{'   <== PASA' if good else ''}")
    return good


def run():
    print(f"\n{'#'*98}\n  EARNINGS INTRADIA (Cañonazo y variantes) — neto fees 0.6% | bar: precision+estabilidad\n{'#'*98}")
    allev = load_or_build()
    allev = sorted(allev, key=lambda e: e["date"])
    print(f"  eventos: {len(allev)}")
    mid = len(allev)//2
    cutoff = allev[mid]["date"] if allev else "2024-01-01"

    print(f"\n  -- LONG: comprar OPEN del dia de reaccion, vender al CLOSE (el 'cañonazo' recupera intradia?) --")
    passes = []
    for thr in (3, 5, 8):
        passes.append(evaluate(f"react o->c | beat dia1>=+{thr}%", None, allev, cutoff,
                               lambda e, t=thr: e["day1_ret"] >= t, "react_oc", side=1))

    print(f"\n  -- LONG: comprar OPEN Day+2 (el 'suelo'), vender al CLOSE Day+2 --")
    for thr in (3, 5, 8):
        passes.append(evaluate(f"D+2 o->c | beat dia1>=+{thr}%", None, allev, cutoff,
                               lambda e, t=thr: e["day1_ret"] >= t, "d2_oc", side=1))

    print(f"\n  -- SHORT: vender OPEN del dia de reaccion, cubrir al CLOSE (cañonazo BAJISTA en miss) --")
    for thr in (3, 5, 8):
        passes.append(evaluate(f"react o->c SHORT | miss dia1<=-{thr}%", None, allev, cutoff,
                               lambda e, t=thr: e["day1_ret"] <= -t, "react_oc", side=-1))

    # referencia: cuanto rebote HAY (low->close) en los beats — techo teorico no-temporizable
    beats = [e for e in allev if e["day1_ret"] >= 5]
    if beats:
        s = stats([e["react_lc"] for e in beats])
        print(f"\n  (referencia) rebote low->close en beats>=+5%: mediana {s['median']:+.1f}% "
              f"(NO temporizable: no sabes el minimo en vivo)")

    print("\n" + "="*98)
    if any(passes):
        print(f"  {sum(passes)} configuracion(es) PASAN el bar de confiabilidad.")
    else:
        print("  NINGUN patron intradia de earnings alcanza el bar -> NO se adoptan.")
    print("="*98 + "\n")


if __name__ == "__main__":
    run()
