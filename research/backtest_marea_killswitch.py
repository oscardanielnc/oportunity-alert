import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

#!/usr/bin/env python3
"""
PAQUETE B — SALIDAS DE MAREA: RATCHET / KILL-SWITCH RÁPIDO / DERISK-RÁPIDO / SECTOR CAP
=======================================================================================
Pedido de Oscar (2026-06-10): "cuando el mercado caiga, salir rápido perdiendo lo mínimo,
no comerme una caída >10%, y recomprar más barato / reentrar con Marea tras la recuperación".

ANTECEDENTE: el derisk por régimen SMA200 ya se midió y RECHAZÓ (Estudio C-Fase2: lento,
+0.02 Sharpe, ventas ambiguas). Lo NO probado es un KILL-SWITCH RÁPIDO por velocidad (QQQ
ret 5d) y amplitud (breadth EMA20 del universo), que reacciona en días, no en semanas.

BASE replicada fiel al motor live: top-80 dollar-vol, ranking mom126, K=5 (también K=8),
gate breakout (cierre = máx 50d), macro = histéresis ±3% QQQ vs SMA200 (gate, adoptada del
Estudio C), chandelier hh−4×ATR22 sobre CIERRE diario, venta al OPEN siguiente, vol-sizing
InvVol VT=0.03, fee $1/lado. Datos desde 2020 (2020 hasta ~oct = warm-up de indicadores →
el covid crash NO es operable; 2022 = bear REAL operable) hasta hoy.

VARIANTES:
  1. RATCHET   : stop_t = max(stop_{t-1}, hh − 4×ATR) — el stop nunca baja aunque el ATR
                 se expanda en pánico (en la BASE el stop PUEDE bajar si el ATR explota).
  2. TIGHTEN   : kill-switch K_ON = (QQQ ret5d < −4%) OR (breadth EMA20 < 30%). Mientras
                 K_ON: chandelier 4→2.5× y 4→3× (dos sub-variantes) + sin compras nuevas.
                 K_OFF = QQQ ret5d > 0 AND breadth > 40% (histéresis).
  3. DERISK-RÁPIDO (lo que Oscar pide literal): con K_ON vender TODO al open siguiente;
                 reentrada natural del motor cuando K_OFF. Sub-variante: vender solo la
                 MITAD (las posiciones más cerca de su stop).
  4. SECTOR CAP: máx 3 posiciones por sector (mapa a mano documentado abajo).
  5. COMBO     : mejor de 1-3 + sector cap.

SENSIBILIDAD: QQQ ret5d ∈ {−3,−4,−5%} × breadth_lo ∈ {25,30,35%} (K_OFF = breadth_lo+10pp).
Meseta = robusto; pico solitario = frágil → descartar.

MÉTRICA DE REGRET (juez del kill-switch): en cada activación, forward +20d del libro vendido
(ponderado por valor). Negativo = acertó (protegió); positivo = whipsaw (vendió y rebotó).
También: activaciones en 2024-2025 (años buenos) = falsas alarmas.

REPRODUCIR:  python research/backtest_marea_killswitch.py
Cache propio: research/_killswitch_cache_2020.json (datos 2020-01-01 → hoy).

═══════════════════════════════════════════════════════════════════════════════════════════════
RESULTADO (2026-06-10) — corrida con datos 2020-01-02 → 2026-06-10 (79/80 tickers, 1618 días):

  BASE K=5: CAGR +38.8% Sharpe 1.54 MaxDD −20.7%  |  BASE K=8: +45.4% / 1.53 / −15.8%

  1. RATCHET — DESCARTAR (el peor del paquete): Sharpe 1.18, MaxDD −27.2% (PEOR que BASE),
     2021 +24% vs +51%, split 20-22 Sharpe 0.48. La "mejora" intuitiva es dañina: cuando el ATR
     se expande en pánico, el stop congelado vende el shakeout, recompra arriba y lo vuelven a
     sacar (churn 135 vs 94 trades). Que el chandelier "afloje" con el ATR es FEATURE, no bug.
  2. TIGHTEN (2.5/3×ATR en K_ON) — DESCARTAR: Sharpe 1.32-1.37 (vs 1.54), MaxDD igual/peor,
     regret 29-43% acertó, costo whipsaw +9/+11%. Apretar el stop en pánico = vender el piso.
  3. DERISK-RÁPIDO (vender todo en K_ON):
       - Con el spec default (−4%/30): MARGINAL-NEGATIVO (Sharpe 1.43, MaxDD −23.0 peor que BASE).
       - Con disparador RÁPIDO −3%/35 (off: ret5>0 & breadth>45): el ÚNICO ganador del paquete:
         CAGR +39.9 Sharpe 1.69 MaxDD −14.2 vol 21.2 | DD2022 −2.8% DD2025 −12.2% | gana en
         AMBAS mitades (1.22/−9.4 y 2.06/−14.2 vs 1.14/−20.7 y 1.86/−16.1). Región coherente
         (thr −2/−3, blo 35-40 → Sharpe 1.63-1.77), NO celda solitaria, PERO con gradiente claro:
         más lento que −3% pierde el edge (−4/30 ya es peor que BASE). El edge VIVE en la velocidad.
       - DERISK-HALF: peor que ALL (Sharpe 1.57, MaxDD −20.5) — vender la mitad no protege.
     COSTO REAL (honestidad): 9.3 activaciones/año, 29% del tiempo en cash, trades 94→229 (×2.4),
     y REGRET brutal: solo 21-27% de las activaciones aciertan; mediana fwd+20d +5.3% → 4 de cada
     5 veces Oscar recompra MÁS CARO, no más barato. Funciona como SEGURO asimétrico (whipsaw
     barato y acotado vs crash evitado grande), no como "recomprar abajo". En 2026 YTD costó
     retorno (+29% vs +56%) con DD igual. 2024-25: 16 falsas alarmas.
  4. SECTOR CAP 3 — DESCARTAR: no baja el DD (mismo −20.7), recorta CAGR (−2.3pp; 2026 +31% vs
     +56% — la concentración semi ES el retorno de Marea), y sobre el derisk no agrega (1.70≈1.69).
  5. COMBO derisk+CAP3 ≈ derisk solo. No suma.

  ALTERNATIVA PASIVA (cero comportamiento nuevo): K=8 ya da MaxDD −15.8% (≈ el −14.2 del derisk)
  con CAGR +45.4% y CERO señales nuevas. El derisk-rápido solo se justifica si Oscar tolera el
  whipsaw psicológico a cambio de DD2022 −2.8% y Sharpe 1.69.
═══════════════════════════════════════════════════════════════════════════════════════════════
"""
import os, sys, json, statistics
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_marea_selection import (
    load_data, perf, year_perf, yearly, summarize,
    K, MULT, WARM, VOL_TARGET, FEE, CAP, DATA_DIR, signal_val,
)
from backtest_marea_regime import regime_features, expo_series
from backtest_portfolio_momentum import build_calendar, _close_on

