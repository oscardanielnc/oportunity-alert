import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
ESTUDIO C — CAPA DE RÉGIMEN MACRO DE MAREA
==========================================
Pregunta de Oscar (2026-06-02): "el sistema cerró su fase de desarrollo; ahora quiero ir
probando ideas para sacar más rentabilidad". Decisión: la SELECCIÓN ya está exprimida y
validada (Estudios A/B/K: rotación, breakouts frescos, pullback, K — todo medido). El edge
que QUEDA no está en QUÉ comprar, sino en la capa de RÉGIMEN: cuándo y CUÁNTO estar expuesto.

Hoy el macro es el más crudo posible: binario `QQQ close > SMA200` que SOLO bloquea entradas
nuevas (nunca vende). Es todo-o-nada y hace whipsaw en la línea de los 200. Aquí abrimos esa
capa sobre EL MISMO arnés validado (top-80, fee $1/lado, open+1, vol-sizing 0.03, K=5, gate
breakout + chandelier 4×ATR) — NO se toca la selección.

  SEÑAL de régimen (sobre QQQ + amplitud del universo):
    binary      : QQQ>SMA200 (réplica del live = sanity)
    hysteresis  : risk-on si QQQ>SMA200·(1+band), risk-off si <·(1−band), pegajoso en medio (anti-whipsaw)
    slope       : QQQ>SMA200 Y SMA200 subiendo (pendiente 20d > 0)
    stack       : QQQ>SMA50>SMA200 (tendencia apilada)
    breadth     : exposición = fracción del top-80 sobre su propia SMA200 (mapeada 30%→0, 70%→1)
    graded      : exposición escalonada {0·25·50·75·100%} según cuántas de
                  {QQQ>SMA200, QQQ>SMA50, SMA50>SMA200, pendiente200>0} se cumplen

  MECANISMO (cómo actúa la exposición ∈[0,1]):
    gate    : solo limita slots de compra nueva, NUNCA vende (= comportamiento actual generalizado)
    size    : escala el TAMAÑO en dólares de cada compra nueva por la exposición (suaviza todo-o-nada)
    derisk  : además VENDE los holders más débiles cuando el régimen cae (circuit-breaker; el lever
              más fuerte sobre el MaxDD, sin medir hasta ahora)

OBJETIVO (igual al Estudio A): ganar = mejor RIESGO-AJUSTADO (Sharpe↑ y MaxDD no peor) Y ROBUSTO
(año-a-año + split-half + bear 2022 adentro). Más trades/fees con igual o peor Sharpe = churn.

REPRODUCIR:
    python backtest_marea_regime.py              # tabla principal (datos desde 2020, bear-inclusive)
    python backtest_marea_regime.py --robust     # + año-a-año y split-half de los finalistas

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-02) — VEREDICTO: SÍ HAY EDGE EN EL RÉGIMEN. El gate binario crudo es subóptimo.
GANADOR ROBUSTO = HISTÉRESIS (anti-whipsaw). El derisk (vender en el bear) es un upgrade opcional.

Tabla (top-80, K=5, gate breakout + chand 4×ATR, datos desde 2020, 2022 = bear operado):
                              CAGR   Sharpe  MaxDD   2022   22DD  Trades  | split-half (Sh 20-22 / 23-26)
  BASE binario (live)        +35.8%   1.35  -23.8%  -17%  -18.8%   107    | 0.88 / 1.69
  Histéresis ±3% (gate)      +40.0%   1.58  -20.7%   -6%   -9.4%    94    | 1.14 / 1.92   ← GANADOR
  Histéresis ±3% DERISK      +39.0%   1.60  -20.7%   -7%   -9.6%    97    | 1.12 / 1.98   ← upgrade
  Binario DERISK             +40.2%   1.61  -20.7%   -8%  -10.6%   126    | 1.10 / 2.01   (más churn)
  Stack QQQ>50>200 (gate)    +55.3%   1.61  -22.2%   -8%   -9.1%    87    | 1.23 / 1.95   (ver CAUTELA)

