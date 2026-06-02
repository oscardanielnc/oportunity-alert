import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
ESTUDIO D — VOL TARGETING DE CARTERA (overlay, SIN apalancamiento, tope 1.0x)
============================================================================
Oscar (2026-06-02): siguiente palanca tras adoptar la histéresis. Decisión: SIN apalancamiento
(tope 1.0x) → el vol targeting solo RECORTA el gross en turbulencia, nunca sube de 100% invertido.

HIPÓTESIS (distinta del régimen, no redundante): el peor drawdown medido (−20.7%, feb-2021) ocurrió
con QQQ SOBRE la SMA200 (risk-on) → la histéresis no lo toca porque es idiosincrático/de volatilidad,
no de tendencia de mercado. Vol targeting ataca eso: cuando la volatilidad del libro (o del mercado)
se dispara aunque el régimen siga verde, encoge el tamaño → baja el DD risk-on.

OJO (lo que la hace ganar o no): ya existe vol-sizing POR NOMBRE (min(1, 0.03/atr%) al entrar). El
vol targeting de CARTERA agrega la dimensión AGREGADA (muchos nombres volátiles a la vez = correlación
alta = turbulencia). Solo se gana su lugar si BAJA el DD y/o SUBE el Sharpe SIN recortar el CAGR a la
par (si baja retorno y DD proporcional → Sharpe igual → no sirve; es solo bajar de tamaño).

Sobre el arnés validado + la BASE ya LIVE (histéresis ±3% gate). NO se toca la selección.
  Estimador de vol : equity (vol realizada del propio equity) | qqq (vol del mercado) | atr (vol media de holdings)
  Ventana          : 20d | 60d
  Vol objetivo     : barrido (15/20/25/30% anualizado)
  Mecanismo        : entry (escala solo el tamaño de compras nuevas; bajo turnover) |
                     rebal (rebalancea TODO el libro a g·equity con banda; control fuerte, más turnover)
  Tope g = 1.0     : NUNCA apalanca (solo recorta). g = min(1, vol_obj / vol_realizada).

REPRODUCIR:
    python backtest_marea_voltarget.py            # decomposición + barridos + peores DD + robustez

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-02) — VEREDICTO: NO APORTA EDGE con tope 1.0x. Es un DIAL de riesgo, no alpha.

1. ENTRY-ONLY (viable, bajo turnover): bajar el vol-objetivo recorta retorno Y DD a la par → el
   Sharpe NUNCA supera al de BASE (tope 1.58). VT qqq t20: MaxDD −20.7→−17.0% (corta feb-2021) y
   recupera antes, pero CAGR +40→+33% (Sharpe 1.55). Sin apalancamiento NO hay free lunch.
   Estimador ATR-de-holdings = malo (Sharpe 1.23-1.37): duplica el vol-sizing por-nombre existente.
2. REBAL (control fuerte): el "mejor" (equity t25, Sharpe 1.61, DD −18) es marginal y NO robusto:
   el extra de retorno es CASH OCIOSO desplegado (más beta, vol 22.8→28%), mete drawdowns tardíos
   nuevos (2025-11, 2026-03) y cuesta +55% fees + rebalanceo manual de todo el libro. No compensa.
3. TEORÍA: el vol targeting sube el Sharpe sobre todo APALANCANDO en calma (mitad asimétrica). Con
   tope 1.0x se elimina esa mitad → solo de-riskea → te mueve a lo largo de la MISMA línea riesgo/
   retorno, no por encima. Para mejorar Sharpe haría falta leverage (Oscar lo descartó).

CONCLUSIÓN: NO adoptar. Disponible solo como DIAL si Oscar quisiera un ride más calmo a costa de
retorno (VT qqq entry t20: DD −17%, Sharpe igual, sin leverage ni turnover extra). Negativo sano,
mismo patrón que el derisk. La diversificación (2º motor) es la palanca que SÍ sube Sharpe sin leverage.
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
import os, sys, json, statistics, math
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_marea_regime import regime_features, expo_series, START_S, CACHE_S
from backtest_marea_selection import (
    load_data, base_cfg, signal_val, perf, year_perf, yearly, summarize,
    K, WARM, VOL_TARGET, FEE, CAP, MULT, DATA_DIR,
)
from backtest_portfolio_momentum import build_calendar


