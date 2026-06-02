import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
ESTUDIO H — SIZING DE POSICIONES: equal-weight vs vol-sizing, parámetros, y el cash ocioso
=========================================================================================
Preguntas de Oscar (2026-06-02):
  1. ¿Qué cambia vs comprar cada uno de los 5 al 20% (equal-weight, 100% invertido)? ¿+retorno? ¿+riesgo?
  2. ¿Qué parámetros/esquemas que ajustan el %equity dan MÁS beneficio y MENOS riesgo?
  3. Si mis 5 posiciones suman ~60% del capital, ¿qué hago con el 40% restante? ¿más acciones?

Sobre el arnés validado (histéresis ±3% gate + breakout + chandelier 4×ATR, top-K mom126, bear-inclusive
desde 2020, fee $1/lado). SOLO cambia el SIZING. Esquemas:
  equal      : equity/K (20% c/u) → ~100% invertido. ESTO es "comprar cada uno al 20%".
  invvol     : equity/K × min(1, VOL_TARGET/atr%)  ← EL ACTUAL (live). Volátiles entran chicos → deja cash.
  risk2stop  : arriesgar R% del equity hasta el stop 4×ATR: pos$ = R·equity/(4·atr%), tope 20%.
EL CASH (pregunta 3): el "40% ocioso" del invvol aparece porque los líderes de Marea son volátiles
(>3%/día) → entran <20%. Opciones para usarlo: (a) NO vol-sizear (equal=100% invertido) o (b) MÁS nombres
(subir K). Ambas se miden. Reportamos %Inv y VOL anualizada para que el riesgo sea explícito.

CLAVE conceptual: risk2stop y invvol son la MISMA familia (ambos sizean ∝ 1/atr%, tope 20%); R≈1.25·VT.
Lo medimos para mostrarlo. El esquema REALMENTE distinto es equal (sin escalar por vol) = +invertido/+riesgo.

REPRODUCIR: python backtest_marea_sizing.py

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-02, bear-inclusive) — el vol-sizing actual está BIEN CALIBRADO; el mejor uso del
cash es MÁS NOMBRES (K=8), no posiciones más grandes.

P1 — Equal-weight 20% vs InvVol 0.03 (actual):
  Equal-20: CAGR +55.6% Sharpe 1.56 MaxDD −23.7% Vol 31.8%  |  InvVol0.03: +40.0% 1.58 −20.7% 22.8%
  → Equal da +RETORNO pero +RIESGO proporcional; Sharpe IGUAL. No es mejor, es más riesgoso. El
    vol-sizing no desperdicia plata: corre más suave por el mismo riesgo-ajustado. = decisión de riesgo.
P2 — Barrido VOL_TARGET (dial riesgo/retorno, Sharpe tiene pico):
  0.02→1.52 · 0.025→1.54 · 0.03→1.58 (LIVE, mejor DD) · 0.04→1.59 (+48% pero DD −22.7) · 0.05→1.56 · equal→1.56
  → VT=0.03 está en el pico de Sharpe con el mejor DD. NO al tanteo, bien calibrado. VT=0.04 = nudge
    marginal más agresivo. Risk2stop R=2% ≡ InvVol 0.025 EXACTO → misma familia (confirmado), no nuevo.
P3 — El 40% de cash (hallazgo accionable):
  Usarlo agrandando las 5 (equal) = +riesgo mismo Sharpe. Usarlo en MÁS NOMBRES:
  InvVol K=8: CAGR +47.4% Sharpe 1.59 MaxDD −15.8% (vs −20.7!) Vol 22.4% → DOMINA (más retorno, MEJOR
  DD, misma vol). Robusto (split-half DD temprano −15.0 vs −20.7). El cash no es ineficiencia (es
  amortiguador), pero si se usa, el camino es DIVERSIFICAR (K↑), no concentrar. Costo: 8 nombres
  manuales en eToro vs 5 (operativo). Coincide y refuerza el K-sweep del Estudio B (K=8 sweet spot;
  acá el beneficio de DD es más visible con el gate de histéresis).

DECISIÓN DE OSCAR: mantener K5/VT0.03 (validado, óptimo para 5 manuales) | subir a K8 (mejor DD/retorno,
8 manuales) | nudge VT0.04 (marginal). NO equal-weight (solo sube riesgo). Sin cambio live por defecto.
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
import os, sys, json, statistics, math
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_marea_regime import regime_features, expo_series, START_S, CACHE_S
from backtest_marea_selection import (
    load_data, signal_val, perf, yearly, summarize, K, WARM, VOL_TARGET, FEE, CAP, MULT, DATA_DIR,
)
from backtest_portfolio_momentum import build_calendar


