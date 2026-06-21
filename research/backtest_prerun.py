#!/usr/bin/env python3
"""
PRE-RUN-UP (pre-posicionamiento de earnings) — ¿hay drift de ANTICIPACION antes del reporte?

Tesis (pre-earnings announcement drift): la acción deriva al alza en los días PREVIOS al
earnings; se entra K días antes y se SALE en el último cierre ANTES del reporte (cero
exposición al evento binario).

Disciplina (igual que el resto):
  - CAUSAL: salida = último cierre antes del reporte. amc en E -> sale close[E] (reporte tras el
    cierre); bmo en E -> sale close[E-1] (reporte antes del open). Entrada = close[exit-K].
  - EXCESS vs QQQ: el retorno crudo de un hold LONG multi-día sube por beta del mercado; se mide
    el EXCESO sobre QQQ en la misma ventana para no confundir drift de earnings con bull genérico.
  - NETO de fees (COMM_RT) + OOS (estabilidad por mitades) + variante con filtro de tendencia (SMA50).
  - Bar: win>=55, media y mediana de EXCESS neto >0, y estable (ambas mitades media>0 y win>=52).

Reusa super_backtest.fetch_all (Alpaca) + backtest_ped.fetch_earnings_events (yfinance) + UNIVERSE.
ASCII only. Uso: python -m research.backtest_prerun [--fresh] [--fee 0.3]
"""
import os, sys, time, json, statistics as st
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from super_backtest import fetch_all
from backtest_ped import fetch_earnings_events, UNIVERSE

WEEKS = 208
KS = (1, 2, 3, 5, 10)                       # días hábiles de hold antes del reporte
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_prerun_cache.json")


def _fee():
    if "--fee" in sys.argv:
        try: return float(sys.argv[sys.argv.index("--fee") + 1])
        except Exception: pass
    return 0.3                              # mega-caps líquidas, hold multi-día: spread ~0.15%/lado


def _sma(bars, i, n=50):
    if i - n + 1 < 0:
        return None
    return sum(b["c"] for b in bars[i-n+1:i+1]) / n


def build_events(ticker, bars, events, qmap):
    dates = [b["t"][:10] for b in bars]
    idx = {d: i for i, d in enumerate(dates)}
    out = []
    for ev in events:
        E, h = ev["date"], ev["hour"]
        if E not in idx:
            continue
        ei = idx[E]
        exit_i = ei if h == "amc" else ei - 1     # último cierre ANTES del reporte
        if exit_i < 55 or exit_i >= len(bars):
            continue
        rec = {"ticker": ticker, "date": E, "hour": h, "K": {}}
        ex_d = dates[exit_i]; ex_p = bars[exit_i]["c"]
        ex_q = qmap.get(ex_d)
        for K in KS:
            en_i = exit_i - K
            if en_i < 50:
                continue
            en_d = dates[en_i]; en_p = bars[en_i]["c"]
            en_q = qmap.get(en_d)
            if en_p <= 0 or ex_p <= 0 or not en_q or not ex_q:
                continue
            ret = (ex_p - en_p) / en_p * 100
            qret = (ex_q - en_q) / en_q * 100
            sma = _sma(bars, en_i, 50)
            rec["K"][str(K)] = {"ret": round(ret, 3), "excess": round(ret - qret, 3),
                                "trend": bool(sma and en_p > sma)}
        if rec["K"]:
            out.append(rec)
    return out


def load_or_build():
    if os.path.exists(CACHE) and "--fresh" not in sys.argv:
        return json.load(open(CACHE, encoding="utf-8"))
    now = datetime.now(timezone.utc)
    start = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sd = (now - timedelta(weeks=WEEKS)).strftime("%Y-%m-%d")
    qbars = fetch_all("QQQ", "1Day", start, limit=2000)
    qmap = {b["t"][:10]: b["c"] for b in qbars}
    print(f"  QQQ barras: {len(qbars)}")
    allev = []
    for tk in UNIVERSE:
        bars = fetch_all(tk, "1Day", start, limit=2000)
        if len(bars) < 60:
            continue
        evs = fetch_earnings_events(tk, sd, sd)
        time.sleep(0.15)
        ev = build_events(tk, bars, evs, qmap)
        allev += ev
        print(f"  {tk:<6} {len(evs)} earnings -> {len(ev)} usables")
    json.dump(allev, open(CACHE, "w", encoding="utf-8"))
    return allev


def stats(pnls):
    if not pnls:
        return None
    wins = sum(1 for p in pnls if p > 0)
    return {"n": len(pnls), "win": round(100*wins/len(pnls), 1),
            "mean": round(st.mean(pnls), 3), "median": round(st.median(pnls), 3),
            "worst": round(min(pnls), 2)}


def evaluate(name, rows, cutoff, K, fee, trend_only):
    sub = []
    for e in rows:
        d = e["K"].get(str(K))
        if not d:
            continue
        if trend_only and not d["trend"]:
            continue
        sub.append((e["date"], d))
    if len(sub) < 12:
        print(f"  {name:<40} n={len(sub):<3} (insuficiente)"); return False
    net = [d["excess"] - fee for _, d in sub]
    raw = [d["ret"] - fee for _, d in sub]
    s = stats(net); sr = stats(raw)
    h1 = [d["excess"]-fee for dt, d in sub if dt < cutoff]
    h2 = [d["excess"]-fee for dt, d in sub if dt >= cutoff]
    s1, s2 = stats(h1), stats(h2)
    stable = (s1 and s2 and s1["n"] >= 5 and s2["n"] >= 5 and s1["mean"] > 0 and s2["mean"] > 0
              and s1["win"] >= 52 and s2["win"] >= 52)
    good = s["win"] >= 55 and s["mean"] > 0 and s["median"] > 0 and stable
    print(f"  {name:<40} n={s['n']:<3} win={s['win']:>5}%  exc{s['mean']:>+6.2f}  "
          f"med{s['median']:>+6.2f}  (crudo{sr['mean']:>+6.2f})  estable={'SI' if stable else '.'}"
          f"{'   <== PASA' if good else ''}")
    return good


def run():
    fee = _fee()
    print(f"\n{'#'*104}\n  PRE-RUN-UP (pre-posicionamiento) — EXCESS vs QQQ, neto fee {fee}% RT | "
          f"bar: win>=55 + exc media/med>0 + estable\n{'#'*104}")
    allev = sorted(load_or_build(), key=lambda e: e["date"])
    print(f"  eventos usables: {len(allev)}")
    if not allev:
        return
    cutoff = allev[len(allev)//2]["date"]
    passes = []
    print("\n  -- LONG pre-run-up (TODOS): comprar K días antes, vender último cierre pre-reporte --")
    for K in KS:
        passes.append(evaluate(f"K={K}d antes  (todos)", allev, cutoff, K, fee, trend_only=False))
    print("\n  -- LONG pre-run-up (solo TENDENCIA: close>SMA50 en la entrada) --")
    for K in KS:
        passes.append(evaluate(f"K={K}d antes  (tendencia)", allev, cutoff, K, fee, trend_only=True))

    print("\n" + "="*104)
    if any(passes):
        print(f"  {sum(passes)} config(s) PASAN el bar -> pre-run-up tiene edge causal/excess (revisar tamaño + OOS).")
    else:
        print("  NINGUNA config pasa el bar -> el pre-run-up NO tiene edge tradeable (excess neto de fees).")
    print("="*104 + "\n")


if __name__ == "__main__":
    run()