def ann_vol(deq):
    rets = [deq[i][1] / deq[i - 1][1] - 1 for i in range(1, len(deq)) if deq[i - 1][1] > 0]
    return statistics.pstdev(rets) * math.sqrt(252) * 100 if len(rets) > 1 else 0


def dd_episodes(deq, topn=4):
    peak = deq[0][1]; peak_d = deq[0][0]; ep = None; eps = []
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
    eps.sort(key=lambda x: x["depth"]); return eps[:topn]


def qqq_vol_series(qqq, win):
    closes = [b["c"] for b in qqq]; out = {}
    for i, b in enumerate(qqq):
        if i > win:
            rets = [closes[j] / closes[j - 1] - 1 for j in range(i - win + 1, i + 1)]
            out[b["t"]] = statistics.pstdev(rets) * math.sqrt(252)
        else:
            out[b["t"]] = None
    return out


# ── motor: histéresis-gate (BASE live) + overlay de vol targeting de cartera ──
def run_vt(data, calendar, expo, vt):
    """
    vt = {"est","win","target","mode"} | None (None = BASE sin overlay).
      g_today = min(1.0, target / vol_realizada)  (tope 1.0 → solo recorta, nunca apalanca)
      mode "entry": g escala el tamaño de compras NUEVAS (sin trim).
      mode "rebal": rebalancea cada posición a g·equity/K con banda 'band' (trim/add).
    """
    cash = CAP; kk = K; cfg = base_cfg()
    positions = {}; daily_eq = []; trades = []; fees = 0.0; inv = []
    qv = vt.get("_qqqvol") if vt else None
    win = vt["win"] if vt else 0
    band = vt.get("band", 0.20) if vt else 0
    prev_eq = CAP

    def g_of(d, equity):
        if not vt:
            return 1.0
        est = vt["est"]; rv = None
        if est == "qqq":
            rv = qv.get(d)
        elif est == "equity":
            if len(daily_eq) > win:
                rets = [daily_eq[i][1] / daily_eq[i - 1][1] - 1 for i in range(len(daily_eq) - win, len(daily_eq))]
                rv = statistics.pstdev(rets) * math.sqrt(252)
        elif est == "atr":
            ap = []
            for tk, p in positions.items():
                dd = data[tk]; i = dd["idx"].get(d)
                if i is not None and dd["pre"]["atr"][i]:
                    ap.append(dd["pre"]["atr"][i] / dd["bars"][i]["c"])
            if ap:
                rv = statistics.mean(ap) * math.sqrt(252)
        if rv is None or rv <= 0:
            return 1.0
        return min(1.0, vt["target"] / rv)

    for d in calendar:
        e = expo.get(d, 1.0); cap_k = max(0, round(e * kk))

        # 1) salidas chandelier
        to_exit = []
        for tk, p in positions.items():
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None or i + 1 >= len(dd["bars"]):
                continue
            b = dd["bars"][i]; p["hh"] = max(p["hh"], b["h"]); a = dd["pre"]["atr"][i]
            if a is not None and b["c"] < p["hh"] - MULT * a:
                to_exit.append((tk, i))
        for tk, i in to_exit:
            dd = data[tk]; xo = dd["bars"][i + 1]["o"]; p = positions.pop(tk)
            cash += p["shares"] * xo - FEE; fees += FEE
            trades.append({"tk": tk, "pnl_pct": (xo / p["entry"] - 1) * 100, "held": i - p["eidx"], "kind": "chand"})

        equity_now = cash + sum(positions[t]["shares"] * data[t]["bars"][data[t]["idx"][d]]["c"]
                                for t in positions if d in data[t]["idx"])
        g = g_of(d, equity_now)

        # 1b) REBAL: ajustar holdings a g·equity/K con banda
        if vt and vt["mode"] == "rebal" and positions:
            tgt = g * equity_now / kk
            for tk in list(positions):
                dd = data[tk]; i = dd["idx"].get(d)
                if i is None or i + 1 >= len(dd["bars"]):
                    continue
                cur = positions[tk]["shares"] * dd["bars"][i]["c"]
                if abs(tgt - cur) > band * (equity_now / kk):
                    xo = dd["bars"][i + 1]["o"]; new_sh = tgt / xo if xo > 0 else positions[tk]["shares"]
                    delta = new_sh - positions[tk]["shares"]
                    if delta > 0 and delta * xo > cash:   # no apalancar: limitar compra al cash
                        new_sh = positions[tk]["shares"] + max(0.0, cash / xo); delta = new_sh - positions[tk]["shares"]
                    cash -= delta * xo + FEE; fees += FEE
                    positions[tk]["shares"] = new_sh

        # 2) relleno de slots (gate breakout idéntico), tamaño × g
        free = cap_k - len(positions)
        if free > 0:
            cands = []
            for tk in data:
                if tk in positions:
                    continue
                dd = data[tk]; i = dd["idx"].get(d)
                if i is None or i < WARM or i + 1 >= len(dd["bars"]):
                    continue
                c = dd["bars"][i]["c"]; sma = dd["pre"]["sma"][i]; bkh = dd["pre"]["bkh"][i]
                if not (sma and c > sma) or not (bkh and c >= bkh):
                    continue
                s = signal_val(dd["pre"], i, cfg["rank"])
                if s is not None:
                    cands.append((s, tk, i))
            cands.sort(reverse=True)
            for s, tk, i in cands[:free]:
                dd = data[tk]; eo = dd["bars"][i + 1]["o"]
                base = equity_now / kk
                a = dd["pre"]["atr"][i]; atr_pct = (a / dd["bars"][i]["c"]) if a else VOL_TARGET
                base *= min(1.0, VOL_TARGET / atr_pct) if atr_pct > 0 else 1.0
                base *= g                                      # overlay de cartera
                target = min(cash, base)
                if target <= FEE + 1:
                    continue
                sh = (target - FEE) / eo
                cash -= sh * eo + FEE; fees += FEE
                positions[tk] = {"shares": sh, "entry": eo, "hh": dd["bars"][i + 1]["h"], "eidx": i + 1}

        eq = cash + sum(positions[tk]["shares"] * data[tk]["bars"][data[tk]["idx"][d]]["c"]
                        for tk in positions if d in data[tk]["idx"])
        daily_eq.append((d, eq)); inv.append(min(1.0, (eq - cash) / eq) if eq > 0 else 0); prev_eq = eq

    return {"deq": daily_eq, "trades": trades, "fees": fees,
            "invested": statistics.mean(inv) if inv else 0}


