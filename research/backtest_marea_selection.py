import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
ESTUDIO A — MARCO DE SELECCIÓN / ROTACIÓN DE MAREA
==================================================
Pregunta de Oscar (2026-06-01): "quiero saber EXACTAMENTE qué vender, comprar o mantener
cada día. PED tiene horizonte; Marea no, y eso me hace ruido. Con solo 5 slots quiero los 5
que más van a subir. Ya probamos que rotar el top-10 no era lo mejor, pero no estoy convencido."

La prueba de rotación previa (backtest_marea_rotation.py) midió UNA forma: swap DIARIO del
holder más débil por un breakout más fuerte. Es la forma más churny posible. Aquí abrimos el
ESPACIO COMPLETO de diseño sobre EL MISMO arnés validado (universo top-80, fee $1/lado,
ejecución al OPEN del día siguiente, filtro macro QQQ>SMA200, vol-sizing 0.03 como el live):

  EJE 1 — Señal de ranking  : mom126(base) | mom63 | mom21 | mom10 | mom/ATR% (riesgo-aj) | aceleración
  EJE 2 — Regla de venta    : chandelier 4×ATR(base) | rank-exit(banda) | horizonte fijo | muerte-momentum | híbridos
  EJE 3 — Cadencia rotación : ninguna(base) | semanal | mensual  (cuándo se permite rank-exit/rebalanceo)
  EJE 4 — Relleno           : breakout+señal(base) | top-K señal puro (sin gate de breakout)

OBJETIVO (decisión de Oscar): ganar = mejor RIESGO-AJUSTADO (Sharpe ↑ y MaxDD no peor),
y ROBUSTO. Una variante solo "gana" si pasa: año-por-año + split-half OOS + vecindario de
parámetros. Más trades/fees con igual o peor Sharpe = churn que destruye el edge (lección Watcher).

Salida: panel por variante (CAGR/Sharpe/MaxDD/peor-mes/turnover/fees/%invertido/WR) vs BASE y QQQ,
desglose por año, y batería de robustez al ganador.

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-01) — VEREDICTO: NO CAMBIAR. BASE (gate breakout + chandelier) es el ganador
riesgo-ajustado robusto. La pregunta de rotación/horizonte queda CERRADA con datos.

El --mode grid/neighborhood (datos desde 2022) parecía mostrar mejoras grandes: quitar el gate
de breakout (Sharpe 1.27→1.48, MaxDD −25→−24) y añadir horizonte fijo (Sharpe hasta 1.92). PERO
con datos desde 2022 los indicadores recién se calientan a fines de ese año → EL BEAR 2022 NUNCA
SE OPERÓ. Era un sample sesgado al bull.

El --mode stress (datos desde 2020 → 2022 = bear REAL y operable) DA VUELTA todo:
  - BASE                       : CAGR +35.8% Sharpe 1.35 MaxDD −23.8%  ← MEJOR DD, top Sharpe
  - NoGate + chand             : Sharpe 1.27 MaxDD −34.0%  ← quitar el gate EMPEORA (el gate
                                  protege en chop/bear; sin él te agarran las reversiones)
  - NoGate + chand + horiz84   : Sharpe 1.39 MaxDD −32.8%  ← +Sharpe marginal, DD MUCHO peor
  - BASE + horiz84             : Sharpe 1.33 MaxDD −32.8%  ← capar por tiempo EMPEORA el DD
  - BASE + horiz126            : Sharpe 1.31 MaxDD −24.5%  ← cap largo ≈ no-op (casi no liga)
  - NoGate + horiz63 (sin stop): Sharpe 1.44 MaxDD −43.4%  ← sin stop = peligroso (2022 DD −28.6%)
  - Rank-band mensual 2K       : Sharpe 0.97 MaxDD −48.5%  ← COLAPSA en el bear (era 1.70 en 2022-start)

