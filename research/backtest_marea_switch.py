import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
ESTUDIO F — CONMUTAR motores por RÉGIMEN (idea de Oscar, 2026-06-02)
===================================================================
Idea de Oscar: NO usar Marea + 2º motor a la vez (la mezcla 50/50 diluye el retorno). En su lugar
usar EL GANADOR de cada periodo: 100% Marea cuando tech/growth está al alza (cuando Marea funciona),
y mover ese capital al 2º motor (trend multi-activo) SOLO en los periodos donde Marea descansaría en
cash. "Usar el ganador en el periodo que más vaya a rendir." Quiere indicadores claros del régimen.

LA SÍNTESIS LIMPIA (sin clasificador frágil nuevo): cada motor ya trae su propio "¿estoy en mi
régimen?". Marea = QQQ>SMA200 con histéresis ±3% (Estudio C, ya live). El sleeve = trend long-only
multi-activo que se autorregula a T-bills (BIL) si ni oro/bonos/commodities trendean (Estudio E).
  Switch:  risk-on  (QQQ>SMA200 hist) -> 100% Marea
           risk-off (QQQ<SMA200 hist) -> 100% Sleeve (que internamente va a BIL si nada trendea)

LA PRUEBA HONESTA (la sutileza que decide): "QQQ<SMA200" marca que MAREA está off, NO que el sleeve
esté on. ¿El sleeve realmente GANA en los periodos donde Marea descansa, o pierde vs simplemente
tener T-bills? Por eso el PISO de comparación = conmutar a BIL puro (cash que paga interés). El
sleeve solo se gana el switch si BATE a ese piso en los días risk-off.

¿HABRÁ OTROS REGÍMENES? (pregunta de Oscar). Sí — la taxonomía real es >2:
  1. Risk-on / tech bull         -> Marea brilla.
  2. Risk-off INFLACIONARIO (2022): caen acciones Y bonos, suben commodities -> sleeve PUEDE ganar (DBC).
  3. Risk-off DEFLACIONARIO (crash): caen acciones, SUBEN bonos/oro (fuga a calidad) -> sleeve gana (TLT/GLD).
  4. Risk-off CHOP / whipsaw: nada trendea limpio -> trend-following SANGRA -> CASH (BIL) es el rey.
El switch a sleeve gana en 2/3; en 4 el sleeve debe replegarse SOLO a BIL. Medimos qué pasó de verdad.

REPRODUCIR:
    python backtest_marea_switch.py

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-02) — VEREDICTO: la idea de CONMUTAR es CORRECTA (le gana a mezclar), pero el 2º
motor que gana en los off resulta ser T-BILLS (cash con interés), NO el sleeve multi-activo.

Estrategias (2020-2026, 81% risk-on / 19% risk-off):
  Marea sola (off=cash, LIVE)   CAGR +40.1% Sharpe 1.58 MaxDD −20.7%
  SWITCH Marea<->Sleeve         CAGR +41.2% Sharpe 1.68 MaxDD −20.7%  (6 switches)
  SWITCH Marea<->BIL (T-bills)  CAGR +41.2% Sharpe 1.70 MaxDD −20.7%  (6 switches)  ← GANADOR
  Mezcla estática 75/25         CAGR +31.5% Sharpe 1.62 MaxDD −14.4%

1. CONMUTAR > MEZCLAR (instinto de Oscar correcto): el switch mantiene el CAGR completo de Marea
   (+41 vs +31.5 de la mezcla) Y sube el Sharpe (1.58→1.70). Usar el ganador por periodo, no diluir.
2. EL JUEZ (bloque 2): en los 313 días risk-off el sleeve rindió +2.7% vs BIL +2.6% → EMPATE. El 2º
   motor sofisticado NO le gana al cash que paga interés; y en el chop 2022 PIERDE (switch↔sleeve −10%
   vs switch↔BIL −4%). Por eso Marea↔BIL (1.70) > Marea↔Sleeve (1.68). El sleeve no se justifica.