START_S = "2020-01-01T00:00:00Z"
CACHE_KS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_killswitch_cache_2020.json")

# ── mapa sector a mano (aprox. ETF: SMH / XLK-hw / IGV / cripto / XLF / spec / XLE / XLV / XLY / XLI) ──
SECTOR_MAP = {
    # SEMI: chips, memoria, ópticos, equipos (SMH)
    "NVDA": "SEMI", "AMD": "SEMI", "MU": "SEMI", "INTC": "SEMI", "AVGO": "SEMI", "MRVL": "SEMI",
    "QCOM": "SEMI", "TSM": "SEMI", "AMAT": "SEMI", "LRCX": "SEMI", "KLAC": "SEMI", "ASML": "SEMI",
    "TXN": "SEMI", "ADI": "SEMI", "SNDK": "SEMI", "WDC": "SEMI", "STX": "SEMI", "LITE": "SEMI",
    "COHR": "SEMI", "AAOI": "SEMI", "ALAB": "SEMI", "GLW": "SEMI", "APH": "SEMI",
    # AI_INFRA: servidores, redes, datacenter power, AI-cloud
    "SMCI": "AI_INFRA", "DELL": "AI_INFRA", "ANET": "AI_INFRA", "CSCO": "AI_INFRA",
    "VRT": "AI_INFRA", "NOK": "AI_INFRA", "AAPL": "AI_INFRA", "CRWV": "AI_INFRA",
    "NBIS": "AI_INFRA", "IREN": "AI_INFRA",
    # SOFT: software / internet / cloud (IGV)
    "MSFT": "SOFT", "GOOGL": "SOFT", "GOOG": "SOFT", "AMZN": "SOFT", "META": "SOFT",
    "PLTR": "SOFT", "NOW": "SOFT", "ORCL": "SOFT", "CRM": "SOFT", "INTU": "SOFT", "SNOW": "SOFT",
    "APP": "SOFT", "PANW": "SOFT", "CRWD": "SOFT", "UBER": "SOFT", "SPOT": "SOFT", "IBM": "SOFT",
    # CRYPTO-proxy
    "MSTR": "CRYPTO", "COIN": "CRYPTO", "HOOD": "CRYPTO", "CRCL": "CRYPTO",
    # FIN tradicional
    "V": "FIN", "MA": "FIN", "JPM": "FIN", "GS": "FIN", "BAC": "FIN",
    # SPEC: espacio / cuántica
    "RKLB": "SPEC", "ASTS": "SPEC", "IONQ": "SPEC", "RGTI": "SPEC", "CBRS": "SPEC",
    # ENERGY / power
    "XOM": "ENERGY", "CVX": "ENERGY", "NEE": "ENERGY", "BE": "ENERGY", "GEV": "ENERGY",
    # HEALTH
    "LLY": "HEALTH", "UNH": "HEALTH", "JNJ": "HEALTH",
    # CONS: consumo / retail / autos
    "WMT": "CONS", "COST": "CONS", "HD": "CONS", "F": "CONS", "TSLA": "CONS",
    # IND
    "CAT": "IND", "GE": "IND", "BA": "IND",
}