LECCIONES:
  1. El gate de breakout SE GANA su lugar: es protección de régimen, invisible en un sample bull.
  2. "Marea sin horizonte" NO es un defecto: el chandelier YA es la regla de venta definida
     (sale si el cierre rompe máx−4×ATR). Capar por tiempo corta la cola derecha → cuesta o no hace nada.
  3. Rotar a los líderes (rank-band) es un espejismo de bull; con el bear adentro hunde Sharpe y DD.
     Confirma y EXPLICA el rechazo previo (backtest_marea_rotation.py): no es que rinda poco, es que
     el DD se descontrola justo cuando importa.
  4. Único lever real: K (--mode ksweep, BASE bear-inclusive). MEDIDO:
       K=3-4 Sharpe 1.22-1.23 MaxDD −31% (más concentración EMPEORA) | K=5 Sharpe 1.35 MaxDD −23.8%
       (live) | K=8 Sharpe 1.39 MaxDD −22.1% (sweet spot) | K=10-12 se aplana/decae + fees suben.
     Veredicto: K=5 está bien elegido. K=8 es marginalmente mejor (Sharpe +0.04, DD +1.7pp) pero el
     gain es chico y no compensa +60% de trades/fees ni manejar 8 nombres manuales en eToro. NO bajar
     de 5. Mild upgrade a K=6-8 solo si algún día se automatiza la ejecución.

REPRODUCIR: python backtest_marea_selection.py --mode stress  (veredicto selección/rotación)
            python backtest_marea_selection.py --mode ksweep  (lever K)
El grid/neighborhood quedan como registro del artefacto-de-sample que el stress desmiente — NO
usarlos para decidir.
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
import os, sys, json, statistics, argparse
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()

from backtest_portfolio_momentum import (
    ALPACA_BASE, hdrs, build_calendar, _close_on,
)
import requests

# ── constantes del arnés validado ────────────────────────────────────────────
START = "2022-01-01T00:00:00Z"
CAP = 10000.0
FEE = 1.0
K = 5
BREAKOUT = 50
MULT = 4.0
REGIME = 200
ATR_N = 22
VOL_TARGET = 0.03
MOMS = [126, 63, 45, 30, 21, 15, 10]   # lookbacks de momentum precomputados
WARM = max(REGIME, max(MOMS), BREAKOUT) + 22 + 1   # +22 para aceleración (mom hace 21d)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selection_cache.json")


# ── fetch con caché en disco (itera barato; los datos diarios no cambian) ─────
def fetch_daily_cached(ticker, cache, start=START):
    if ticker in cache:
        return cache[ticker]
    bars, params = [], {"timeframe": "1Day", "start": start, "feed": "sip", "limit": 1000,
                        "sort": "asc", "adjustment": "all"}
    try:
        while True:
            r = requests.get(f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                             params=params, headers=hdrs(), timeout=30)
            r.raise_for_status()
            d = r.json(); chunk = d.get("bars") or []
            bars.extend(chunk)
            if not d.get("next_page_token"): break
            params["page_token"] = d["next_page_token"]
    except Exception as e:
        print(f"  fetch err {ticker}: {e}")
    out = [{"t": b["t"][:10], "o": float(b["o"]), "h": float(b["h"]),
            "l": float(b["l"]), "c": float(b["c"])} for b in bars]
    cache[ticker] = out
    return out


def precompute(bars):
    """Arrays alineados a bars: sma200, atr22, breakout-high(50), y momentum a varios lookbacks."""
    n = len(bars); closes = [b["c"] for b in bars]
    sma = [None]*n; atr = [None]*n; bkh = [None]*n
    moms = {m: [None]*n for m in MOMS}
    csum = 0.0
    for i in range(n):
        csum += closes[i]
        if i >= REGIME: csum -= closes[i-REGIME]
        if i >= REGIME-1: sma[i] = csum/REGIME
        if i >= ATR_N:
            trs = [max(bars[j]["h"]-bars[j]["l"], abs(bars[j]["h"]-bars[j-1]["c"]),
                       abs(bars[j]["l"]-bars[j-1]["c"])) for j in range(i-ATR_N+1, i+1)]
            atr[i] = sum(trs)/ATR_N
        if i >= BREAKOUT: bkh[i] = max(closes[i-BREAKOUT:i])
        for m in MOMS:
            if i >= m and closes[i-m] > 0:
                moms[m][i] = closes[i]/closes[i-m]-1
    return {"sma": sma, "atr": atr, "bkh": bkh, "mom": moms}


def signal_val(pre, i, rank):
    """Valor de la señal de ranking para el día i. None si falta dato."""
    if rank.startswith("mom") and rank[3:].isdigit():
        return pre["mom"][int(rank[3:])][i]
    if rank == "mom_atr":                       # momentum 126d por unidad de volatilidad (riesgo-aj)
        m = pre["mom"][126][i]; a = pre["atr"][i]
        return None if (m is None or not a) else m  # se divide por atr% abajo (en el motor, con close)
    if rank == "accel":                          # ¿el momentum 126d está SUBIENDO? (mom hoy - mom hace 21d)
        m, mp = pre["mom"][126][i], pre["mom"][126][i-21] if i >= 21 else None
        return None if (m is None or mp is None) else m - mp
    raise ValueError(rank)