3. DE DÓNDE VIENE LA MEJORA (1.58→1.70): de ganar yield de T-bills (~4-5%) sobre el capital ocioso que
   hoy el modelo trata como 0% en los off. Retorno casi sin riesgo → sube Sharpe; MaxDD sin cambio.
4. IMPLEMENTACIÓN (mucho más simple de lo esperado): la "definición de periodo" YA existe (histéresis
   ±3% de QQQ, Estudio C, ya live). El "2º motor" = parquear el cash en BIL cuando Marea está off. Un
   solo cambio chico, sin leverage/shorts. OJO real-world: verificar si eToro YA paga interés sobre el
   cash ocioso (si sí, el beneficio ya está capturado; si no, BIL/money-market lo captura).
5. OTROS REGÍMENES (pregunta de Oscar): taxonomía real = 4. risk-on bull (Marea) | off inflacionario
   (2022, sleeve debería ayudar pero no lo hizo neto) | off deflacionario/crash (TLT/GLD vuelan, sleeve
   ganaría — casi no apareció en muestra) | off chop (trend sangra, cash rey). Como el sleeve neto
   EMPATA al cash y pierde en el chop, la elección robusta es T-BILLS en todos los off.
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
import os, sys, json, statistics, math
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_marea_regime import regime_features, expo_series, START_S, CACHE_S
from backtest_marea_voltarget import run_vt
from backtest_marea_2ndmotor import (
    load_etfs, build_sleeve, to_rets, corr, prep_etf, CASH_ETF, TREND_ETFS,
)
from backtest_marea_selection import load_data, perf, yearly, CAP, DATA_DIR
from backtest_portfolio_momentum import build_calendar


def ann_vol(deq):
    r = [deq[i][1] / deq[i - 1][1] - 1 for i in range(1, len(deq)) if deq[i - 1][1] > 0]
    return statistics.pstdev(r) * math.sqrt(252) * 100 if len(r) > 1 else 0


def build_from_rets(dates, rets):
    eq = CAP; deq = []
    for d in dates:
        eq *= (1 + rets.get(d, 0.0)); deq.append((d, eq))
    return deq


def switch(dates, on_rets, off_rets, regime_on, cost=0.001):
    """100% on_rets cuando régimen on; 100% off_rets cuando off. Cobra 'cost' en cada transición."""
    eq = CAP; deq = []; prev = None; flips = 0
    for d in dates:
        on = regime_on(d)
        r = on_rets.get(d, 0.0) if on else off_rets.get(d, 0.0)
        if prev is not None and on != prev:
            eq *= (1 - cost); flips += 1
        eq *= (1 + r); deq.append((d, eq)); prev = on
    return deq, flips


def m(deq):
    return perf(deq), ann_vol(deq)