# ── features del kill-switch: QQQ ret5d + breadth EMA20 del universo ──────────
def qqq_ret5(qqq):
    qc = [b["c"] for b in qqq]
    return {b["t"]: (qc[i] / qc[i - 5] - 1) if i >= 5 else None for i, b in enumerate(qqq)}


def ema20_breadth(data, calendar):
    """% del universo con cierre > su propia EMA20, por fecha."""
    em = {}
    kf = 2.0 / 21.0
    for tk, dd in data.items():
        closes = [b["c"] for b in dd["bars"]]
        e = [None] * len(closes)
        prev = None
        for i, c in enumerate(closes):
            if i == 19:
                prev = sum(closes[:20]) / 20.0
            elif i > 19:
                prev = c * kf + prev * (1 - kf)
            if i >= 19:
                e[i] = prev
        em[tk] = e
    br = {}
    for d in calendar:
        ab = tot = 0
        for tk, dd in data.items():
            i = dd["idx"].get(d)
            if i is None or em[tk][i] is None:
                continue
            tot += 1
            if dd["bars"][i]["c"] > em[tk][i]:
                ab += 1
        br[d] = (ab / tot) if tot >= 20 else None
    return br


def build_ks(calendar, ret5, breadth, thr=0.04, blo=0.30, bhi=None):
    """Máquina de estados del kill-switch. K_ON: ret5<−thr OR breadth<blo.
    K_OFF: ret5>0 AND breadth>bhi (histéresis). Devuelve serie por fecha + activaciones."""
    if bhi is None:
        bhi = blo + 0.10
    on = False
    series, acts = {}, []
    for d in calendar:
        r, b = ret5.get(d), breadth.get(d)
        if not on:
            if (r is not None and r < -thr) or (b is not None and b < blo):
                on = True
                acts.append(d)
        else:
            if (r is not None and r > 0) and (b is not None and b > bhi):
                on = False
        series[d] = on
    return series, acts