# ── motor único, dirigido por config ─────────────────────────────────────────
def run(data, calendar, cfg, idx_obj):
    """
    cfg:
      rank   : señal de ranking (ver signal_val)
      gate   : "breakout" (base) | "none"  -> requisito de ENTRADA
      exits  : subconjunto de {"chandelier","time","momdeath"}  (rank-exit va por 'band')
      mult   : multiplicador chandelier
      hold   : días de tenencia para exit por tiempo
      cadence: None | "weekly" | "monthly"  -> días en que se permite rank-exit
      band   : None (sin rank-exit) | float  -> vende holder si su rank >= band*K
      gate   se aplica solo al relleno; la tenencia exige above_sma.
    """
    cash = CAP
    kk = cfg.get("K", K)
    positions = {}     # tk -> {shares, entry, hh, edate, eidx}
    daily_eq, trades = [], []
    fees = 0.0
    invested_frac = []

    def sig_of(tk, i):
        pre = data[tk]["pre"]; v = signal_val(pre, i, cfg["rank"])
        if v is None: return None
        if cfg["rank"] == "mom_atr":
            a = pre["atr"][i]; c = data[tk]["bars"][i]["c"]
            return v/(a/c) if (a and c and a > 0) else None
        return v

    for ci, d in enumerate(calendar):
        rebal = (cfg.get("cadence") == "weekly" and ci % 5 == 0) or \
                (cfg.get("cadence") == "monthly" and ci % 21 == 0)

        # pool rankeado de hoy (todo above_sma con señal) -> rank index para rank-exit
        rank_idx = {}
        if cfg.get("band") and rebal:
            pool = []
            for tk in data:
                dd = data[tk]; i = dd["idx"].get(d)
                if i is None or i < WARM: continue
                c = dd["bars"][i]["c"]; sma = dd["pre"]["sma"][i]
                if sma and c > sma:
                    s = sig_of(tk, i)
                    if s is not None: pool.append((s, tk))
            pool.sort(reverse=True)
            rank_idx = {tk: r for r, (_, tk) in enumerate(pool)}

        # ---------- 1) SALIDAS ----------
        to_exit = []
        for tk, p in positions.items():
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None or i+1 >= len(dd["bars"]): continue
            b = dd["bars"][i]; p["hh"] = max(p["hh"], b["h"])
            a = dd["pre"]["atr"][i]; sma = dd["pre"]["sma"][i]; m126 = dd["pre"]["mom"][126][i]
            hit = None
            if "chandelier" in cfg["exits"] and a is not None and b["c"] < p["hh"] - cfg["mult"]*a:
                hit = "chand"
            if hit is None and "time" in cfg["exits"] and (i - p["eidx"]) >= cfg["hold"]:
                hit = "time"
            if hit is None and "momdeath" in cfg["exits"] and \
               ((m126 is not None and m126 < 0) or (sma and b["c"] < sma)):
                hit = "momdeath"
            if hit is None and cfg.get("band") and rebal:
                r = rank_idx.get(tk)
                if r is None or r >= cfg["band"]*kk:     # cayó de la banda (o ya no está above_sma)
                    hit = "rank"
            if hit: to_exit.append((tk, i, hit))
        for tk, i, kind in to_exit:
            dd = data[tk]; xo = dd["bars"][i+1]["o"]; p = positions.pop(tk)
            cash += p["shares"]*xo - FEE; fees += FEE
            trades.append({"tk": tk, "entry_date": p["edate"], "exit_date": dd["bars"][i+1]["t"],
                           "pnl_usd": p["shares"]*(xo-p["entry"]) - 2*FEE,
                           "pnl_pct": (xo/p["entry"]-1)*100,
                           "held": i - p["eidx"], "kind": kind})

        # régimen macro (cfg["macro"]=False lo desactiva → fuerza operar también en el bear)
        regime_ok = True
        if cfg.get("macro", True):
            ii = idx_obj["idx"].get(d)
            if ii is not None and idx_obj["sma"][ii] is not None:
                regime_ok = idx_obj["bars"][ii]["c"] > idx_obj["sma"][ii]

        # ---------- 2) RELLENO de slots libres ----------
        free = kk - len(positions)
        if free > 0 and regime_ok:
            # gate "fresh": breakout que NO es un líder de momentum 6m (fuera del top-N por mom126).
            # Operacionaliza el panel _fresh_breakouts (caso SNOW: rompe pero 6m flojo).
            strong_set = set()
            if cfg["gate"] == "fresh":
                pool = []
                for tk in data:
                    dd = data[tk]; i = dd["idx"].get(d)
                    if i is None or i < WARM: continue
                    c = dd["bars"][i]["c"]; sma = dd["pre"]["sma"][i]; m126 = dd["pre"]["mom"][126][i]
                    if sma and c > sma and m126 is not None: pool.append((m126, tk))
                pool.sort(reverse=True)
                strong_set = {tk for _, tk in pool[:cfg.get("fresh_topn", 10)]}
            cands = []
            for tk in data:
                if tk in positions: continue
                dd = data[tk]; i = dd["idx"].get(d)
                if i is None or i < WARM or i+1 >= len(dd["bars"]): continue
                c = dd["bars"][i]["c"]; sma = dd["pre"]["sma"][i]; bkh = dd["pre"]["bkh"][i]
                if not (sma and c > sma): continue
                if cfg["gate"] in ("breakout", "fresh") and not (bkh and c >= bkh): continue
                if cfg["gate"] == "fresh" and tk in strong_set: continue
                s = sig_of(tk, i)
                if s is not None: cands.append((s, tk, i))
            cands.sort(reverse=True)
            equity_now = cash + sum(positions[t]["shares"]*_close_on(data[t], d) for t in positions)
            for s, tk, i in cands[:free]:
                dd = data[tk]; eo = dd["bars"][i+1]["o"]
                base = equity_now/kk
                a = dd["pre"]["atr"][i]; atr_pct = (a/dd["bars"][i]["c"]) if a else VOL_TARGET
                base *= min(1.0, VOL_TARGET/atr_pct) if atr_pct > 0 else 1.0
                target = min(cash, base)
                if target <= FEE+1: continue
                sh = (target-FEE)/eo
                cash -= sh*eo + FEE; fees += FEE
                positions[tk] = {"shares": sh, "entry": eo, "hh": dd["bars"][i+1]["h"],
                                 "edate": dd["bars"][i+1]["t"], "eidx": i+1}

        eq = cash + sum(positions[tk]["shares"]*_close_on(data[tk], d) for tk in positions)
        daily_eq.append((d, eq))
        invested_frac.append(len(positions)/kk)

    return {"deq": daily_eq, "trades": trades, "fees": fees,
            "invested": statistics.mean(invested_frac) if invested_frac else 0}