def row(name, res):
    s = summarize(res)
    return (f"  {name:<32} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {ann_vol(res['deq']):>5.1f}% "
            f"{s['worst_m']:>+6.1f}% {s['trades']:>6} {s['invested']*1:>4.0f}% {s['fees']:>6.0f}")


def main():
    uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]
    print(f"\n{'#'*116}")
    print(f"  ESTUDIO D — VOL TARGETING DE CARTERA (tope 1.0x, solo recorta) | top-80 K={K} | BASE = histéresis ±3% gate | desde 2020")
    print(f"{'#'*116}")
    data, idx_obj, qqq = load_data(uni, START_S, CACHE_S)
    cal = build_calendar(data)
    print(f"  {len(data)}/{len(uni)} con historia | {len(cal)} días | régimen...")
    feats = regime_features(qqq, data, cal)
    hyst = expo_series(cal, feats, "hysteresis", {"band": 0.03})
    qv20, qv60 = qqq_vol_series(qqq, 20), qqq_vol_series(qqq, 60)

    hdr = (f"  {'Variante':<32} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'Vol%':>5} {'PeorMes':>7} "
           f"{'Trades':>6} {'%Inv':>5} {'Fees$':>6}")

    base = run_vt(data, cal, hyst, None)
    print(f"\n  ── 1) BASE (histéresis ±3% gate, sin overlay) vs overlays ENTRY-ONLY ──")
    print(hdr); print(f"  {'-'*92}")
    print(row("BASE (sin vol targeting)", base))
    results = {"BASE": base}
    # entry-only: barrido estimador × target (win 60 para realizada, qqq y equity; atr es instantánea)
    for est, qvref in [("qqq", qv60), ("equity", None), ("atr", None)]:
        for tgt in (0.15, 0.20, 0.25, 0.30):
            vt = {"est": est, "win": 60, "target": tgt, "mode": "entry", "_qqqvol": qvref}
            res = run_vt(data, cal, hyst, vt)
            nm = f"VT {est} t{int(tgt*100)} (entry)"
            results[nm] = res; print(row(nm, res))
        print()

    print(f"  ── 2) Mejor familia en REBAL (control fuerte, banda 0.20) — ¿el techo del overlay? ──")
    print(hdr); print(f"  {'-'*92}")
    print(row("BASE (sin vol targeting)", base))
    for est, qvref in [("qqq", qv60), ("equity", None)]:
        for tgt in (0.20, 0.25):
            vt = {"est": est, "win": 60, "target": tgt, "mode": "rebal", "band": 0.20, "_qqqvol": qvref}
            res = run_vt(data, cal, hyst, vt)
            nm = f"VT {est} t{int(tgt*100)} (rebal)"
            results[nm] = res; print(row(nm, res))

    # ── 3) ¿corta el DD risk-on de feb-2021? peores episodios ──
    print(f"\n  ── 3) PEORES DRAWDOWNS — ¿el overlay corta el −20.7% risk-on de feb-2021? ──")
    pick = ["BASE", "VT qqq t20 (entry)", "VT equity t20 (entry)", "VT qqq t20 (rebal)"]
    for nm in pick:
        if nm not in results:
            continue
        print(f"    {nm}:")
        for ep in dd_episodes(results[nm]["deq"], 3):
            print(f"      {ep['peak_d']} → {ep['trough_d']}  {ep['depth']*100:>+6.1f}%  (recupera {ep['recover'] or 'abierto'})")

    # ── 4) robustez de los candidatos ──
    print(f"\n  ── 4) ROBUSTEZ (año-a-año + split-half) ──")
    fin = [nm for nm in ["BASE", "VT qqq t20 (entry)", "VT equity t20 (entry)", "VT qqq t20 (rebal)"] if nm in results]
    yrs = sorted(yearly(results["BASE"]["deq"]).keys())
    print(f"  {'Variante':<24} " + " ".join(f"{y:>7}" for y in yrs))
    for nm in fin:
        yr = yearly(results[nm]["deq"]); print(f"  {nm:<24} " + " ".join(f"{yr.get(y,0):>+6.0f}%" for y in yrs))
    print(f"\n  {'Variante':<24} {'20-22 Sh':>9} {'20-22 DD':>9} {'23-26 Sh':>9} {'23-26 DD':>9}")
    for nm in fin:
        deq = results[nm]["deq"]
        h1 = [(d, e) for d, e in deq if d < "2023-01-01"]; h2 = [(d, e) for d, e in deq if d >= "2023-01-01"]
        p1, p2 = perf(h1), perf(h2)
        print(f"  {nm:<24} {p1['sharpe']:>9.2f} {p1['mdd']:>+8.1f}% {p2['sharpe']:>9.2f} {p2['mdd']:>+8.1f}%")

    print(f"\n{'='*116}")
    print("  Lectura: el overlay gana SOLO si baja MaxDD y/o sube Sharpe SIN recortar el CAGR a la par.")
    print("  Mirá 'Vol%': si baja mucho pero el CAGR cae igual → Sharpe ~igual = solo bajaste tamaño (no sirve).")
    print("  Clave: ¿corta el episodio risk-on de feb-2021 que la histéresis no tocó? Si no, el overlay no aporta.")
    print(f"{'='*116}\n")


if __name__ == "__main__":
    main()
