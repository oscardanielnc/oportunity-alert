import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
ESTUDIO B — BREAKOUTS FRESCOS / CASO SNOW
=========================================
Pregunta (pendiente acordado 2026-06-01): los breakouts recientes con fuerza 6m BAJA (estilo
SNOW: +77% en 10d pero momentum 126d solo +12% → invisible en el top-10 de líderes) ¿son
comprables sistemáticamente, o son solo RADAR? Hipótesis: NO robusto (los pops revierten; el
edge del momentum vive en la PERSISTENCIA de 6m, no en el pop de 2 días).

Reforzada por el Estudio A: rotar al líder más fuerte colapsa en el bear → un breakout de
fuerza-6m-baja probablemente revierte aún más. Pero se mide a fondo, con las MISMAS reglas.

Aprendizaje de A aplicado: datos DESDE 2020 (2022 = bear real operable). Medir en sample
sesgado-al-bull (desde 2022) es justo el error que A desenmascaró.

DOS partes:
  B1 — EVENT STUDY (sin operar): cada ruptura (primer toque) se clasifica en
       STRONG (líder top-10 por mom126, lo que Marea YA compra) vs FRESH (ruptura fuera del
       top-10) vs SNOW (ruptura con mom126 en el tercil bajo Y ret_10d en la mitad alta).
       Mide retorno futuro EN EXCESO de QQQ a +1/+5/+10/+20/+40 días. Responde "¿revierten?".
  B2 — SLEEVE OPERABLE: estrategia standalone de breakouts frescos (gate=fresh, rank=ret_10d,
       salida chandelier/tiempo), neta de fees, open+1, vs BASE Marea y QQQ. Incluye stress 2022.

BARRA DE DECISIÓN (declarada de antemano): "COMPRABLE" solo si el exceso forward es POSITIVO y
robusto año-a-año Y el sleeve tiene Sharpe/MaxDD comparable a Marea incl. 2022. Si no → RADAR.

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-01) — VEREDICTO: RADAR, NO COMPRA. Hipótesis confirmada.

B1 EVENT STUDY (exceso vs QQQ tras la ruptura, primer toque, datos desde 2020):
  jerarquía clara STRONG >> SNOW > FRESH a todos los horizontes:
    +20d: STRONG +6.8% | SNOW +2.5% | FRESH +1.7%      +40d: STRONG +9.2% | SNOW +5.8% | FRESH +3.6%
  El pop NO se invierte de golpe (exceso positivo), pero es 1/4–1/2 del de los LÍDERES que Marea
  ya compra. El edge vive en la PERSISTENCIA de 6m, no en el pop reciente.
  Año-a-año (+20d): STRONG positivo casi siempre; SNOW POSITIVO en bull pero NEGATIVO en 2022
  (−1.2%) y 2023 (−0.5%) → revierte cuando baja la marea. NO robusto.

B2 SLEEVE OPERABLE (gate=fresh, rank=ret_10d, bear-inclusive):
    BASE Marea           : CAGR +35.8% Sharpe 1.35 MaxDD −23.8%
    Fresh chandelier     : CAGR +32.3% Sharpe 1.15 MaxDD −37.8%  ← peor en todo
    Fresh + horiz 20/40d : Sharpe 1.15 / 0.98, 2022 −23% / −27%  ← sufre en el bear
    Fresh chand mult 3.0 : CAGR +31.7% Sharpe 1.35 MaxDD −24.6%  ← su MEJOR caso solo IGUALA a
                           Marea con MENOS retorno; nunca la bate.
  Conclusión: el sleeve fresco rinde menos riesgo-ajustado y sufre más en 2022. El panel 🆕 se
  queda como RADAR/vigilancia. Si Oscar tradea uno discrecional: stop ANGOSTO (3×ATR), tamaño
  chico, sabiendo que es unvalidated y que revierte en regímenes malos.

REPRODUCIR: python backtest_fresh_breakouts.py
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
import os, statistics
from collections import defaultdict

from backtest_marea_selection import (
    load_data, run, base_cfg, perf, year_perf, summarize,
    K, MULT, WARM, BREAKOUT, MOMS, CAP, FEE, VOL_TARGET,
)
from backtest_portfolio_momentum import build_calendar

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
START_S = "2020-01-01T00:00:00Z"
CACHE_S = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selection_cache_2020.json")
HORIZONS = [1, 5, 10, 20, 40]
import json