# ── métricas (autocontenidas, rebasadas al primer día del tramo) ──────────────
def perf(deq):
    by_month = {}
    for d, eq in deq: by_month[d[:7]] = eq
    months = sorted(by_month)
    base = deq[0][1]; rets = []; prev = base
    for m in months:
        rets.append(by_month[m]/prev-1); prev = by_month[m]
    peak = -1e9; mdd = 0.0
    for _, eq in deq: peak = max(peak, eq); mdd = min(mdd, eq/peak-1)
    yrs = len(deq)/252; final = deq[-1][1]
    cagr = (final/base)**(1/yrs)-1 if yrs > 0 else 0
    mean = statistics.mean(rets) if rets else 0
    std = statistics.pstdev(rets) if len(rets) > 1 else 0
    return {"total": (final/base-1)*100, "cagr": cagr*100, "mdd": mdd*100,
            "worst_m": (min(rets)*100 if rets else 0),
            "sharpe": (mean/std*(12**0.5) if std > 0 else 0)}


def yearly(deq):
    first, last = {}, {}
    for d, eq in deq:
        y = d[:4]
        if y not in first: first[y] = eq
        last[y] = eq
    return {y: (last[y]/first[y]-1)*100 for y in sorted(last)}


def summarize(res):
    s = perf(res["deq"]); tr = res["trades"]
    wins = [t for t in tr if t["pnl_pct"] > 0]
    s["trades"] = len(tr)
    s["wr"] = (len(wins)/len(tr)*100) if tr else 0
    s["fees"] = res["fees"]
    s["invested"] = res["invested"]*100
    s["avg_hold"] = statistics.mean([t["held"] for t in tr]) if tr else 0
    return s


# ── variantes (puntos coherentes del espacio de diseño, no celdas al azar) ────
def base_cfg(**kw):
    cfg = {"rank": "mom126", "gate": "breakout", "exits": {"chandelier"},
           "mult": MULT, "hold": 9999, "cadence": None, "band": None, "macro": True}
    cfg.update(kw); return cfg