# ── motor: BASE (hyst±3% gate + chand 4×ATR + vol-sizing + open+1) extendido ──
def run_ks(data, calendar, cfg, expo, ks=None):
    """
    cfg:
      K          : nº de slots (5 base, 8 alternativa)
      mult       : chandelier base (4.0)
      ratchet    : True -> el stop nunca baja (max acumulado)
      ks_mode    : None | "tighten" | "derisk_all" | "derisk_half"
      ks_mult    : multiplicador chandelier mientras K_ON (tighten)
      sector_cap : None | int (máx posiciones por sector)
    Mientras K_ON (cualquier ks_mode): SIN compras nuevas.
    """
    cash = CAP
    kk = cfg.get("K", K)
    mult = cfg.get("mult", MULT)
    ratchet = cfg.get("ratchet", False)
    ks_mode = cfg.get("ks_mode")
    ks_mult = cfg.get("ks_mult", 3.0)
    scap = cfg.get("sector_cap")
    positions = {}
    daily_eq, trades = [], []
    fees = 0.0
    invested_frac = []
    prev_on = False
    cur_ep = None

    for d in calendar:
        e = expo.get(d, 1.0)
        on = bool(ks and ks.get(d, False))
        activated = on and not prev_on
        prev_on = on
        if activated:
            cur_ep = d
        if not on:
            cur_ep = None
        cap_k = max(0, round(e * kk))
        eff_mult = ks_mult if (on and ks_mode == "tighten") else mult

        # ---------- 1) SALIDAS (chandelier / ratchet / tighten) ----------
        to_exit = {}
        for tk, p in positions.items():
            dd = data[tk]; i = dd["idx"].get(d)
            if i is None or i + 1 >= len(dd["bars"]):
                continue
            b = dd["bars"][i]; p["hh"] = max(p["hh"], b["h"])
            p["_i"] = i; p["_close"] = b["c"]
            a = dd["pre"]["atr"][i]
            if a is None:
                continue
            lvl = p["hh"] - eff_mult * a
            if ratchet:
                p["stop"] = max(p.get("stop", lvl), lvl)
                lvl = p["stop"]
            p["_lvl"] = lvl
            if b["c"] < lvl:
                to_exit[tk] = (i, "ks_stop" if on else "chand")

        # ---------- 1b) DERISK-RÁPIDO: vender el libro al activarse K_ON ----------
        if on and ks_mode == "derisk_all":
            for tk, p in positions.items():
                if tk not in to_exit and "_i" in p:
                    to_exit[tk] = (p["_i"], "ks")
        elif activated and ks_mode == "derisk_half":
            held = [tk for tk, p in positions.items() if tk not in to_exit and "_i" in p]
            # las más cerca de su stop primero (distancia close→stop, relativa)
            def dist(tk):
                p = positions[tk]
                return ((p["_close"] - p["_lvl"]) / p["_close"]) if "_lvl" in p else 1e9
            held.sort(key=dist)
            for tk in held[:(len(held) + 1) // 2]:
                to_exit[tk] = (positions[tk]["_i"], "ks")

        for tk, (i, kind) in to_exit.items():
            dd = data[tk]; xo = dd["bars"][i + 1]["o"]; p = positions.pop(tk)
            cash += p["shares"] * xo - FEE; fees += FEE
            trades.append({"tk": tk, "pnl_pct": (xo / p["entry"] - 1) * 100,
                           "held": i - p["eidx"], "kind": kind, "ep": cur_ep,
                           "exit_date": dd["bars"][i + 1]["t"], "exit_idx": i + 1,
                           "exit_px": xo, "val": p["shares"] * xo})

        # ---------- 2) RELLENO de slots (gate breakout; bloqueado si K_ON) ----------
        free = cap_k - len(positions)
        if free > 0 and not (on and ks_mode):
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
                if not (bkh and c >= bkh):
                    continue
                s = signal_val(dd["pre"], i, "mom126")
                if s is not None:
                    cands.append((s, tk, i))
            cands.sort(reverse=True)
            equity_now = cash + sum(positions[t]["shares"] * _close_on(data[t], d) for t in positions)
            sec_count = {}
            if scap:
                for t in positions:
                    sc = SECTOR_MAP.get(t, "OTHER")
                    sec_count[sc] = sec_count.get(sc, 0) + 1
            filled = 0
            for s, tk, i in cands:
                if filled >= free:
                    break
                if scap:
                    sc = SECTOR_MAP.get(tk, "OTHER")
                    if sec_count.get(sc, 0) >= scap:
                        continue
                dd = data[tk]; eo = dd["bars"][i + 1]["o"]
                base = equity_now / kk
                a = dd["pre"]["atr"][i]; atr_pct = (a / dd["bars"][i]["c"]) if a else VOL_TARGET
                base *= min(1.0, VOL_TARGET / atr_pct) if atr_pct > 0 else 1.0
                target = min(cash, base)
                if target <= FEE + 1:
                    continue
                sh = (target - FEE) / eo
                cash -= sh * eo + FEE; fees += FEE
                positions[tk] = {"shares": sh, "entry": eo, "hh": dd["bars"][i + 1]["h"],
                                 "edate": dd["bars"][i + 1]["t"], "eidx": i + 1}
                if scap:
                    sc = SECTOR_MAP.get(tk, "OTHER")
                    sec_count[sc] = sec_count.get(sc, 0) + 1
                filled += 1

        eq = cash + sum(positions[tk]["shares"] * _close_on(data[tk], d) for tk in positions)
        daily_eq.append((d, eq))
        invested_frac.append(len(positions) / kk)

    return {"deq": daily_eq, "trades": trades, "fees": fees,
            "invested": statistics.mean(invested_frac) if invested_frac else 0}


# ── métricas extendidas ───────────────────────────────────────────────────────
def ann_vol(deq):
    by_month = {}
    for d, eq in deq:
        by_month[d[:7]] = eq
    months = sorted(by_month)
    rets, prev = [], deq[0][1]
    for m in months:
        rets.append(by_month[m] / prev - 1); prev = by_month[m]
    return (statistics.pstdev(rets) * (12 ** 0.5) * 100) if len(rets) > 1 else 0.0


def regret(res, data, H=20):
    """Por activación: forward +20d (ponderado por valor) del libro vendido por kill-switch."""
    eps = {}
    for t in res["trades"]:
        if t.get("ep") and t["kind"] in ("ks", "ks_stop"):
            eps.setdefault(t["ep"], []).append(t)
    rows = []
    for ep in sorted(eps):
        wsum = vsum = 0.0
        for t in eps[ep]:
            dd = data[t["tk"]]; xi = t["exit_idx"]
            if xi + H < len(dd["bars"]):
                f = dd["bars"][xi + H]["c"] / t["exit_px"] - 1
                wsum += f * t["val"]; vsum += t["val"]
        if vsum > 0:
            rows.append({"ep": ep, "fwd": wsum / vsum, "n": len(eps[ep])})
    hits = [r for r in rows if r["fwd"] < 0]
    whips = [r["fwd"] for r in rows if r["fwd"] >= 0]
    return {
        "n_eps": len(rows),
        "hit_pct": (len(hits) / len(rows) * 100) if rows else 0,
        "fwd_med": statistics.median([r["fwd"] for r in rows]) * 100 if rows else 0,
        "fwd_mean": statistics.mean([r["fwd"] for r in rows]) * 100 if rows else 0,
        "whip_cost": (statistics.mean(whips) * 100) if whips else 0,
        "n_whips": len(whips),
        "rows": rows,
    }


def row(name, res, deq_years=("2020", "2022", "2024", "2025", "2026")):
    s = summarize(res)
    yrs = len(res["deq"]) / 252
    cells = []
    for y in deq_years:
        yp = year_perf(res["deq"], y)
        cells.append(f"{yp['mdd']:>+6.1f}%" if yp else "   n/a")
    return (f"{name:<28} {s['cagr']:>+5.1f}% {s['sharpe']:>6.2f} {s['mdd']:>+6.1f}% "
            f"{ann_vol(res['deq']):>5.1f}% " + " ".join(cells) +
            f" {s['trades']/yrs:>5.1f} {s['trades']:>5}")


def split_half(res):
    deq = res["deq"]
    h1 = [(d, e) for d, e in deq if d < "2023-01-01"]
    h2 = [(d, e) for d, e in deq if d >= "2023-01-01"]
    p1, p2 = perf(h1), perf(h2)
    return p1, p2


def main():
    uni = json.load(open(os.path.join(DATA_DIR, "pilot_universe.json")))["tickers"]
    unmapped = [t for t in uni if t not in SECTOR_MAP]
    print(f"\n{'#'*118}")
    print(f"  PAQUETE B — SALIDAS: ratchet / kill-switch rápido / derisk-rápido / sector cap")
    print(f"  BASE = hyst±3% gate + chand {MULT}×ATR + vol-sizing | top-80 | fee ${FEE}/lado | 2020 → hoy")
    print(f"{'#'*118}")
    data, idx_obj, qqq = load_data(uni, START_S, CACHE_KS)
    cal = build_calendar(data)
    last = cal[-1]
    print(f"  {len(data)}/{len(uni)} con historia | {len(cal)} días | último día: {last} | "
          f"sin mapa de sector: {unmapped or 'ninguno'}")
    print(f"  NOTA: warm-up de indicadores consume 2020 hasta ~oct → el covid crash (mar-2020) NO es operable.")

    feats = regime_features(qqq, data, cal)
    expo = expo_series(cal, feats, "hysteresis", {"band": 0.03})
    ret5 = qqq_ret5(qqq)
    breadth = ema20_breadth(data, cal)

    # kill-switch default: −4% / 30→40
    ks_def, acts_def = build_ks(cal, ret5, breadth, 0.04, 0.30)
    on_days = sum(1 for d in cal if ks_def[d])
    by_year = {}
    for a in acts_def:
        by_year[a[:4]] = by_year.get(a[:4], 0) + 1
    print(f"\n  KILL-SWITCH default (QQQ ret5d<−4% OR breadth<30%; OFF: ret5>0 AND breadth>40%):")
    print(f"    {len(acts_def)} activaciones | {on_days} días K_ON ({on_days/len(cal)*100:.0f}% del tiempo)")
    print(f"    activaciones/año: " + "  ".join(f"{y}:{by_year.get(y,0)}" for y in sorted(set(d[:4] for d in cal))))
    print(f"    fechas: {', '.join(acts_def)}")

    # ── tabla principal ──
    variants = [
        ("BASE K=5 (live)",            dict(),                                                  None),
        ("BASE K=8",                   dict(K=8),                                               None),
        ("RATCHET (stop no baja)",     dict(ratchet=True),                                      None),
        ("RATCHET K=8",                dict(ratchet=True, K=8),                                 None),
        ("TIGHTEN 3.0xATR",            dict(ks_mode="tighten", ks_mult=3.0),                    ks_def),
        ("TIGHTEN 2.5xATR",            dict(ks_mode="tighten", ks_mult=2.5),                    ks_def),
        ("DERISK-ALL (vender todo)",   dict(ks_mode="derisk_all"),                              ks_def),
        ("DERISK-HALF (peor mitad)",   dict(ks_mode="derisk_half"),                             ks_def),
        ("SECTOR CAP 3 (solo)",        dict(sector_cap=3),                                      None),
        ("SECTOR CAP 3 K=8",           dict(sector_cap=3, K=8),                                 None),
        ("RATCHET + CAP3",             dict(ratchet=True, sector_cap=3),                        None),
        ("TIGHTEN 3.0 + CAP3",         dict(ks_mode="tighten", ks_mult=3.0, sector_cap=3),      ks_def),
        ("DERISK-ALL + CAP3",          dict(ks_mode="derisk_all", sector_cap=3),                ks_def),
        ("TIGHTEN 3.0 + RATCHET",      dict(ks_mode="tighten", ks_mult=3.0, ratchet=True),      ks_def),
        ("DERISK-ALL K=8",             dict(ks_mode="derisk_all", K=8),                         ks_def),
    ]
    print(f"\n  {'Variante':<28} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'Vol%':>6} "
          f"{'DD2020':>7} {'DD2022':>7} {'DD2024':>7} {'DD2025':>7} {'DD26YTD':>7} {'Tr/año':>6} {'Trades':>5}")
    print(f"  {'-'*116}")
    results = {}
    for name, cfg, ks in variants:
        res = run_ks(data, cal, cfg, expo, ks)
        results[name] = res
        print(f"  {row(name, res)}")
    qdeq = [(b["t"], CAP * b["c"] / qqq[0]["c"]) for b in qqq if b["t"] >= cal[0]]
    sb = perf(qdeq)
    print(f"  {'-'*116}")
    print(f"  {'QQQ buy&hold':<28} {sb['cagr']:>+5.1f}% {sb['sharpe']:>6.2f} {sb['mdd']:>+6.1f}%")

    # ── retorno por año (robustez) ──
    print(f"\n  RETORNO POR AÑO (%):")
    years = sorted(yearly(results['BASE K=5 (live)']["deq"]).keys())
    print(f"  {'Variante':<28} " + " ".join(f"{y:>7}" for y in years))
    print(f"  {'-'*(28+8*len(years))}")
    for name, _, _ in variants:
        yr = yearly(results[name]["deq"])
        print(f"  {name:<28} " + " ".join(f"{yr.get(y,0):>+6.0f}%" for y in years))

    # ── split-half OOS ──
    print(f"\n  SPLIT-HALF OOS (Sharpe | MaxDD):  2020-22 (bear adentro)  vs  2023-26 (bull)")
    print(f"  {'Variante':<28} {'20-22 Sh':>9} {'20-22 DD':>9} {'23-26 Sh':>9} {'23-26 DD':>9}")
    print(f"  {'-'*70}")
    for name, _, _ in variants:
        p1, p2 = split_half(results[name])
        print(f"  {name:<28} {p1['sharpe']:>9.2f} {p1['mdd']:>+8.1f}% {p2['sharpe']:>9.2f} {p2['mdd']:>+8.1f}%")

    # ── REGRET por activación (variantes con kill-switch) ──
    print(f"\n  REGRET — por activación del kill-switch: forward +20d del libro vendido")
    print(f"  (fwd<0 = acertó/protegió | fwd>0 = whipsaw: vendió y rebotó)")
    print(f"  {'Variante':<28} {'#act c/venta':>12} {'%acertó':>8} {'fwd med%':>9} {'fwd mean%':>10} "
          f"{'#whipsaw':>9} {'costo whip%':>11}")
    print(f"  {'-'*92}")
    for name in ["TIGHTEN 3.0xATR", "TIGHTEN 2.5xATR", "DERISK-ALL (vender todo)",
                 "DERISK-HALF (peor mitad)", "DERISK-ALL + CAP3"]:
        q = regret(results[name], data)
        print(f"  {name:<28} {q['n_eps']:>12} {q['hit_pct']:>7.0f}% {q['fwd_med']:>+8.1f}% "
              f"{q['fwd_mean']:>+9.1f}% {q['n_whips']:>9} {q['whip_cost']:>+10.1f}%")
    # detalle de episodios del derisk-all (lo que Oscar pide literal)
    q = regret(results["DERISK-ALL (vender todo)"], data)
    print(f"\n  DETALLE DERISK-ALL por episodio (fecha activación → fwd+20d del libro vendido):")
    for r in q["rows"]:
        tag = "ACERTÓ " if r["fwd"] < 0 else "WHIPSAW"
        print(f"    {r['ep']}  fwd+20d {r['fwd']*100:>+6.1f}%  ({r['n']} posiciones)  {tag}")

    # ── SENSIBILIDAD de umbrales (meseta vs pico) ──
    print(f"\n  SENSIBILIDAD de umbrales — TIGHTEN 3.0×ATR y DERISK-ALL")
    print(f"  (filas: QQQ ret5d thr | columnas: breadth_lo; K_OFF = ret5>0 AND breadth>lo+10pp)")
    for mode_name, cfg in [("TIGHTEN 3.0xATR", dict(ks_mode="tighten", ks_mult=3.0)),
                           ("DERISK-ALL", dict(ks_mode="derisk_all"))]:
        print(f"\n  {mode_name}:  celdas = Sharpe / MaxDD% / DD2022% / #act (act 24-25)")
        hdr = "thr/blo"
        print(f"  {hdr:>8} " + " ".join(f"{int(b*100):>26}%" for b in (0.25, 0.30, 0.35)))
        for thr in (0.03, 0.04, 0.05):
            cells = []
            for blo in (0.25, 0.30, 0.35):
                ks, acts = build_ks(cal, ret5, breadth, thr, blo)
                res = run_ks(data, cal, cfg, expo, ks)
                s = summarize(res)
                y22 = year_perf(res["deq"], "2022")
                d22 = y22["mdd"] if y22 else 0
                a2425 = sum(1 for a in acts if a[:4] in ("2024", "2025"))
                cells.append(f"{s['sharpe']:.2f}/{s['mdd']:+.1f}/{d22:+.1f}/{len(acts)}({a2425})")
            print(f"  {-int(thr*100):>7}% " + " ".join(f"{c:>27}" for c in cells))

    # ── FINALISTAS: las mejores celdas de la sensibilidad, medidas a fondo ──
    # La grilla mostró que el disparador RÁPIDO (thr=−3%) domina al default (−4%) en DERISK-ALL,
    # con el mejor punto en el BORDE (blo=35%). Hay que medir el vecindario extendido (¿meseta o
    # pico?, incluyendo blo=40% fuera de la grilla y thr=−2%) + métricas completas/regret/split.
    print(f"\n  {'='*116}")
    print(f"  FINALISTAS — kill-switch RÁPIDO (thr=−3%) a fondo")
    print(f"  {'='*116}")
    fin_ks = {}
    for thr, blo in [(0.02, 0.35), (0.03, 0.30), (0.03, 0.35), (0.03, 0.40), (0.035, 0.35), (0.04, 0.35)]:
        fin_ks[(thr, blo)] = build_ks(cal, ret5, breadth, thr, blo)
    finalists = [
        ("BASE K=5 (ref)",               dict(),                                  None),
        ("BASE K=8 (ref)",               dict(K=8),                               None),
        ("DERISK-ALL -2/35",             dict(ks_mode="derisk_all"),              fin_ks[(0.02, 0.35)][0]),
        ("DERISK-ALL -3/30",             dict(ks_mode="derisk_all"),              fin_ks[(0.03, 0.30)][0]),
        ("DERISK-ALL -3/35",             dict(ks_mode="derisk_all"),              fin_ks[(0.03, 0.35)][0]),
        ("DERISK-ALL -3/40",             dict(ks_mode="derisk_all"),              fin_ks[(0.03, 0.40)][0]),
        ("DERISK-ALL -3.5/35",           dict(ks_mode="derisk_all"),              fin_ks[(0.035, 0.35)][0]),
        ("DERISK-ALL -4/35",             dict(ks_mode="derisk_all"),              fin_ks[(0.04, 0.35)][0]),
        ("DERISK-ALL -3/35 K=8",         dict(ks_mode="derisk_all", K=8),         fin_ks[(0.03, 0.35)][0]),
        ("DERISK-ALL -3/35 + CAP3",      dict(ks_mode="derisk_all", sector_cap=3), fin_ks[(0.03, 0.35)][0]),
        ("DERISK-HALF -3/35",            dict(ks_mode="derisk_half"),             fin_ks[(0.03, 0.35)][0]),
        ("TIGHTEN 3.0 -3/35",            dict(ks_mode="tighten", ks_mult=3.0),    fin_ks[(0.03, 0.35)][0]),
    ]
    print(f"\n  {'Variante':<28} {'CAGR%':>6} {'Sharpe':>6} {'MaxDD%':>7} {'Vol%':>6} "
          f"{'DD2020':>7} {'DD2022':>7} {'DD2024':>7} {'DD2025':>7} {'DD26YTD':>7} {'Tr/año':>6} {'Trades':>5}")
    print(f"  {'-'*116}")
    fres = {}
    for name, cfg, ks in finalists:
        res = run_ks(data, cal, cfg, expo, ks)
        fres[name] = res
        print(f"  {row(name, res)}")

    print(f"\n  RETORNO POR AÑO (%):")
    print(f"  {'Variante':<28} " + " ".join(f"{y:>7}" for y in years))
    for name, _, _ in finalists:
        yr = yearly(fres[name]["deq"])
        print(f"  {name:<28} " + " ".join(f"{yr.get(y,0):>+6.0f}%" for y in years))

    print(f"\n  SPLIT-HALF OOS (Sharpe | MaxDD):")
    print(f"  {'Variante':<28} {'20-22 Sh':>9} {'20-22 DD':>9} {'23-26 Sh':>9} {'23-26 DD':>9}")
    for name, _, _ in finalists:
        p1, p2 = split_half(fres[name])
        print(f"  {name:<28} {p1['sharpe']:>9.2f} {p1['mdd']:>+8.1f}% {p2['sharpe']:>9.2f} {p2['mdd']:>+8.1f}%")

    print(f"\n  REGRET de los finalistas (fwd+20d del libro vendido por activación):")
    print(f"  {'Variante':<28} {'#act c/venta':>12} {'%acertó':>8} {'fwd med%':>9} {'fwd mean%':>10} "
          f"{'#whipsaw':>9} {'costo whip%':>11}")
    for name in ["DERISK-ALL -3/30", "DERISK-ALL -3/35", "DERISK-ALL -3/40",
                 "DERISK-ALL -3/35 K=8", "DERISK-HALF -3/35"]:
        q = regret(fres[name], data)
        print(f"  {name:<28} {q['n_eps']:>12} {q['hit_pct']:>7.0f}% {q['fwd_med']:>+8.1f}% "
              f"{q['fwd_mean']:>+9.1f}% {q['n_whips']:>9} {q['whip_cost']:>+10.1f}%")

    print(f"\n  CARGA OPERATIVA de los kill-switch finalistas (activaciones/año y % tiempo K_ON):")
    for (thr, blo), (ks, acts) in sorted(fin_ks.items()):
        ond = sum(1 for d in cal if ks[d])
        byy = {}
        for a in acts:
            byy[a[:4]] = byy.get(a[:4], 0) + 1
        print(f"    thr=-{thr*100:.1f}% blo={blo*100:.0f}%: {len(acts)} act ({len(acts)/(len(cal)/252):.1f}/año), "
              f"{ond/len(cal)*100:.0f}% tiempo K_ON | 24-25: {byy.get('2024',0)+byy.get('2025',0)} | "
              + " ".join(f"{y}:{byy.get(y,0)}" for y in sorted(set(d[:4] for d in cal))))

    print(f"\n{'='*118}")
    print("  Lectura: el objetivo de Oscar = no comerse >10% en crashes sin matar el CAGR de años buenos.")
    print("  Juez: (1) DD2022 y DD26YTD vs BASE, (2) CAGR/Sharpe no peor, (3) regret: %acertó>50 y costo")
    print("  whipsaw bajo, (4) pocas falsas alarmas 2024-25, (5) meseta en la sensibilidad (no pico).")
    print(f"{'='*118}\n")


if __name__ == "__main__":
    main()