def main():
    uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]
    print(f"\n{'#'*112}")
    print(f"  ESTUDIO F — CONMUTAR motores por RÉGIMEN | Marea (risk-on) <-> Sleeve/BIL (risk-off) | desde 2020")
    print(f"{'#'*112}")
    data, idx_obj, qqq = load_data(uni, START_S, CACHE_S)
    cal = build_calendar(data)
    feats = regime_features(qqq, data, cal)
    hyst = expo_series(cal, feats, "hysteresis", {"band": 0.03})
    marea_deq = run_vt(data, cal, hyst, None)["deq"]
    etfs = load_etfs()
    sleeve_deq, _ = build_sleeve(etfs, [d for d, _ in marea_deq])
    bil_p = prep_etf(etfs[CASH_ETF])

    mr = to_rets(marea_deq); sr = to_rets(sleeve_deq)
    common = sorted(set(mr) & set(sr) & set(bil_p))
    bil_r = {d: bil_p[d]["ret"] for d in common}
    regime_on = lambda d: hyst.get(d, 1.0) > 0

    n_on = sum(1 for d in common if regime_on(d)); n_off = len(common) - n_on
    print(f"  {len(common)} días comunes | risk-ON {n_on} ({n_on/len(common)*100:.0f}%) | "
          f"risk-OFF {n_off} ({n_off/len(common)*100:.0f}%)")

    # ── estrategias ──
    marea_alone = build_from_rets(common, mr)                          # off -> cash implícito (live)
    sw_sleeve, f1 = switch(common, mr, sr, regime_on)                  # off -> sleeve trend
    sw_bil, f2 = switch(common, mr, bil_r, regime_on)                  # off -> BIL puro (piso)
    # mezcla estática 75/25 de referencia (Estudio E)
    am = 0.75 * CAP; as_ = 0.25 * CAP; blend = []; pm = common[0][:7]
    for d in common:
        am *= (1 + mr[d]); as_ *= (1 + sr[d]); tot = am + as_
        if d[:7] != pm:
            pm = d[:7]; am = 0.75 * tot; as_ = 0.25 * tot
        blend.append((d, tot))

    print(f"\n  ── 1) ESTRATEGIAS (tramo común {common[0]} → {common[-1]}) ──")
    print(f"  {'Estrategia':<34} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'Vol%':>5} {'Switches':>9}")
    print(f"  {'-'*74}")
    rows = [
        ("Marea sola (off=cash, LIVE)",     marea_alone, "-"),
        ("SWITCH Marea<->Sleeve",           sw_sleeve,   f1),
        ("SWITCH Marea<->BIL (piso)",       sw_bil,      f2),
        ("Mezcla estática 75/25 (Est. E)",  blend,       "-"),
    ]
    res = {}
    for nm, dq, fl in rows:
        s, v = m(dq); res[nm] = dq
        print(f"  {nm:<34} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {v:>5.1f}% {str(fl):>9}")

    # ── 2) ¿el sleeve GANA en los días risk-off, o pierde vs BIL? (el juez) ──
    off_days = [d for d in common if not regime_on(d)]
    def cum(rets, days):
        p = 1.0
        for d in days: p *= (1 + rets.get(d, 0.0))
        return (p - 1) * 100
    print(f"\n  ── 2) RENDIMIENTO SOLO EN DÍAS RISK-OFF ({len(off_days)} días) — ¿el sleeve bate al cash? ──")
    print(f"    Sleeve trend acumulado en off : {cum(sr, off_days):+.1f}%")
    print(f"    BIL (T-bills) acumulado en off: {cum(bil_r, off_days):+.1f}%")
    print(f"    -> Si sleeve <= BIL, conmutar al sleeve NO se justifica (mejor cash en los off).")

    # ── 3) desglose por AÑO (¿en qué tipo de periodo ayudó cada quién?) ──
    print(f"\n  ── 3) RETORNO POR AÑO ──")
    yrs = sorted(yearly(marea_alone).keys())
    print(f"  {'':<34} " + " ".join(f"{y:>7}" for y in yrs))
    for nm, dq, _ in rows:
        yy = yearly(res[nm]); print(f"  {nm:<34} " + " ".join(f"{yy.get(y,0):>+6.0f}%" for y in yrs))

    # ── 4) robustez split-half ──
    print(f"\n  ── 4) ROBUSTEZ split-half ──")
    print(f"  {'Estrategia':<34} {'20-22 Sh':>9} {'20-22 DD':>9} {'23-26 Sh':>9} {'23-26 DD':>9}")
    for nm, dq, _ in rows:
        d2 = res[nm]
        h1 = [(d, e) for d, e in d2 if d < "2023-01-01"]; h2 = [(d, e) for d, e in d2 if d >= "2023-01-01"]
        p1, p2 = perf(h1), perf(h2)
        print(f"  {nm:<34} {p1['sharpe']:>9.2f} {p1['mdd']:>+8.1f}% {p2['sharpe']:>9.2f} {p2['mdd']:>+8.1f}%")

    print(f"\n{'='*112}")
    print("  Lectura: el SWITCH se gana su lugar si bate a 'Marea sola' (más CAGR/Sharpe usando los off)")
    print("  Y al piso 'Marea<->BIL' (el sleeve aporta sobre el cash). Si el sleeve <= BIL en los off,")
    print("  la versión correcta es Marea<->BIL (cash), no el sleeve. Mirá el bloque 2: ahí se decide.")
    print(f"{'='*112}\n")


if __name__ == "__main__":
    main()