VARIANTS = [
    # nombre,                              config
    ("BASE (live: chand 4xATR)",           base_cfg()),
    # --- EJE 2: regla de venta (le da a Marea un 'cuándo vender' definido) ---
    ("Rank-band mensual (2K)",             base_cfg(exits=set(), cadence="monthly", band=2.0)),
    ("Rank-band mensual estricto (1K)",    base_cfg(exits=set(), cadence="monthly", band=1.0)),
    ("Rank-band semanal (2K)",             base_cfg(exits=set(), cadence="weekly", band=2.0)),
    ("Chand + rank-band mensual (2K)",     base_cfg(exits={"chandelier"}, cadence="monthly", band=2.0)),
    ("Muerte de momentum",                 base_cfg(exits={"momdeath"})),
    ("Horizonte fijo 21d",                 base_cfg(exits={"time"}, hold=21)),
    ("Horizonte fijo 42d",                 base_cfg(exits={"time"}, hold=42)),
    ("Horizonte fijo 63d",                 base_cfg(exits={"time"}, hold=63)),
    ("Chand + horizonte 63d",              base_cfg(exits={"chandelier", "time"}, hold=63)),
    # --- EJE 1: señal de ranking (manteniendo chandelier base) ---
    ("Señal mom63 (más reactiva)",         base_cfg(rank="mom63")),
    ("Señal mom21 (corto plazo)",          base_cfg(rank="mom21")),
    ("Señal mom/ATR% (riesgo-aj)",         base_cfg(rank="mom_atr")),
    ("Señal aceleración",                  base_cfg(rank="accel")),
    # --- EJE 4: relleno sin gate de breakout (momentum puro top-K) ---
    ("Top-K mom puro (sin breakout)",      base_cfg(gate="none")),
    ("Top-K mom puro + rank mensual",      base_cfg(gate="none", exits=set(), cadence="monthly", band=2.0)),
]

# Vecindario de parámetros + combinación de las ideas que batieron a BASE (anti-overfit:
# el ganador y sus vecinas deben mantenerse; una celda solitaria que gana = sobreajuste).
NEIGHBORHOOD = [
    ("BASE (live: chand 4xATR)",           base_cfg()),
    # -- sin gate de breakout: barrido de señal (la ganadora limpia: Sharpe↑ y MaxDD↓) --
    ("NoGate mom126 (chand)",              base_cfg(gate="none")),
    ("NoGate mom63  (chand)",              base_cfg(gate="none", rank="mom63")),
    ("NoGate mom45  (chand)",              base_cfg(gate="none", rank="mom45")),
    ("NoGate mom30  (chand)",              base_cfg(gate="none", rank="mom30")),
    ("NoGate mom21  (chand)",              base_cfg(gate="none", rank="mom21")),
    ("NoGate mom15  (chand)",              base_cfg(gate="none", rank="mom15")),
    # -- señal corta CON gate de breakout (vecindario de mom21) --
    ("Gate mom30 (chand)",                 base_cfg(rank="mom30")),
    ("Gate mom21 (chand)",                 base_cfg(rank="mom21")),
    ("Gate mom15 (chand)",                 base_cfg(rank="mom15")),
    # -- horizonte fijo: vecindario alrededor de 63d --
    ("Horizonte 50d",                      base_cfg(exits={"time"}, hold=50)),
    ("Horizonte 63d",                      base_cfg(exits={"time"}, hold=63)),
    ("Horizonte 75d",                      base_cfg(exits={"time"}, hold=75)),
    ("Horizonte 84d",                      base_cfg(exits={"time"}, hold=84)),
    # -- combos de las mejores ideas --
    ("NoGate mom21 (chand)  ★combo",       base_cfg(gate="none", rank="mom21")),
    ("NoGate + horizonte 63d",             base_cfg(gate="none", exits={"time"}, hold=63)),
    ("NoGate mom21 + horiz 63d",           base_cfg(gate="none", rank="mom21", exits={"time"}, hold=63)),
    ("Chand + horiz 63d (gate)",           base_cfg(exits={"chandelier", "time"}, hold=63)),
    # -- combos PRUDENTES: NoGate + horizonte PERO con chandelier de seguridad (stop-loss) --
    ("NoGate + chand + horiz 63d",         base_cfg(gate="none", exits={"chandelier", "time"}, hold=63)),
    ("NoGate + chand + horiz 75d",         base_cfg(gate="none", exits={"chandelier", "time"}, hold=75)),
    ("NoGate + chand + horiz 84d",         base_cfg(gate="none", exits={"chandelier", "time"}, hold=84)),
    # -- banda: barrido (caracteriza el trade-off retorno/DD; falla el piso de MaxDD) --
    ("Rank-band mensual 1.5K",             base_cfg(exits=set(), cadence="monthly", band=1.5)),
    ("Rank-band mensual 2.0K",             base_cfg(exits=set(), cadence="monthly", band=2.0)),
    ("Rank-band mensual 2.5K",             base_cfg(exits=set(), cadence="monthly", band=2.5)),
    ("Chand+band 2K (DD-controlada)",      base_cfg(exits={"chandelier"}, cadence="monthly", band=2.0)),
]


