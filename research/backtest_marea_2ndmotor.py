import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
ESTUDIO E — 2º MOTOR DESCORRELACIONADO (sleeve de trend-following multi-activo, long-only)
=========================================================================================
Oscar (2026-06-02): siguiente palanca tras descartar el vol targeting. La lección de ese estudio:
SIN apalancamiento, lo único que sube el Sharpe estructuralmente es DIVERSIFICAR a otra línea
riesgo/retorno (no moverte a lo largo de la misma).

QUÉ ES: Marea es UN motor — momentum largo en acciones (tech/growth) = beta larga. En el bear se
va a cash (no gana) y todo su retorno está correlacionado al Nasdaq. El 2º motor es una segunda
estrategia con BAJA correlación que pueda ganar cuando Marea no: trend-following long-only sobre
ETFs de OTRAS clases de activo (oro, bonos, materias primas, dólar, REITs). Cada ETF se tiene LONG
solo si está en tendencia (precio > SMA200); si no, esa porción va a CASH (BIL, T-bills 1-3m, paga
interés real → realista). Rebalanceo MENSUAL (bajo turnover, operable manual). SIN shorts, SIN leverage.

POR QUÉ PODRÍA FUNCIONAR (y por qué medirlo, no asumirlo): en 2022 cayeron acciones Y bonos juntos,
pero las materias primas (DBC) volaron (energía) → un trend sleeve habría estado long DBC ganando
mientras Marea estaba en cash. ESO es "crisis-alpha-lite". PERO long-only NO puede ganar con activos
que CAEN (solo evita pérdidas yendo a cash), así que el beneficio es más MUDO que un managed-futures
long/short real. Los datos deciden.

LA PRUEBA (honesta, mismo rigor que vol targeting): diversificar solo ayuda si el sleeve tiene
(a) retorno esperado POSITIVO y (b) correlación BAJA con Marea. Si el sleeve rinde ~0, combinar solo
baja la vol (= dial de riesgo, NO edge — la trampa del vol targeting). El sleeve gana su lugar SOLO si
alguna combinación Marea+sleeve SUBE el Sharpe sobre Marea-sola (1.58) de forma ROBUSTA, idealmente
bajando el MaxDD más de lo que cede en CAGR. Combinación = split de capital con rebalanceo mensual.

REPRODUCIR:
    python backtest_marea_2ndmotor.py

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-02) — VEREDICTO: FUNCIONA como diversificación (Sharpe↑ + DD↓, robusto), pero es
un diversificador de BAJO RETORNO → trade-off de preferencia de riesgo, no un free lunch limpio.

Componentes (2020-2026): Marea CAGR +40.1% Sharpe 1.58 MaxDD −20.7% | Sleeve trend CAGR +6.7%
Sharpe 0.85 MaxDD −17.2%. CORRELACIÓN diaria +0.11 (genuinamente baja = buen diversificador; a
diferencia del vol targeting, la 2ª pata SÍ tiene retorno positivo).

Combinación (rebal mensual) — Sharpe sube monótono y el DD se desploma:
  85/15  CAGR +34.9% Sharpe 1.60 MaxDD −16.9%
  75/25  CAGR +31.5% Sharpe 1.62 MaxDD −14.4%
  50/50  CAGR +23.1% Sharpe 1.64 MaxDD −10.8%
Robusto: split-half mejora el DD en AMBAS mitades (−20.7→−9.9 temprana, −16.1→−10.8 reciente);
Sharpe sube o se sostiene. ESTO sí mueve la línea riesgo/retorno hacia ARRIBA (no a lo largo de ella).

EL "PERO" (honesto): la mejora de Sharpe es MODESTA (+0.02..+0.06) y el costo en CAGR es GRANDE
(40→23% al 50/50). El sleeve solo rinde ~7%/año y fue casi MUERTO en 2023-24 (−2%/+2% mientras Marea
+31%/+67%). El crisis-alpha esperado en 2022 NO apareció (sleeve −6%, igual que Marea): long-only no
puede GANAR con activos que caen, solo evitar pérdidas. El beneficio es correlación/vol baja, no bear-gains.

CONCLUSIÓN: el CONCEPTO funciona (diversificación real, robusta) — cambia retorno por un ride más suave
y Sharpe algo mejor. Decisión de PREFERENCIA DE RIESGO de Oscar + carga operativa real (2º sleeve de 6
ETFs, rebal mensual). Para optimizar Sharpe/DD: 15-25% al sleeve es defendible. SIGUIENTE si interesa:
buscar un 2º motor de MAYOR retorno standalone aún descorrelacionado (rel-momentum entre activos, u
otra estrategia) — el cuello de botella es el ~7% del sleeve, no la correlación.
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
import os, sys, json, statistics, math
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_marea_regime import regime_features, expo_series, START_S, CACHE_S
from backtest_marea_voltarget import run_vt
from backtest_marea_selection import (
    load_data, fetch_daily_cached, perf, yearly, CAP, DATA_DIR,
)
from backtest_portfolio_momentum import build_calendar