POR QUÉ GANA LA HISTÉRESIS (cushion ±3% alrededor de la SMA200 antes de cambiar de régimen):
  - El gate binario hace whipsaw: apaga el riesgo apenas QQQ toca por debajo de la SMA200 y lo
    re-enciende apenas la toca por arriba → cruces ida-y-vuelta falsos justo en los pivotes.
  - El cushion exige convicción (QQQ 3% sobre/bajo la SMA200) antes de cambiar de estado → mata
    los flip-flops. Resultado: MEJOR en TODOS los ejes a la vez Y con MENOS trades (107→94, menos
    fees) — lo opuesto al churn. El bear 2022 pasa de −17% a −6%.
  - NO es sobreajuste: meseta PLANA en el vecindario (±2/±3/±4% ≈ Sharpe 1.56-1.58, MaxDD −20.7,
    2022 −6%; ±5% recién degrada). Gana en AMBAS mitades OOS. Mecanismo económicamente sólido
    (anti-whipsaw es mejora clásica de momentum). Stateless-friendly: la ruta de histéresis se
    recalcula determinísticamente desde el histórico de QQQ cada día, sin persistir estado.

DERISK (vender los holders más débiles cuando el régimen cae, no solo dejar de comprar):
  - Baja aún más el DD de la mitad reciente (−13.9% combo vs −21.9% base) y sube Sharpe. PERO es
    un cambio de COMPORTAMIENTO mayor (Oscar recibiría señales de VENDER por régimen, no solo
    "no comprar"). El combo con histéresis evita el churn del derisk puro (97 vs 126 trades).
  - Decisión de Oscar: ¿quiere que el sistema le diga "salí del libro" en quiebres de mercado, o
    prefiere que el chandelier por-nombre gestione la salida? Fase 2.

CAUTELA — Stack (QQQ>SMA50>SMA200): CAGR +55% es tentador pero el exceso se CONCENTRA en 2024
  (+94%) y 2026 (+115%) → mayor dependencia del sample (melt-ups limpios); su MaxDD (−22.2%) es
  peor que la histéresis. Candidato a estudio aparte, NO primer movimiento. RECHAZADAS: breadth-gate
  (≈base), graduado-size (mata retorno), stack-derisk (churn 201 trades).

SIGUIENTE PASO si Oscar adopta: cambiar `macro_ok()` en pilot/momentum_signals.py de binario a
histéresis ±3% (recalculada desde el histórico QQQ, sin estado persistido). Cambio mínimo y de
bajo riesgo sobre el motor validado; la selección NO se toca.
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
import os, sys, json, argparse, statistics
try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows cp1252 no soporta →/× en consola
except Exception:
    pass

from backtest_marea_selection import (
    load_data, base_cfg, signal_val, perf, year_perf, summarize,
    K, MULT, WARM, VOL_TARGET, FEE, CAP, DATA_DIR,
)
from backtest_portfolio_momentum import build_calendar, _close_on

START_S = "2020-01-01T00:00:00Z"   # 2020-21 calientan indicadores, 2022 = bear REAL operable
CACHE_S = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selection_cache_2020.json")


# ── features de régimen del mercado, precomputados una vez sobre el calendario ──
def _sma(seq, n, i):
    return sum(seq[i-n+1:i+1]) / n if i >= n - 1 else None


def regime_features(qqq, data, calendar):
    """Para cada fecha del calendario: features de QQQ (sma50/200, pendiente) + amplitud del universo."""
    qc = [b["c"] for b in qqq]
    qidx = {b["t"]: i for i, b in enumerate(qqq)}
    feats = {}
    for d in calendar:
        i = qidx.get(d)
        f = {"px": None, "sma50": None, "sma200": None, "slope_up": False, "breadth": None}
        if i is not None and i >= 200:
            px = qc[i]; s200 = _sma(qc, 200, i); s50 = _sma(qc, 50, i)
            s200_prev = _sma(qc, 200, i - 20) if i >= 220 else None
            f.update(px=px, sma50=s50, sma200=s200,
                     slope_up=(s200_prev is not None and s200 > s200_prev))
        # amplitud: fracción del universo con cierre > su propia SMA200 ese día
        above = tot = 0
        for tk in data:
            dd = data[tk]; j = dd["idx"].get(d)
            if j is None or j < WARM:
                continue
            sma = dd["pre"]["sma"][j]
            if sma:
                tot += 1
                if dd["bars"][j]["c"] > sma:
                    above += 1
        f["breadth"] = (above / tot) if tot else None
        feats[d] = f
    return feats