def ann_vol(deq):
    r = [deq[i][1] / deq[i - 1][1] - 1 for i in range(1, len(deq)) if deq[i - 1][1] > 0]
    return statistics.pstdev(r) * math.sqrt(252) * 100 if len(r) > 1 else 0


def run_sized(data, cal, expo, scheme="invvol", vt=VOL_TARGET, R=0.02, kk=K):
    cash = CAP; positions = {}; deq = []; trades = []; fees = 0.0; inv = []
    for d in cal:
        e = expo.get(d, 1.0); cap_k = max(0, round(e * kk))
        to_exit = []
        for tk, p in positions.items():
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None or i + 1 >= len(dd["bars"]): continue
            b = dd["bars"][i]; p["hh"] = max(p["hh"], b["h"]); a = dd["pre"]["atr"][i]
            if a is not None and b["c"] < p["hh"] - MULT * a:
                to_exit.append((tk, i))
        for tk, i in to_exit:
            dd = data[tk]; xo = dd["bars"][i + 1]["o"]; p = positions.pop(tk)
            cash += p["shares"] * xo - FEE; fees += FEE
            trades.append({"pnl_pct": (xo / p["entry"] - 1) * 100, "held": i - p["eidx"]})
        equity_now = cash + sum(positions[t]["shares"] * data[t]["bars"][data[t]["idx"][d]]["c"]
                                for t in positions if d in data[t]["idx"])
        free = cap_k - len(positions)
        if free > 0:
            cands = []
            for tk in data:
                if tk in positions: continue
                dd = data[tk]; i = dd["idx"].get(d)
                if i is None or i < WARM or i + 1 >= len(dd["bars"]): continue
                c = dd["bars"][i]["c"]; sma = dd["pre"]["sma"][i]; bkh = dd["pre"]["bkh"][i]
                if not (sma and c > sma) or not (bkh and c >= bkh): continue
                s = signal_val(dd["pre"], i, "mom126")
                if s is not None: cands.append((s, tk, i))
            cands.sort(reverse=True)
            for s, tk, i in cands[:free]:
                dd = data[tk]; eo = dd["bars"][i + 1]["o"]
                a = dd["pre"]["atr"][i]; atrp = (a / dd["bars"][i]["c"]) if a else None
                base = equity_now / kk
                if scheme == "equal" or not atrp or atrp <= 0:
                    t = base
                elif scheme == "invvol":
                    t = base * min(1.0, vt / atrp)
                elif scheme == "risk2stop":
                    t = min(base, R * equity_now / (4 * atrp))
                else:
                    t = base
                target = min(cash, t)
                if target <= FEE + 1: continue
                sh = (target - FEE) / eo; cash -= sh * eo + FEE; fees += FEE
                positions[tk] = {"shares": sh, "entry": eo, "hh": dd["bars"][i + 1]["h"], "eidx": i + 1}
        eq = cash + sum(positions[tk]["shares"] * data[tk]["bars"][data[tk]["idx"][d]]["c"]
                        for tk in positions if d in data[tk]["idx"])
        deq.append((d, eq)); inv.append(min(1.0, (eq - cash) / eq) if eq > 0 else 0)
    return {"deq": deq, "trades": trades, "fees": fees, "invested": statistics.mean(inv) if inv else 0}


def row(name, res):
    s = summarize(res)
    return (name, s, ann_vol(res["deq"]))