# ── B1: EVENT STUDY ───────────────────────────────────────────────────────────
def event_study(data, qqq, cal):
    qclose = {b["t"]: b["c"] for b in qqq}

    # clasificación cross-seccional por día: top-10 por mom126 y terciles de mom126/ret10 entre breakouts
    strong_by_day, m126_lowtercile_by_day, ret10_hialf_by_day = {}, {}, {}
    for d in cal:
        elig = []          # (mom126, tk) above_sma
        brk = []           # (tk, mom126, ret10) breakouts above_sma
        for tk in data:
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None or i < WARM: continue
            c = dd["bars"][i]["c"]; sma = dd["pre"]["sma"][i]; bkh = dd["pre"]["bkh"][i]
            m126 = dd["pre"]["mom"][126][i]; r10 = dd["pre"]["mom"][10][i]
            if not (sma and c > sma and m126 is not None): continue
            elig.append((m126, tk))
            if bkh and c >= bkh and r10 is not None:
                brk.append((tk, m126, r10))
        elig.sort(reverse=True)
        strong_by_day[d] = {tk for _, tk in elig[:10]}
        if brk:
            ms = sorted(b[1] for b in brk)                       # mom126 de breakouts
            r10s = sorted(b[2] for b in brk)
            m126_lowtercile_by_day[d] = ms[max(0, len(ms)//3 - 1)]   # umbral tercil bajo
            ret10_hialf_by_day[d] = r10s[len(r10s)//2]               # mediana ret10
        else:
            m126_lowtercile_by_day[d] = None; ret10_hialf_by_day[d] = None

    # recolectar episodios (primer toque) por bucket
    rows = {b: {h: [] for h in HORIZONS} for b in ("STRONG", "FRESH", "SNOW", "ALL")}
    rows_year = {b: defaultdict(list) for b in ("STRONG", "FRESH", "SNOW")}   # exceso a h=20 por año
    counts = defaultdict(int)
    for tk in data:
        dd = data[tk]; bars = dd["bars"]; pre = dd["pre"]
        for i in range(WARM, len(bars)):
            d = bars[i]["t"]; c = bars[i]["c"]
            sma = pre["sma"][i]; bkh = pre["bkh"][i]; m126 = pre["mom"][126][i]; r10 = pre["mom"][10][i]
            if not (sma and c > sma and bkh and c >= bkh and m126 is not None): continue
            # primer toque: ayer NO era breakout
            pbkh = pre["bkh"][i-1]; pc = bars[i-1]["c"]
            if pbkh is not None and pc >= pbkh: continue
            qd = qclose.get(d)
            if qd is None: continue
            is_strong = tk in strong_by_day.get(d, set())
            lowt = m126_lowtercile_by_day.get(d); hialf = ret10_hialf_by_day.get(d)
            is_snow = (lowt is not None and m126 <= lowt and r10 is not None and hialf is not None and r10 >= hialf)
            buckets = ["ALL"] + (["STRONG"] if is_strong else ["FRESH"]) + (["SNOW"] if is_snow else [])
            for b in buckets: counts[b] += 1
            for h in HORIZONS:
                if i + h >= len(bars): continue
                qf = qclose.get(bars[i+h]["t"])
                if qf is None: continue
                exc = (bars[i+h]["c"]/c - 1) - (qf/qd - 1)
                for b in buckets: rows[b][h].append(exc)
            if i + 20 < len(bars):
                qf = qclose.get(bars[i+20]["t"])
                if qf is not None:
                    exc20 = (bars[i+20]["c"]/c - 1) - (qf/qd - 1)
                    for b in buckets:
                        if b != "ALL": rows_year[b][d[:4]].append(exc20)

    print(f"\n{'='*92}\n  B1 — EVENT STUDY: retorno EN EXCESO de QQQ tras la ruptura (primer toque, %)\n{'='*92}")
    print(f"  Episodios: STRONG={counts['STRONG']} (líderes, lo que Marea compra) | "
          f"FRESH={counts['FRESH']} (fuera del top-10) | SNOW={counts['SNOW']} (6m bajo + 10d alto)\n")
    print(f"  {'Bucket':<8} {'Horizonte':<6} {'media%':>8} {'mediana%':>9} {'hit-rate':>9} {'N':>6}")
    print(f"  {'-'*54}")
    for b in ("STRONG", "FRESH", "SNOW", "ALL"):
        for h in HORIZONS:
            xs = rows[b][h]
            if not xs: continue
            mean = statistics.mean(xs)*100; med = statistics.median(xs)*100
            hit = sum(1 for x in xs if x > 0)/len(xs)*100
            print(f"  {b:<8} +{h:<5} {mean:>+7.1f}% {med:>+8.1f}% {hit:>7.0f}% {len(xs):>6}")
        print(f"  {'-'*54}")

    print(f"\n  EXCESO a +20d POR AÑO (¿el pop persiste en cada régimen, o es de un año?):")
    years = sorted({y for b in rows_year for y in rows_year[b]})
    print(f"  {'Bucket':<8} " + " ".join(f"{y:>8}" for y in years))
    for b in ("STRONG", "FRESH", "SNOW"):
        cells = []
        for y in years:
            xs = rows_year[b].get(y, [])
            cells.append(f"{statistics.mean(xs)*100:>+6.1f}%" if xs else "   n/a ")
        print(f"  {b:<8} " + " ".join(cells))
    print(f"\n  Lectura: si FRESH/SNOW tienen exceso < STRONG (o negativo), el pop REVIERTE y el edge")
    print(f"  está en la persistencia de 6m (lo que Marea ya captura) → los 🆕 son RADAR, no compra.\n")


# ── B2: SLEEVE OPERABLE ───────────────────────────────────────────────────────
def sleeve(data, idx_obj, cal, qqq):
    print(f"\n{'='*100}\n  B2 — SLEEVE OPERABLE de breakouts frescos (gate=fresh, rank=ret_10d) | datos desde 2020\n{'='*100}")
    variants = [
        ("BASE Marea (referencia)",        base_cfg()),
        ("Fresh chandelier (macro ON)",    base_cfg(gate="fresh", rank="mom10")),
        ("Fresh chandelier (macro OFF)",   base_cfg(gate="fresh", rank="mom10", macro=False)),
        ("Fresh + horizonte 20d",          base_cfg(gate="fresh", rank="mom10", exits={"chandelier", "time"}, hold=20)),
        ("Fresh + horizonte 40d",          base_cfg(gate="fresh", rank="mom10", exits={"chandelier", "time"}, hold=40)),
        ("Fresh chand mult 3.0",           base_cfg(gate="fresh", rank="mom10", mult=3.0)),
    ]
    print(f"  {'Variante':<30} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'PeorMes':>7} {'2022':>7} {'WR%':>4} {'Trades':>6} {'Hold':>5}")
    print(f"  {'-'*92}")
    for name, cfg in variants:
        res = run(data, cal, cfg, idx_obj); s = summarize(res)
        y22 = year_perf(res["deq"], "2022")
        r22 = f"{y22['total']:>+5.0f}%" if y22 else "  n/a "
        print(f"  {name:<30} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {s['worst_m']:>+6.1f}% "
              f"{r22:>7} {s['wr']:>3.0f}% {s['trades']:>6} {s['avg_hold']:>5.0f}")
    qdeq = [(b["t"], CAP*b["c"]/qqq[0]["c"]) for b in qqq if b["t"] >= cal[0]]
    sb = perf(qdeq)
    print(f"  {'-'*92}")
    print(f"  {'QQQ buy&hold':<30} {sb['cagr']:>+5.1f}% {sb['sharpe']:>6.2f} {sb['mdd']:>+6.1f}% {sb['worst_m']:>+6.1f}%")
    print(f"\n  Lectura: COMPRABLE solo si Fresh tiene Sharpe/MaxDD comparable a BASE Marea incl. 2022.")
    print(f"  Si rinde menos riesgo-ajustado y sufre más en el bear → confirmado RADAR (vigilancia, no compra).\n")


def main():
    uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]
    print(f"\n{'#'*92}\n  ESTUDIO B — BREAKOUTS FRESCOS / SNOW | universo top-80 | datos desde 2020 (bear incl.)\n{'#'*92}")
    data, idx_obj, qqq = load_data(uni, START_S, CACHE_S)
    cal = build_calendar(data)
    print(f"  {len(data)}/{len(uni)} con historia | {len(cal)} días")
    event_study(data, qqq, cal)
    sleeve(data, idx_obj, cal, qqq)


if __name__ == "__main__":
    main()
