import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
ESTUDIO C-Fase2 — DERISK (circuit-breaker de régimen) A FONDO
=============================================================
El Estudio C (backtest_marea_regime.py) encontró que la histéresis ±3% (anti-whipsaw, gate)
ya bate al binario en todo. El DERISK (además de no comprar, VENDER los holders más débiles
cuando el régimen cae) mostró Sharpe 1.60 y mejor DD reciente, pero es un cambio de COMPORTAMIENTO
mayor (Oscar recibiría señales de VENDER por régimen). Oscar (2026-06-02): "probemos la Fase 2 a fondo".

LA PREGUNTA REAL no es "¿derisk sube el Sharpe?" sino:
  1. ¿El derisk agrega valor ROBUSTO por encima de la histéresis-gate, o la histéresis ya hace todo?
  2. ¿Las ventas-por-régimen son BUENAS (evitan más caída) o CHURN (vendés el piso y rebota)?  ← clave
  3. ¿De qué drawdowns concretos protege? ¿Aguanta el bear 2022 Y el chop 2025 sin churnear?
  4. ¿Cuántas señales de venta extra le mete a Oscar por año (carga operativa real)?
  5. ¿Es sensible al diseño (banda, regla de venta, confirmación de re-entrada)?

MÉTRICA DE ARREPENTIMIENTO (regret): para cada venta-por-régimen, miramos el precio del MISMO
ticker H días después. Si subió → vendimos barato (churn). Si bajó → acertamos (protección). La
mediana del forward-return tras las ventas-régimen es el juez: negativo = el derisk salva; positivo
= está cortando ganadores por miedo macro.

Todo sobre el arnés validado (top-80, K=5, gate breakout + chand 4×ATR, vol, open+1, fee $1/lado,
datos desde 2020 bear-inclusive). NO se toca la selección.

REPRODUCIR:
    python backtest_marea_derisk.py            # decomposición + diseño + calidad de ventas + robustez

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-02) — VEREDICTO: EL DERISK NO SE GANA SU COMPLEJIDAD. La histéresis ±3% (gate)
es el cambio correcto; el circuit-breaker que VENDE en el bear se DESCARTA.

1. DECOMPOSICIÓN — la histéresis ya hace casi todo:
     BASE binario gate      CAGR +35.8% Sharpe 1.35 MaxDD −23.8%
     Histéresis ±3% gate    CAGR +40.0% Sharpe 1.58 MaxDD −20.7%   ← el salto real
     Histéresis ±3% DERISK  CAGR +39.0% Sharpe 1.60 MaxDD −20.7%   ← +0.02 Sharpe, MISMO DD, CAGR↓
2. EL PEOR DD ES IRREDUCIBLE POR RÉGIMEN: el drawdown más profundo (−20.7%, feb-jul 2021) ocurre
   con QQQ SOBRE la SMA200 (risk-on) → es rotación/concentración, no mercado. Ninguna regla de
   régimen lo toca. El derisk solo recorta caídas secundarias de la mitad reciente (−16→−14%), marginal.
3. CALIDAD DE LAS VENTAS-RÉGIMEN (el juez) — AMBIGUA: histéresis±3% derisk dispara solo 9 ventas en
   6 años; forward+20d mediana −6.6% (protege) PERO media +10.1% y 44% siguieron subiendo (n=9, ruido).
   No es "el derisk salva" claro; es "a veces protege, a veces corta un ganador". Binario derisk vende
   42× (protección más limpia pero más churn/carga).
4. DISEÑO: robusto a la banda (±2/±3/±4 ≈ Sharpe 1.6) pero GRADED/partial derisk = malo (churn,
   Sharpe 1.26-1.42, DD −25/−27); CONFIRMACIÓN de re-entrada EMPEORA (confirm5 Sharpe 1.56, pierde
   el rebote). Nada mejora al combo simple.
5. CARGA OPERATIVA: ~0.5 eventos/año (2022/2025/2026). Manejable, pero cada uno = vender hasta K
   nombres de golpe → cambio de COMPORTAMIENTO mayor para Oscar (señales de "liquidá el libro").