def load_data(universe, start=START, cache_path=CACHE):
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    n0 = len(cache)
    raw = {}
    for tk in universe + ["QQQ"]:
        b = fetch_daily_cached(tk, cache, start)
        if len(b) > WARM + 5: raw[tk] = b
    if len(cache) != n0:
        json.dump(cache, open(cache_path, "w"))
    qqq = raw.pop("QQQ")
    data = {tk: {"bars": bars, "idx": {b["t"]: i for i, b in enumerate(bars)},
                 "pre": precompute(bars)} for tk, bars in raw.items()}
    idx_obj = {"bars": qqq, "idx": {b["t"]: i for i, b in enumerate(qqq)},
               "sma": precompute(qqq)["sma"]}
    return data, idx_obj, qqq


def year_perf(deq, yr):
    """Perf de un año concreto (rebasado al primer día del año). None si no hay datos."""
    sl = [(d, e) for d, e in deq if d[:4] == yr]
    return perf(sl) if len(sl) > 5 else None


def stress(uni, uni_name):
    """
    STRESS del cabo '2022 nunca operó': datos desde 2020 (2020-21 = warm-up, 2022 = bear REAL
    y operable). Compara cada finalista con FILTRO MACRO ON vs OFF (forzar operar en el bear)
    para ver el MaxDD real CON stop (chandelier) vs SIN stop (horizonte solo). Luego barre K y
    el multiplicador del chandelier bajo NoGate. Si la variante recomendada sobrevive el bear
    con stop, el cambio es seguro fuera del régimen alcista.
    """
    START_S = "2020-01-01T00:00:00Z"
    CACHE_S = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selection_cache_2020.json")
    print(f"\n{'#'*104}")
    print(f"  STRESS — bear 2022 operable | universo {uni_name} ({len(uni)}) | datos desde 2020 | K={K}")
    print(f"{'#'*104}")
    data, idx_obj, qqq = load_data(uni, START_S, CACHE_S)
    cal = build_calendar(data)
    qc = {b["t"]: b["c"] for b in qqq}
    print(f"  {len(data)}/{len(uni)} con historia | {len(cal)} días | 2020-21 calientan, 2022 = bear real\n")

    finalists = [
        ("BASE (gate+chand)",            base_cfg()),
        ("BASE + horiz84 (gate+ch+t)",  base_cfg(exits={"chandelier", "time"}, hold=84)),
        ("BASE + horiz126 (gate+ch+t)", base_cfg(exits={"chandelier", "time"}, hold=126)),
        ("NoGate + chand",              base_cfg(gate="none")),
        ("NoGate + chand + horiz84",    base_cfg(gate="none", exits={"chandelier", "time"}, hold=84)),
        ("NoGate + horiz63 (SIN stop)", base_cfg(gate="none", exits={"time"}, hold=63)),
        ("Rank-band mensual 2K",        base_cfg(exits=set(), cadence="monthly", band=2.0)),
    ]
    # QQQ 2022 como referencia del bear
    q22 = [(b["t"], b["c"]) for b in qqq if b["t"][:4] == "2022"]
    q22dd = perf([(d, c) for d, c in q22])["mdd"] if len(q22) > 5 else 0
    q22ret = (q22[-1][1]/q22[0][1]-1)*100 if len(q22) > 5 else 0
    print(f"  Referencia bear: QQQ 2022 ret {q22ret:+.0f}% | MaxDD {q22dd:+.1f}%\n")

    print(f"  {'Variante':<30} {'macro':<5} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'2022 ret':>9} {'2022 DD':>8} {'Trades':>6}")
    print(f"  {'-'*90}")
    for name, cfg0 in finalists:
        for macro in (True, False):
            cfg = dict(cfg0); cfg["macro"] = macro
            res = run(data, cal, cfg, idx_obj); s = summarize(res)
            y22 = year_perf(res["deq"], "2022")
            r22 = f"{y22['total']:>+7.0f}%" if y22 else "   n/a "
            d22 = f"{y22['mdd']:>+6.1f}%" if y22 else "  n/a "
            print(f"  {name:<30} {'ON' if macro else 'OFF':<5} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% "
                  f"{r22:>9} {d22:>8} {s['trades']:>6}")
        print()

    # ── barrido de K (concentración) sobre la variante recomendada, macro ON ──
    print(f"  BARRIDO DE K (NoGate + chand + horiz84, macro ON) — ¿cuántas posiciones?")
    print(f"  {'K':>3} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'PeorMes':>7} {'WR%':>4} {'Trades':>6}")
    print(f"  {'-'*48}")
    for kk in (3, 5, 8, 10):
        cfg = base_cfg(gate="none", exits={"chandelier", "time"}, hold=84, K=kk)
        s = summarize(run(data, cal, cfg, idx_obj))
        print(f"  {kk:>3} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {s['worst_m']:>+6.1f}% "
              f"{s['wr']:>3.0f}% {s['trades']:>6}")

    # ── barrido del multiplicador del chandelier bajo NoGate, macro ON ──
    print(f"\n  BARRIDO CHANDELIER (NoGate + chand, sin horizonte, macro ON) — ¿qué tan ancho el stop?")
    print(f"  {'mult':>5} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'PeorMes':>7} {'WR%':>4} {'Trades':>6}")
    print(f"  {'-'*50}")
    for m in (2.5, 3.0, 4.0, 5.0, 6.0):
        cfg = base_cfg(gate="none", mult=m)
        s = summarize(run(data, cal, cfg, idx_obj))
        print(f"  {m:>5.1f} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {s['worst_m']:>+6.1f}% "
              f"{s['wr']:>3.0f}% {s['trades']:>6}")

    print(f"\n{'='*104}")
    print("  Lectura: con macro OFF se ve el bear 2022 REAL. La variante SIN stop (horiz solo) debería")
    print("  sufrir más DD en 2022 que las que llevan chandelier. Si NoGate+chand aguanta el bear con")
    print("  DD parecido a BASE, el cambio es seguro. El filtro macro igual protege en vivo (esto es el")
    print("  peor caso de stress). K bajo = más concentrado/volátil; K alto = más diluido.")
    print(f"{'='*104}\n")