def main():
    uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]
    print(f"\n{'#'*112}")
    print(f"  ESTUDIO H — SIZING | Marea (histéresis gate + breakout + chand 4×ATR) | top-80 | desde 2020 (bear)")
    print(f"{'#'*112}")
    data, idx_obj, qqq = load_data(uni, START_S, CACHE_S)
    cal = build_calendar(data)
    feats = regime_features(qqq, data, cal)
    hyst = expo_series(cal, feats, "hysteresis", {"band": 0.03})

    variants = [
        ("Equal-weight 20% (K5, full)",  dict(scheme="equal", kk=5)),
        ("InvVol VT=0.02 (K5)",          dict(scheme="invvol", vt=0.02, kk=5)),
        ("InvVol VT=0.025 (K5)",         dict(scheme="invvol", vt=0.025, kk=5)),
        ("InvVol VT=0.03 (K5, LIVE)",    dict(scheme="invvol", vt=0.03, kk=5)),
        ("InvVol VT=0.04 (K5)",          dict(scheme="invvol", vt=0.04, kk=5)),
        ("InvVol VT=0.05 (K5)",          dict(scheme="invvol", vt=0.05, kk=5)),
        ("Risk2stop R=2% (K5)",          dict(scheme="risk2stop", R=0.02, kk=5)),
        ("--- usar el cash: +nombres ---", None),
        ("InvVol VT=0.03, K6",           dict(scheme="invvol", vt=0.03, kk=6)),
        ("InvVol VT=0.03, K8",           dict(scheme="invvol", vt=0.03, kk=8)),
        ("Equal-weight 20%... K6",       dict(scheme="equal", kk=6)),
        ("Equal-weight K8 (12.5% c/u)",  dict(scheme="equal", kk=8)),
    ]
    print(f"\n  {'Esquema':<32} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'Vol%':>5} {'PeorMes':>7} "
          f"{'WR%':>4} {'Trades':>6} {'%Inv':>5} {'Fees$':>6}")
    print(f"  {'-'*98}")
    results = {}
    for name, cfg in variants:
        if cfg is None:
            print(f"  {name}"); continue
        res = run_sized(data, cal, hyst, **cfg); results[name] = res
        s = summarize(res); v = ann_vol(res["deq"])
        print(f"  {name:<32} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {v:>5.1f}% "
              f"{s['worst_m']:>+6.1f}% {s['wr']:>3.0f}% {s['trades']:>6} {s['invested']:>4.0f}% {s['fees']:>6.0f}")

    # benchmark QQQ
    qdeq = [(b["t"], CAP * b["c"] / qqq[0]["c"]) for b in qqq if b["t"] >= cal[0]]
    sq = perf(qdeq)
    print(f"  {'-'*98}")
    print(f"  {'QQQ buy&hold':<32} {sq['cagr']:>+5.1f}% {sq['sharpe']:>6.2f} {sq['mdd']:>+6.1f}% {ann_vol(qdeq):>5.1f}%")

    # robustez de los finalistas
    fin = ["Equal-weight 20% (K5, full)", "InvVol VT=0.03 (K5, LIVE)", "InvVol VT=0.025 (K5)", "InvVol VT=0.03, K8"]
    fin = [f for f in fin if f in results]
    print(f"\n  ── ROBUSTEZ (año-a-año) ──")
    yrs = sorted(yearly(results[fin[0]]["deq"]).keys())
    print(f"  {'Esquema':<32} " + " ".join(f"{y:>7}" for y in yrs))
    for f in fin:
        yy = yearly(results[f]["deq"]); print(f"  {f:<32} " + " ".join(f"{yy.get(y,0):>+6.0f}%" for y in yrs))
    print(f"\n  ── SPLIT-HALF (Sharpe | MaxDD) ──")
    print(f"  {'Esquema':<32} {'20-22 Sh':>9} {'20-22 DD':>9} {'23-26 Sh':>9} {'23-26 DD':>9}")
    for f in fin:
        deq = results[f]["deq"]
        h1 = [(d, e) for d, e in deq if d < "2023-01-01"]; h2 = [(d, e) for d, e in deq if d >= "2023-01-01"]
        p1, p2 = perf(h1), perf(h2)
        print(f"  {f:<32} {p1['sharpe']:>9.2f} {p1['mdd']:>+8.1f}% {p2['sharpe']:>9.2f} {p2['mdd']:>+8.1f}%")

    print(f"\n{'='*112}")
    print("  Lectura — P1 (equal 20% vs invvol): comparar CAGR/Vol/MaxDD/Sharpe. Equal = +invertido (+riesgo).")
    print("  P2 (parámetros): el barrido VT muestra el dial riesgo/retorno; el mejor Sharpe/MaxDD robusto gana.")
    print("  P3 (el 40% cash): comparar 'usar el cash' (equal=más grande, o K6/K8=más nombres) vs dejarlo. Si")
    print("  subir %Inv no sube el Sharpe → el cash NO es ineficiencia, es la gestión de riesgo (vol baja).")
    print(f"{'='*112}\n")


if __name__ == "__main__":
    main()