CONCLUSIÓN: adoptar HISTÉRESIS ±3% GATE (captura todo el edge de régimen, CERO comportamiento nuevo
— solo hace más listo el gate de "no comprar" que ya existe). DESCARTAR el derisk: +0.02 Sharpe,
mismo MaxDD en el peor episodio, CAGR levemente menor, ventas de calidad ambigua — no compensa la
complejidad ni las señales de venta. Negativo sano, mismo patrón que Estudios A/B.

NOTA para futuros estudios: el −20.7% residual es de SELECCIÓN/concentración en risk-on (no régimen).
Bajarlo pediría más diversificación (K↑, ya visto marginal en Estudio B) o caps de sector intra-risk-on.
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
import os, sys, json, statistics
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_marea_regime import (
    run_regime, expo_series, regime_features, START_S, CACHE_S,
)
from backtest_marea_selection import (
    load_data, base_cfg, perf, year_perf, yearly, summarize,
    K, MULT, FEE, CAP, DATA_DIR,
)
from backtest_portfolio_momentum import build_calendar


# ── transform de expo: exigir N días sostenidos antes de re-encender riesgo ───
def confirm_recovery(expo, calendar, n):
    """Retrasa cada subida de 0→positivo hasta que se sostenga n días (anti-falsa-recuperación)."""
    if n <= 0:
        return expo
    out = {}; streak = 0; last = expo[calendar[0]]
    for d in calendar:
        e = expo[d]
        if e > 0:
            streak += 1
            out[d] = e if streak >= n else 0.0
        else:
            streak = 0; out[d] = 0.0
    return out


# ── episodios de drawdown (underwater) a partir de la curva de equity ─────────
def dd_episodes(deq, topn=4):
    peak = deq[0][1]; peak_d = deq[0][0]
    ep = None; eps = []
    for d, eq in deq:
        if eq >= peak:
            if ep:
                ep["recover"] = d; eps.append(ep); ep = None
            peak = eq; peak_d = d
        else:
            depth = eq / peak - 1
            if ep is None:
                ep = {"peak_d": peak_d, "trough_d": d, "depth": depth, "recover": None}
            elif depth < ep["depth"]:
                ep["depth"] = depth; ep["trough_d"] = d
    if ep:
        eps.append(ep)
    eps.sort(key=lambda x: x["depth"])
    return eps[:topn]


# ── calidad de las ventas-por-régimen: forward return tras la venta ───────────
def exit_quality(res, data, H=20):
    reg = [t for t in res["trades"] if t["kind"] == "regime"]
    chand = [t for t in res["trades"] if t["kind"] == "chand"]
    fwd = []
    for t in reg:
        dd = data[t["tk"]]; xi = t["exit_idx"]
        if xi + H < len(dd["bars"]):
            fwd.append(dd["bars"][xi + H]["c"] / t["exit_px"] - 1)
    return {
        "n_regime": len(reg), "n_chand": len(chand),
        "pnl_regime": statistics.mean([t["pnl_pct"] for t in reg]) if reg else 0,
        "pnl_chand": statistics.mean([t["pnl_pct"] for t in chand]) if chand else 0,
        "fwd_median": (statistics.median(fwd) * 100) if fwd else 0,
        "fwd_mean": (statistics.mean(fwd) * 100) if fwd else 0,
        "fwd_pos_pct": (sum(1 for x in fwd if x > 0) / len(fwd) * 100) if fwd else 0,
        "n_fwd": len(fwd),
    }


# ── eventos de risk-off (expo cae a 0) y días fuera, por año ──────────────────
def riskoff_stats(expo, calendar):
    events = {}; days_off = {}
    prev = expo[calendar[0]]
    for d in calendar:
        y = d[:4]; e = expo[d]
        days_off.setdefault(y, 0); events.setdefault(y, 0)
        if e == 0:
            days_off[y] += 1
        if prev > 0 and e == 0:
            events[y] += 1
        prev = e
    return events, days_off


def line(s, cfg_extra=None):
    return s