TREND_ETFS = ["GLD", "TLT", "IEF", "DBC", "UUP", "VNQ"]   # clases de activo diversificadoras
CASH_ETF = "BIL"                                          # pata de cash (T-bills 1-3m, paga interés)
ETF_START = "2018-06-01T00:00:00Z"                        # warm-up SMA200 antes de 2020
ETF_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_2ndmotor_cache.json")


def corr(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs)); sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (sx * sy) if sx > 0 and sy > 0 else 0.0


def ann_vol_from_rets(rets):
    return statistics.pstdev(rets) * math.sqrt(252) * 100 if len(rets) > 1 else 0


def load_etfs():
    cache = json.load(open(ETF_CACHE)) if os.path.exists(ETF_CACHE) else {}
    n0 = len(cache); out = {}
    for tk in TREND_ETFS + [CASH_ETF]:
        b = fetch_daily_cached(tk, cache, ETF_START)
        if len(b) > 220:
            out[tk] = b
    if len(cache) != n0:
        json.dump(cache, open(ETF_CACHE, "w"))
    return out


def prep_etf(bars):
    """date -> {c, sma200, ret} con SMA200 trailing y retorno diario."""
    closes = [b["c"] for b in bars]; out = {}
    for i, b in enumerate(bars):
        sma = sum(closes[i - 199:i + 1]) / 200 if i >= 199 else None
        ret = (closes[i] / closes[i - 1] - 1) if i > 0 else 0.0
        out[b["t"]] = {"c": closes[i], "sma200": sma, "ret": ret}
    return out


def build_sleeve(etfs, calendar):
    """
    Trend long-only multi-activo, rebalanceo MENSUAL. Sub-cuentas por activo (drift exacto).
    En cada inicio de mes: equal-weight entre los ETFs en tendencia (precio prev > SMA200 prev);
    el resto del capital a CASH (BIL). Devuelve deq [(date, equity)].
    """
    P = {tk: prep_etf(b) for tk, b in etfs.items()}
    cash_tk = CASH_ETF
    # solo fechas donde TODOS los ETFs tienen barra y SMA200 lista (alineación limpia)
    dates = [d for d in calendar
             if all(d in P[tk] and P[tk][d]["sma200"] is not None for tk in P)]
    if not dates:
        return [], dates
    accts = {}; total = CAP; deq = []
    prev_month = None
    for k, d in enumerate(dates):
        # 1) compounding diario de las sub-cuentas con el retorno de su activo
        if accts:
            for tk in list(accts):
                accts[tk] *= (1 + P[tk][d]["ret"])
            total = sum(accts.values())
        # 2) rebalanceo al cambiar de mes (señal = prev day)
        month = d[:7]
        if month != prev_month:
            prev_month = month
            sig_d = dates[k - 1] if k > 0 else d
            intrend = [tk for tk in TREND_ETFS
                       if P[tk][sig_d]["c"] > P[tk][sig_d]["sma200"]]
            accts = {}
            if intrend:
                w = total / len(intrend)
                for tk in intrend:
                    accts[tk] = w
            else:
                accts[cash_tk] = total          # nada en tendencia -> todo a T-bills
            # la porción no asignada (siempre 0 acá: si hay intrend van 100% a ellos) -> sin cash
            # variante defensiva: mezclar cash cuando pocos en tendencia (no usada; equal-weight puro)
        deq.append((d, total))
    return deq, dates


def to_rets(deq):
    return {deq[i][0]: deq[i][1] / deq[i - 1][1] - 1 for i in range(1, len(deq))}


def blend(marea_deq, sleeve_deq, w, rebal="monthly"):
    """Combina Marea (1-w) + sleeve (w) con rebalanceo mensual del split de capital."""
    mr = to_rets(marea_deq); sr = to_rets(sleeve_deq)
    common = sorted(set(mr) & set(sr))
    if not common:
        return []
    am = (1 - w) * CAP; as_ = w * CAP; deq = []; prev_month = common[0][:7]
    for d in common:
        am *= (1 + mr[d]); as_ *= (1 + sr[d])
        tot = am + as_
        if d[:7] != prev_month:                 # rebalancea el split al cambiar de mes
            prev_month = d[:7]; am = (1 - w) * tot; as_ = w * tot
        deq.append((d, tot))
    return deq


def m(deq):
    s = perf(deq); rets = list(to_rets(deq).values())
    return s, ann_vol_from_rets(rets)