def expo_series(calendar, feats, signal, params):
    """Convierte features → exposición ∈[0,1] por fecha, según la señal de régimen elegida."""
    out = {}
    state = 1.0  # para histéresis (arranca risk-on)
    for d in calendar:
        f = feats[d]
        px, s50, s200 = f["px"], f["sma50"], f["sma200"]
        if s200 is None:
            out[d] = 1.0          # sin datos → no bloquear (igual que el live)
            continue
        if signal == "binary":
            e = 1.0 if px > s200 else 0.0
        elif signal == "hysteresis":
            band = params.get("band", 0.03)
            if px > s200 * (1 + band):
                state = 1.0
            elif px < s200 * (1 - band):
                state = 0.0
            e = state
        elif signal == "slope":
            e = 1.0 if (px > s200 and f["slope_up"]) else 0.0
        elif signal == "stack":
            e = 1.0 if (s50 and px > s50 > s200) else 0.0
        elif signal == "breadth":
            b = f["breadth"]
            lo, hi = params.get("lo", 0.30), params.get("hi", 0.70)
            e = 1.0 if b is None else max(0.0, min(1.0, (b - lo) / (hi - lo)))
        elif signal == "graded":
            conds = [px > s200, bool(s50 and px > s50), bool(s50 and s50 > s200), f["slope_up"]]
            e = sum(conds) / 4.0
        else:
            raise ValueError(signal)
        out[d] = e
    return out


# ── motor: idéntico a la BASE validada salvo la capa de exposición de régimen ──
def run_regime(data, calendar, cfg, expo):
    """
    Igual que el run() del Estudio A (gate breakout + chandelier 4×ATR + vol-sizing + open+1),
    pero la exposición de régimen 'expo[fecha]' ∈[0,1] modula el libro según cfg['regime_mode']:
      gate   : cap de slots = round(expo·K); NUNCA vende (solo no añade)
      size   : slots = K; tamaño de cada compra nueva ×= expo
      derisk : cap de slots = round(expo·K) y VENDE los holders de menor señal hasta cumplirlo
    """
    cash = CAP
    kk = cfg.get("K", K)
    mode = cfg.get("regime_mode", "gate")
    positions = {}
    daily_eq, trades = [], []
    fees = 0.0
    invested_frac = []

    def sig_of(tk, i):
        v = signal_val(data[tk]["pre"], i, cfg["rank"])
        return v

    for d in calendar:
        e = expo.get(d, 1.0)
        cap_k = kk if mode == "size" else max(0, round(e * kk))

        # ---------- 1) SALIDAS (chandelier idéntico) ----------
        to_exit = []
        for tk, p in positions.items():
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None or i + 1 >= len(dd["bars"]):
                continue
            b = dd["bars"][i]; p["hh"] = max(p["hh"], b["h"])
            a = dd["pre"]["atr"][i]
            if a is not None and b["c"] < p["hh"] - cfg["mult"] * a:
                to_exit.append((tk, i, "chand"))

        # ---------- 1b) DERISK: vender holders más débiles si el cap de régimen bajó ----------
        if mode == "derisk":
            rule = cfg.get("derisk_rule", "weak")   # weak=menor señal | loser=peor P&L
            held = [tk for tk in positions if tk not in {t for t, _, _ in to_exit}]
            over = len(held) - cap_k
            if over > 0:
                ranked = []
                for tk in held:
                    dd = data[tk]; i = dd["idx"].get(d)
                    if i is None or i + 1 >= len(dd["bars"]):
                        continue
                    if rule == "loser":
                        key = dd["bars"][i]["c"] / positions[tk]["entry"] - 1   # P&L no realizado
                    else:
                        s = sig_of(tk, i); key = s if s is not None else -1e9
                    ranked.append((key, tk, i))
                ranked.sort()  # menor (señal o P&L) primero
                for _, tk, i in ranked[:over]:
                    to_exit.append((tk, i, "regime"))

        for tk, i, kind in to_exit:
            dd = data[tk]; xo = dd["bars"][i + 1]["o"]; p = positions.pop(tk)
            cash += p["shares"] * xo - FEE; fees += FEE
            trades.append({"tk": tk, "pnl_pct": (xo / p["entry"] - 1) * 100,
                           "held": i - p["eidx"], "kind": kind,
                           "exit_date": dd["bars"][i + 1]["t"],
                           "exit_idx": i + 1, "exit_px": xo})

        # ---------- 2) RELLENO de slots libres (gate breakout, idéntico) ----------
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
                if not (sma and c > sma):
                    continue
                if not (bkh and c >= bkh):     # gate breakout (BASE)
                    continue
                s = sig_of(tk, i)
                if s is not None:
                    cands.append((s, tk, i))
            cands.sort(reverse=True)
            equity_now = cash + sum(positions[t]["shares"] * _close_on(data[t], d) for t in positions)
            for s, tk, i in cands[:free]:
                dd = data[tk]; eo = dd["bars"][i + 1]["o"]
                base = equity_now / kk
                a = dd["pre"]["atr"][i]; atr_pct = (a / dd["bars"][i]["c"]) if a else VOL_TARGET
                base *= min(1.0, VOL_TARGET / atr_pct) if atr_pct > 0 else 1.0
                if mode == "size":
                    base *= e
                target = min(cash, base)
                if target <= FEE + 1:
                    continue
                sh = (target - FEE) / eo
                cash -= sh * eo + FEE; fees += FEE
                positions[tk] = {"shares": sh, "entry": eo, "hh": dd["bars"][i + 1]["h"],
                                 "edate": dd["bars"][i + 1]["t"], "eidx": i + 1}

        eq = cash + sum(positions[tk]["shares"] * _close_on(data[tk], d) for tk in positions)
        daily_eq.append((d, eq))
        invested_frac.append(len(positions) / kk)

    return {"deq": daily_eq, "trades": trades, "fees": fees,
            "invested": statistics.mean(invested_frac) if invested_frac else 0}