def ksweep(uni, uni_name):
    """
    Lever K (nº de posiciones) sobre la BASE Marea REAL (gate breakout + chandelier 4×ATR),
    bear-inclusive (datos desde 2020). ¿Subir de 5 a 8-10 mejora Sharpe/MaxDD por diversificación,
    o el chandelier ya basta? Tensión operativa: Oscar maneja ~5 manuales en eToro.
    """
    START_S = "2020-01-01T00:00:00Z"
    CACHE_S = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selection_cache_2020.json")
    print(f"\n{'#'*96}")
    print(f"  LEVER K — BASE Marea (gate breakout + chandelier 4×ATR) | universo {uni_name} | datos desde 2020")
    print(f"{'#'*96}")
    data, idx_obj, qqq = load_data(uni, START_S, CACHE_S)
    cal = build_calendar(data)
    print(f"  {len(data)}/{len(uni)} con historia | {len(cal)} días (2022 = bear real operable)\n")
    print(f"  {'K':>3} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'PeorMes':>7} {'2022':>6} {'WR%':>4} "
          f"{'Trades':>6} {'%Inv':>5} {'Fees$':>6}")
    print(f"  {'-'*70}")
    for kk in (3, 4, 5, 6, 8, 10, 12):
        res = run(data, cal, base_cfg(K=kk), idx_obj); s = summarize(res)
        y22 = year_perf(res["deq"], "2022")
        r22 = f"{y22['total']:>+4.0f}%" if y22 else " n/a "
        print(f"  {kk:>3} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% {s['worst_m']:>+6.1f}% "
              f"{r22:>6} {s['wr']:>3.0f}% {s['trades']:>6} {s['invested']:>4.0f}% {s['fees']:>6.0f}")
    print(f"\n  Lectura: más K = menos concentración (mejor Sharpe/DD) pero más nombres que manejar y más")
    print(f"  fees. Buscar el punto donde la mejora riesgo-ajustada se aplana vs el costo operativo (~5).\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", choices=["broad", "core"], default="broad",
                    help="broad = top-80 liquidez (honesto, default); core = 30 tickers del OOS")
    ap.add_argument("--mode", choices=["grid", "neighborhood", "stress", "ksweep"], default="grid",
                    help="grid; neighborhood; stress = bear 2022 + barridos; ksweep = lever K sobre BASE")
    args = ap.parse_args()

    if args.universe == "broad":
        uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]
    else:
        from backtest_marea_oos import UNIVERSE as uni

    if args.mode == "stress":
        return stress(uni, args.universe)
    if args.mode == "ksweep":
        return ksweep(uni, args.universe)
    variants = VARIANTS if args.mode == "grid" else NEIGHBORHOOD

    print(f"\n{'#'*112}")
    print(f"  ESTUDIO A — SELECCIÓN/ROTACIÓN | universo {args.universe} ({len(uni)}) | K={K} brk={BREAKOUT} "
          f"chand={MULT} +macro +vol | fee ${FEE}/lado | OBJETIVO: Sharpe↑/MaxDD robusto")
    print(f"{'#'*112}")
    data, idx_obj, qqq = load_data(uni)
    cal = build_calendar(data)
    print(f"  Con historia suficiente desde 2022: {len(data)}/{len(uni)} | {len(cal)} días de calendario\n")

    print(f"  {'Variante':<34} {'Total%':>7} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'PeorMes':>7} "
          f"{'WR%':>4} {'Trades':>6} {'Hold':>5} {'%Inv':>5} {'Fees$':>6}")
    print(f"  {'-'*110}")
    results = {}
    for name, cfg in variants:
        res = run(data, cal, cfg, idx_obj)
        s = summarize(res); results[name] = (s, res)
        print(f"  {name:<34} {s['total']:>+6.0f}% {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% "
              f"{s['worst_m']:>+6.1f}% {s['wr']:>3.0f}% {s['trades']:>6} {s['avg_hold']:>5.0f} "
              f"{s['invested']:>4.0f}% {s['fees']:>6.0f}")

    # benchmark QQQ
    qdeq = [(b["t"], CAP*b["c"]/qqq[0]["c"]) for b in qqq if b["t"] >= cal[0]]
    sb = perf(qdeq)
    print(f"  {'-'*110}")
    print(f"  {'QQQ buy&hold':<34} {sb['total']:>+6.0f}% {sb['cagr']:>+5.1f}% {sb['sharpe']:>6.2f} "
          f"{sb['mdd']:>+6.1f}% {sb['worst_m']:>+6.1f}%")

    # DESGLOSE POR AÑO (robustez)
    print(f"\n  DESGLOSE POR AÑO (retorno %):")
    base_deq = results["BASE (live: chand 4xATR)"][1]["deq"]
    years = sorted(yearly(base_deq).keys())
    print(f"  {'Variante':<34} " + " ".join(f"{y:>7}" for y in years))
    print(f"  {'-'*(34+8*len(years))}")
    for name, _ in variants:
        yr = yearly(results[name][1]["deq"])
        print(f"  {name:<34} " + " ".join(f"{yr.get(y, 0):>+6.0f}%" for y in years))
    qy = yearly(qdeq)
    print(f"  {'QQQ':<34} " + " ".join(f"{qy.get(y, 0):>+6.0f}%" for y in years))

    # SPLIT-HALF OOS (2022-23 construir vs 2024-26 confirmar)
    print(f"\n  SPLIT-HALF OOS (Sharpe | MaxDD por mitad):")
    print(f"  {'Variante':<34} {'22-23 Sh':>9} {'22-23 DD':>9} {'24-26 Sh':>9} {'24-26 DD':>9}")
    print(f"  {'-'*78}")
    for name, _ in variants:
        deq = results[name][1]["deq"]
        h1 = [(d, e) for d, e in deq if d < "2024-01-01"]
        h2 = [(d, e) for d, e in deq if d >= "2024-01-01"]
        if not h1 or not h2: continue
        p1, p2 = perf(h1), perf(h2)
        print(f"  {name:<34} {p1['sharpe']:>9.2f} {p1['mdd']:>+8.1f}% {p2['sharpe']:>9.2f} {p2['mdd']:>+8.1f}%")

    print(f"\n{'='*112}")
    print("  Lectura: el GANADOR debe mejorar Sharpe sin empeorar MaxDD vs BASE, ganar (o empatar) en")
    print("  AMBAS mitades, y ser razonable año a año. Más trades/fees con peor Sharpe = churn (Watcher).")
    print("  Si NINGUNA variante bate a BASE en riesgo-ajustado robusto -> la pregunta de rotación queda")
    print("  CERRADA: el chandelier sin horizonte es lo correcto, y el 'qué vender' es 'cuando rompe el stop'.")
    print(f"{'='*112}\n")


if __name__ == "__main__":
    main()