def main():
    uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]
    print(f"\n{'#'*112}")
    print(f"  ESTUDIO E — 2º MOTOR DESCORRELACIONADO (trend long-only multi-activo) | BASE = Marea histéresis ±3% | desde 2020")
    print(f"{'#'*112}")
    data, idx_obj, qqq = load_data(uni, START_S, CACHE_S)
    cal = build_calendar(data)
    feats = regime_features(qqq, data, cal)
    hyst = expo_series(cal, feats, "hysteresis", {"band": 0.03})
    marea_deq = run_vt(data, cal, hyst, None)["deq"]
    print(f"  Marea: {len(marea_deq)} días | sleeve ETFs: {TREND_ETFS} + cash {CASH_ETF}")

    etfs = load_etfs()
    print(f"  ETFs cargados: {sorted(etfs)}")
    sleeve_deq, sdates = build_sleeve(etfs, [d for d, _ in marea_deq])

    # alinear ambos a fechas comunes
    mr = to_rets(marea_deq); sr = to_rets(sleeve_deq)
    common = sorted(set(mr) & set(sr))
    mrl = [mr[d] for d in common]; srl = [sr[d] for d in common]
    rho = corr(mrl, srl)

    # recortar curvas al tramo común para métricas justas
    md = [(d, e) for d, e in marea_deq if d in set(common) or d == marea_deq[0][0]]
    md = [(d, e) for d, e in marea_deq if d >= common[0]]
    sd = [(d, e) for d, e in sleeve_deq if d >= common[0]]

    print(f"\n  ── 1) COMPONENTES por separado (tramo común {common[0]} → {common[-1]}) ──")
    print(f"  {'Estrategia':<26} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'Vol%':>5}")
    print(f"  {'-'*54}")
    for nm, dq in [("Marea (BASE histéresis)", md), (f"Sleeve trend ({len(TREND_ETFS)} ETFs)", sd)]:
        s, v = m(dq)
        print(f"  {nm:<26} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {v:>5.1f}%")
    print(f"\n  CORRELACIÓN diaria Marea vs Sleeve: {rho:+.2f}   "
          f"(baja/negativa = buen diversificador; ~0.6+ = redundante)")

    print(f"\n  ── 2) COMBINACIÓN Marea(1-w) + Sleeve(w), rebalanceo mensual ──")
    print(f"  {'Mezcla':<26} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'Vol%':>5} {'vs BASE Sharpe':>15}")
    print(f"  {'-'*74}")
    base_sharpe = m(md)[0]["sharpe"]
    blends = {}
    for w in (0.0, 0.15, 0.25, 0.35, 0.50):
        bd = blend(marea_deq, sleeve_deq, w)
        blends[w] = bd; s, v = m(bd)
        tag = "← BASE" if w == 0 else f"{s['sharpe']-base_sharpe:+.2f}"
        print(f"  {'Marea '+str(int((1-w)*100))+'/'+str(int(w*100))+' Sleeve':<26} "
              f"{s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {v:>5.1f}% {tag:>15}")

    print(f"\n  ── 3) ¿El sleeve gana cuando Marea sufre? RETORNO POR AÑO ──")
    yrs = sorted(yearly(md).keys())
    ym = yearly(md); ysl = yearly(sd)
    print(f"  {'':<26} " + " ".join(f"{y:>7}" for y in yrs))
    print(f"  {'Marea':<26} " + " ".join(f"{ym.get(y,0):>+6.0f}%" for y in yrs))
    print(f"  {'Sleeve trend':<26} " + " ".join(f"{ysl.get(y,0):>+6.0f}%" for y in yrs))
    best_w = max([w for w in blends if w > 0], key=lambda w: m(blends[w])[0]["sharpe"])
    yb = yearly(blends[best_w])
    print(f"  {'Mezcla '+str(int((1-best_w)*100))+'/'+str(int(best_w*100)):<26} "
          + " ".join(f"{yb.get(y,0):>+6.0f}%" for y in yrs))

    print(f"\n  ── 4) ROBUSTEZ split-half de la mejor mezcla (w={best_w:.0%}) vs Marea ──")
    print(f"  {'':<26} {'20-22 Sh':>9} {'20-22 DD':>9} {'23-26 Sh':>9} {'23-26 DD':>9}")
    for nm, dq in [("Marea sola", md), (f"Mezcla {int((1-best_w)*100)}/{int(best_w*100)}", blends[best_w])]:
        h1 = [(d, e) for d, e in dq if d < "2023-01-01"]; h2 = [(d, e) for d, e in dq if d >= "2023-01-01"]
        p1, p2 = perf(h1), perf(h2)
        print(f"  {nm:<26} {p1['sharpe']:>9.2f} {p1['mdd']:>+8.1f}% {p2['sharpe']:>9.2f} {p2['mdd']:>+8.1f}%")

    print(f"\n{'='*112}")
    print("  VEREDICTO: el sleeve se adopta SOLO si (a) tiene CAGR positivo, (b) correlación baja con Marea,")
    print("  y (c) alguna mezcla SUBE el Sharpe sobre Marea-sola de forma robusta (ambas mitades) bajando el")
    print("  MaxDD. Si solo baja vol sin subir Sharpe → dial de riesgo, no edge (trampa del vol targeting).")
    print(f"{'='*112}\n")


if __name__ == "__main__":
    main()