# ── variantes: puntos coherentes (señal × mecanismo), no celdas al azar ───────
def variants():
    bc = lambda **kw: base_cfg(**kw)
    return [
        # nombre,                            señal,        params,            cfg(mecanismo)
        ("BASE binario (live)",              "binary",     {},                bc(regime_mode="gate")),
        # -- anti-whipsaw: histéresis (solo cambia CUÁNDO cruza el gate) — VECINDARIO de banda --
        ("Histéresis ±2% (gate)",            "hysteresis", {"band": 0.02},    bc(regime_mode="gate")),
        ("Histéresis ±3% (gate)",            "hysteresis", {"band": 0.03},    bc(regime_mode="gate")),
        ("Histéresis ±4% (gate)",            "hysteresis", {"band": 0.04},    bc(regime_mode="gate")),
        ("Histéresis ±5% (gate)",            "hysteresis", {"band": 0.05},    bc(regime_mode="gate")),
        ("Histéresis ±3% DERISK (combo)",    "hysteresis", {"band": 0.03},    bc(regime_mode="derisk")),
        # -- confirmación de tendencia --
        ("Pendiente SMA200 (gate)",          "slope",      {},                bc(regime_mode="gate")),
        ("Stack QQQ>50>200 (gate)",          "stack",      {},                bc(regime_mode="gate")),
        # -- amplitud del universo --
        ("Breadth 30→70 (gate)",             "breadth",    {},                bc(regime_mode="gate")),
        ("Breadth 30→70 (size)",             "breadth",    {},                bc(regime_mode="size")),
        # -- exposición graduada (suaviza todo-o-nada) --
        ("Graduado (size)",                  "graded",     {},                bc(regime_mode="size")),
        ("Graduado (derisk)",                "graded",     {},                bc(regime_mode="derisk")),
        # -- circuit-breaker: vender en el bear en vez de solo no comprar --
        ("Binario DERISK (vende en bear)",   "binary",     {},                bc(regime_mode="derisk")),
        ("Histéresis ±5% DERISK",            "hysteresis", {"band": 0.05},    bc(regime_mode="derisk")),
        ("Stack DERISK",                     "stack",      {},                bc(regime_mode="derisk")),
        ("Breadth DERISK",                   "breadth",    {},                bc(regime_mode="derisk")),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--robust", action="store_true", help="añade año-a-año + split-half de finalistas")
    args = ap.parse_args()

    uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]

    print(f"\n{'#'*112}")
    print(f"  ESTUDIO C — CAPA DE RÉGIMEN | top-80 | K={K} gate breakout + chand {MULT}×ATR + vol "
          f"| fee ${FEE}/lado | datos desde 2020 (2022 = bear real)")
    print(f"{'#'*112}")
    data, idx_obj, qqq = load_data(uni, START_S, CACHE_S)
    cal = build_calendar(data)
    print(f"  {len(data)}/{len(uni)} con historia | {len(cal)} días | calculando features de régimen...")
    feats = regime_features(qqq, data, cal)

    # bear 2022 de referencia
    q22 = [(b["t"], b["c"]) for b in qqq if b["t"][:4] == "2022"]
    q22ret = (q22[-1][1] / q22[0][1] - 1) * 100 if len(q22) > 5 else 0
    q22dd = perf([(d, c) for d, c in q22])["mdd"] if len(q22) > 5 else 0
    print(f"  Referencia bear: QQQ 2022 ret {q22ret:+.0f}% | MaxDD {q22dd:+.1f}%\n")

    print(f"  {'Variante':<30} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'PeorMes':>7} "
          f"{'2022':>6} {'22DD':>6} {'Trades':>6} {'%Inv':>5} {'Fees$':>6}")
    print(f"  {'-'*98}")
    results = {}
    for name, sig, params, cfg in variants():
        expo = expo_series(cal, feats, sig, params)
        res = run_regime(data, cal, cfg, expo)
        s = summarize(res); results[name] = (s, res)
        y22 = year_perf(res["deq"], "2022")
        r22 = f"{y22['total']:>+4.0f}%" if y22 else " n/a "
        d22 = f"{y22['mdd']:>+5.1f}%" if y22 else " n/a "
        print(f"  {name:<30} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {s['worst_m']:>+6.1f}% "
              f"{r22:>6} {d22:>6} {s['trades']:>6} {s['invested']:>4.0f}% {s['fees']:>6.0f}")

    # benchmark QQQ
    qdeq = [(b["t"], CAP * b["c"] / qqq[0]["c"]) for b in qqq if b["t"] >= cal[0]]
    sb = perf(qdeq)
    print(f"  {'-'*98}")
    print(f"  {'QQQ buy&hold':<30} {sb['cagr']:>+5.1f}% {sb['sharpe']:>6.2f} {sb['mdd']:>+6.1f}% {sb['worst_m']:>+6.1f}%")

    if args.robust:
        names = [v[0] for v in variants()]
        base_deq = results["BASE binario (live)"][1]["deq"]
        from backtest_marea_selection import yearly
        years = sorted(yearly(base_deq).keys())
        print(f"\n  DESGLOSE POR AÑO (retorno %):")
        print(f"  {'Variante':<30} " + " ".join(f"{y:>7}" for y in years))
        print(f"  {'-'*(30+8*len(years))}")
        for name in names:
            yr = yearly(results[name][1]["deq"])
            print(f"  {name:<30} " + " ".join(f"{yr.get(y, 0):>+6.0f}%" for y in years))

        print(f"\n  SPLIT-HALF OOS (Sharpe | MaxDD):")
        print(f"  {'Variante':<30} {'20-22 Sh':>9} {'20-22 DD':>9} {'23-26 Sh':>9} {'23-26 DD':>9}")
        print(f"  {'-'*70}")
        for name in names:
            deq = results[name][1]["deq"]
            h1 = [(d, eq) for d, eq in deq if d < "2023-01-01"]
            h2 = [(d, eq) for d, eq in deq if d >= "2023-01-01"]
            if not h1 or not h2:
                continue
            p1, p2 = perf(h1), perf(h2)
            print(f"  {name:<30} {p1['sharpe']:>9.2f} {p1['mdd']:>+8.1f}% {p2['sharpe']:>9.2f} {p2['mdd']:>+8.1f}%")

    print(f"\n{'='*112}")
    print("  Lectura: el GANADOR debe SUBIR Sharpe SIN empeorar MaxDD vs BASE binario, y aguantar el bear")
    print("  2022 (ret/DD) mejor o igual. 'derisk' debería bajar el DD pero ojo al whipsaw (CAGR/fees).")
    print("  'size'/'graded' suavizan el todo-o-nada. Si nada bate a BASE robusto → el binario está bien.")
    print(f"{'='*112}\n")


if __name__ == "__main__":
    main()
