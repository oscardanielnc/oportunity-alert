#!/usr/bin/env python3
"""
CAÑONAZO corregido — trigger por GAP (causalmente válido), NO por day1_ret (lookahead).

El backtest_earnings_intraday original disparaba "react o->c" filtrando por day1_ret
(= close del día de reacción) y operaba open->close de ESE MISMO día → lookahead: usaba el
cierre para decidir la entrada del open. Aquí el trigger es el GAP de apertura
(open[r] vs cierre previo), que SÍ se conoce al open. La jugada (react_oc = open->close del
día de reacción) es idéntica; solo cambia el disparador a uno honesto.

Reusa el cache de eventos de backtest_earnings_intraday (_earn_intraday_cache.json), que ya
trae gap y react_oc por evento. Mismo bar de confiabilidad (win>=70, mean/med>0, estable por
mitades) y mismas fees (0.6% RT eToro). ASCII only.
  python -m research.backtest_canon_gap   (requiere el cache; si no, correr antes el intraday)
"""
import os, sys, json, statistics as st

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_earn_intraday_cache.json")
COMM_RT = 0.6


def stats(pnls):
    if len(pnls) < 1:
        return None
    wins = sum(1 for p in pnls if p > 0)
    return {"n": len(pnls), "win": round(100*wins/len(pnls), 1),
            "mean": round(st.mean(pnls), 2), "median": round(st.median(pnls), 2),
            "worst": round(min(pnls), 2)}


def evaluate(name, allev, cutoff, sub_filter, side):
    sub = [e for e in allev if sub_filter(e)]
    if len(sub) < 12:
        print(f"  {name:<48} n={len(sub):<3} (insuficiente)"); return False
    pnl = [side*e["react_oc"] - COMM_RT for e in sub]
    s = stats(pnl)
    h1 = [side*e["react_oc"]-COMM_RT for e in sub if e["date"] < cutoff]
    h2 = [side*e["react_oc"]-COMM_RT for e in sub if e["date"] >= cutoff]
    s1, s2 = stats(h1), stats(h2)
    stable = (s1 and s2 and s1["n"] >= 5 and s2["n"] >= 5 and s1["mean"] > 0 and s2["mean"] > 0
              and s1["win"] >= 60 and s2["win"] >= 60)
    good = s["win"] >= 70 and s["mean"] > 0 and s["median"] > 0 and stable
    print(f"  {name:<48} n={s['n']:<3} win={s['win']:>5}%  mean{s['mean']:>+6.2f}  "
          f"med{s['median']:>+6.2f}  worst{s['worst']:>+7.1f}  estable={'SI' if stable else '.'}"
          f"{'   <== PASA' if good else ''}")
    return good


def run():
    if not os.path.exists(CACHE):
        print("[ERROR] falta el cache. Corré antes: python -m research.backtest_earnings_intraday"); return
    allev = sorted(json.load(open(CACHE, encoding="utf-8")), key=lambda e: e["date"])
    print(f"\n{'#'*100}\n  CAÑONAZO por GAP (causal) — neto fees 0.6% | bar: win>=70 + media/mediana>0 + estable\n{'#'*100}")
    print(f"  eventos: {len(allev)}")
    mid = len(allev)//2
    cutoff = allev[mid]["date"] if allev else "2024-01-01"
    passes = []

    print("\n  -- CONTINUACION LONG: gap UP fuerte -> comprar OPEN, vender CLOSE (sigue subiendo?) --")
    for thr in (2, 3, 5, 8):
        passes.append(evaluate(f"react o->c LONG | gap>=+{thr}%", allev, cutoff,
                               lambda e, t=thr: e["gap"] >= t, side=1))

    print("\n  -- CONTINUACION SHORT: gap DOWN fuerte -> vender OPEN, cubrir CLOSE (sigue cayendo?) --")
    for thr in (2, 3, 5, 8):
        passes.append(evaluate(f"react o->c SHORT | gap<=-{thr}%", allev, cutoff,
                               lambda e, t=thr: e["gap"] <= -t, side=-1))

    print("\n  -- FADE LONG: gap DOWN fuerte -> comprar OPEN, vender CLOSE (rebota intradia?) --")
    for thr in (2, 3, 5, 8):
        passes.append(evaluate(f"react o->c FADE-LONG | gap<=-{thr}%", allev, cutoff,
                               lambda e, t=thr: e["gap"] <= -t, side=1))

    print("\n  -- FADE SHORT: gap UP fuerte -> vender OPEN, cubrir CLOSE (se desinfla intradia?) --")
    for thr in (2, 3, 5, 8):
        passes.append(evaluate(f"react o->c FADE-SHORT | gap>=+{thr}%", allev, cutoff,
                               lambda e, t=thr: e["gap"] >= t, side=-1))

    print("\n" + "="*100)
    if any(passes):
        print(f"  {sum(passes)} configuracion(es) PASAN el bar -> hay un cañonazo causal real (a revisar OOS).")
    else:
        print("  NINGUNA config causal (gap-triggered) pasa el bar -> el cañonazo NO sobrevive sin lookahead.")
    print("="*100 + "\n")


if __name__ == "__main__":
    run()