def main():
    uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]
    print(f"\n{'#'*112}")
    print(f"  ESTUDIO C-Fase2 — DERISK A FONDO | top-80 K={K} gate+chand {MULT}×ATR | datos desde 2020 (bear 2022)")
    print(f"{'#'*112}")
    data, idx_obj, qqq = load_data(uni, START_S, CACHE_S)
    cal = build_calendar(data)
    print(f"  {len(data)}/{len(uni)} con historia | {len(cal)} días | calculando régimen...")
    feats = regime_features(qqq, data, cal)

    # series de exposición reutilizables
    E = {
        "binary":  expo_series(cal, feats, "binary", {}),
        "hyst2":   expo_series(cal, feats, "hysteresis", {"band": 0.02}),
        "hyst3":   expo_series(cal, feats, "hysteresis", {"band": 0.03}),
        "hyst4":   expo_series(cal, feats, "hysteresis", {"band": 0.04}),
        "graded":  expo_series(cal, feats, "graded", {}),
    }

    def runc(expo, **cfg):
        return run_regime(data, cal, base_cfg(**cfg), expo)

    # ════════════ 1) DECOMPOSICIÓN: ¿cuánto agrega el derisk sobre la histéresis? ════════════
    print(f"\n  ── 1) DECOMPOSICIÓN del edge (¿la histéresis ya hace el trabajo?) ──")
    decomp = [
        ("BASE binario gate",        E["binary"], {"regime_mode": "gate"}),
        ("Histéresis ±3% gate",      E["hyst3"],  {"regime_mode": "gate"}),
        ("Histéresis ±3% DERISK",    E["hyst3"],  {"regime_mode": "derisk"}),
    ]
    print(f"  {'Variante':<26} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'PeorMes':>7} {'Trades':>6} {'Fees$':>6}")
    print(f"  {'-'*70}")
    dec_res = {}
    for name, expo, cfg in decomp:
        res = runc(expo, **cfg); s = summarize(res); dec_res[name] = (res, expo)
        print(f"  {name:<26} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {s['worst_m']:>+6.1f}% "
              f"{s['trades']:>6} {s['fees']:>6.0f}")

    print(f"\n  PEORES EPISODIOS DE DRAWDOWN por variante (¿de qué caídas protege el derisk?):")
    for name, _, _ in decomp:
        res = dec_res[name][0]
        print(f"    {name}:")
        for ep in dd_episodes(res["deq"], 4):
            rec = ep["recover"] or "abierto"
            print(f"      {ep['peak_d']} → {ep['trough_d']}  {ep['depth']*100:>+6.1f}%  (recupera {rec})")

    # ════════════ 2) DISEÑO del derisk: banda × regla de venta × confirmación re-entrada ════════════
    print(f"\n  ── 2) DISEÑO del derisk (robustez al cómo) ──")
    design = [
        ("Binario DERISK",            E["binary"], {"regime_mode": "derisk"}),
        ("Hyst ±2% DERISK",           E["hyst2"],  {"regime_mode": "derisk"}),
        ("Hyst ±3% DERISK",           E["hyst3"],  {"regime_mode": "derisk"}),
        ("Hyst ±4% DERISK",           E["hyst4"],  {"regime_mode": "derisk"}),
        ("Hyst ±3% DERISK +confirm3", confirm_recovery(E["hyst3"], cal, 3), {"regime_mode": "derisk"}),
        ("Hyst ±3% DERISK +confirm5", confirm_recovery(E["hyst3"], cal, 5), {"regime_mode": "derisk"}),
        ("Graded DERISK (weak)",      E["graded"], {"regime_mode": "derisk", "derisk_rule": "weak"}),
        ("Graded DERISK (loser)",     E["graded"], {"regime_mode": "derisk", "derisk_rule": "loser"}),
    ]
    print(f"  {'Variante':<28} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'2022':>6} {'22DD':>6} "
          f"{'2025':>6} {'Trades':>6}")
    print(f"  {'-'*82}")
    des_res = {}
    for name, expo, cfg in design:
        res = runc(expo, **cfg); s = summarize(res); des_res[name] = (res, expo)
        y22 = year_perf(res["deq"], "2022"); y25 = year_perf(res["deq"], "2025")
        r22 = f"{y22['total']:>+4.0f}%" if y22 else " n/a "
        d22 = f"{y22['mdd']:>+5.1f}%" if y22 else " n/a "
        r25 = f"{y25['total']:>+4.0f}%" if y25 else " n/a "
        print(f"  {name:<28} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {r22:>6} {d22:>6} "
              f"{r25:>6} {s['trades']:>6}")

    # ════════════ 3) CALIDAD de las ventas-por-régimen (¿buenas o churn?) ════════════
    print(f"\n  ── 3) CALIDAD de las ventas-por-régimen (regret: precio +20d después de vender) ──")
    print(f"  {'Variante':<28} {'#reg':>5} {'P&L reg%':>8} {'P&L ch%':>8} {'fwd+20 med%':>11} "
          f"{'fwd+20 mean%':>12} {'%subió':>7}")
    print(f"  {'-'*86}")
    for name in ["Binario DERISK", "Hyst ±3% DERISK", "Hyst ±3% DERISK +confirm5", "Graded DERISK (loser)"]:
        res = des_res[name][0]; q = exit_quality(res, data, H=20)
        print(f"  {name:<28} {q['n_regime']:>5} {q['pnl_regime']:>+7.1f}% {q['pnl_chand']:>+7.1f}% "
              f"{q['fwd_median']:>+10.1f}% {q['fwd_mean']:>+11.1f}% {q['fwd_pos_pct']:>6.0f}%")
    print(f"  Lectura: fwd+20 NEGATIVO y %subió<50 → las ventas-régimen evitaron caídas (derisk SALVA).")
    print(f"           fwd+20 POSITIVO y %subió>50 → vendiste y siguió subiendo (CHURN: derisk corta ganadores).")

    # ════════════ 4) CARGA OPERATIVA: eventos risk-off / año ════════════
    print(f"\n  ── 4) CARGA OPERATIVA (eventos de risk-off y días fuera, Hyst ±3%) ──")
    ev, off = riskoff_stats(E["hyst3"], cal)
    years = sorted(off)
    print(f"  {'':<14}" + " ".join(f"{y:>7}" for y in years))
    print(f"  {'eventos':<14}" + " ".join(f"{ev.get(y,0):>7}" for y in years))
    print(f"  {'días fuera':<14}" + " ".join(f"{off.get(y,0):>7}" for y in years))
    tot_ev = sum(ev.values())
    print(f"  → {tot_ev} eventos de venta-por-régimen en {len(cal)} días (~{tot_ev/(len(cal)/252):.1f}/año). "
          f"Cada evento = vender hasta {K} nombres de golpe.")

    # ════════════ 5) ROBUSTEZ: año-a-año + split-half de los finalistas ════════════
    print(f"\n  ── 5) ROBUSTEZ (año-a-año + split-half) ──")
    finalists = ["BASE binario gate", "Histéresis ±3% gate", "Histéresis ±3% DERISK"]
    fres = {n: dec_res[n][0] for n in finalists}
    yrs = sorted(yearly(fres[finalists[0]]["deq"]).keys())
    print(f"  {'Variante':<26} " + " ".join(f"{y:>7}" for y in yrs))
    for n in finalists:
        yr = yearly(fres[n]["deq"])
        print(f"  {n:<26} " + " ".join(f"{yr.get(y,0):>+6.0f}%" for y in yrs))
    print(f"\n  {'Variante':<26} {'20-22 Sh':>9} {'20-22 DD':>9} {'23-26 Sh':>9} {'23-26 DD':>9}")
    for n in finalists:
        deq = fres[n]["deq"]
        h1 = [(d, e) for d, e in deq if d < "2023-01-01"]; h2 = [(d, e) for d, e in deq if d >= "2023-01-01"]
        p1, p2 = perf(h1), perf(h2)
        print(f"  {n:<26} {p1['sharpe']:>9.2f} {p1['mdd']:>+8.1f}% {p2['sharpe']:>9.2f} {p2['mdd']:>+8.1f}%")

    print(f"\n{'='*112}")
    print("  VEREDICTO (rellenar tras leer): el derisk se adopta SOLO si (a) agrega DD/Sharpe robusto sobre")
    print("  la histéresis-gate, (b) las ventas-régimen tienen fwd+20 negativo (protegen, no churnean), y")
    print("  (c) la carga de señales/año es manejable. Si no, la histéresis-gate sola es el cambio correcto.")
    print(f"{'='*112}\n")


if __name__ == "__main__":
    main()
