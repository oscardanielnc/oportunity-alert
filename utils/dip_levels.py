"""
Dip Levels — motor de soportes de entrada (código puro, sin IA).

Para cada ticker calcula, sobre barras diarias Alpaca:
  - SOPORTES CORTO PLAZO  (a dónde rebota hoy/esta semana): EMA20, Bollinger inf,
    mín. recientes, swing lows recientes (pivotes k=3)
  - SOPORTES ESTRUCTURALES (suelo serio): SMA200, EMA50, swing lows mayores (k=7),
    Fibonacci del último gran swing, nodo de alto volumen (POC)
  - CHIPS DE RIESGO (solo datos): tendencia, RSI14, drawdown, vs SPY, volumen, gap

Niveles cercanos se agrupan en zonas (clustering por ATR); la fuerza (1-5) premia
confluencia (varios tipos en la misma zona), toques, anclas mayores y volumen.

Solo se devuelven soportes POR DEBAJO del precio (los que ya rompió son resistencia,
se excluyen). El ranking lo arma dip_scanner por el % al soporte corto más cercano.

Regla del proyecto: código antes que IA. Aquí 0 tokens.
"""
import os
import math
import logging
import requests
from datetime import datetime, timezone, timedelta

# Reutilizamos los indicadores puros ya auditados en conviction_gates.
from utils.conviction_gates import _ema, _rsi, _atr, _bollinger

logger = logging.getLogger(__name__)

ALPACA_BASE = "https://data.alpaca.markets"
ALPACA_FEED_BARS = "sip"

# ── PARÁMETROS COMPARTIDOS (el indicador Pine usa EXACTAMENTE estos mismos valores) ──
# Cambiar aquí = cambiar también en research/pine/dip_levels.pine para mantener alineación.
P = {
    "EMA_SHORT": 20, "EMA_MID": 50, "SMA_LONG": 200, "BB_LEN": 20, "BB_MULT": 2.0, "ATR_LEN": 14,
    "SWING_RECENT_K": 3, "SWING_RECENT_WIN": 45, "SWING_MAJOR_K": 7,
    "RECENT_LOW": 10, "FIB_LOOKBACK": 180,
    "POC_LOOKBACK": 120, "POC_BINS": 40, "VALUE_AREA": 0.70,
    "CLUSTER_ATR_MULT": 0.5, "CLUSTER_PCT": 0.012,
    "REACTION_FWD": 10, "REACTION_STRONG_PCT": 3.0,   # rebote mediano fuerte → +fuerza
    "WEEKLY_EMA": 20, "WEEKLY_SMA": 50, "WEEKLY_LOW_WIN": 26,
    "ROUND_N": 2,
    "DRAWDOWN_WIN": 60, "VS_SPY_IDIO_PCT": -3.0, "VOL_AVG_WIN": 20,
}


# ── Fetch barras diarias (hasta ~300 para SMA200 + swing lows mayores) ──────────

def fetch_daily_bars(ticker: str, limit: int = 300) -> list:
    """Últimas `limit` barras diarias (asc). Lista vacía si falla."""
    try:
        key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            logger.warning("[Dips] Alpaca keys no configuradas")
            return []
        headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        }
        # Sin `start`, Alpaca devuelve solo el día actual. Pedimos ~1.6 días calendario
        # por barra para cubrir fines de semana/feriados y llegar a `limit` ruedas.
        start = (datetime.now(timezone.utc) - timedelta(days=int(limit * 1.6) + 30)).strftime("%Y-%m-%d")
        # SIP free: sin acceso a los últimos 15 min; acotamos el end a hace 20 min.
        end = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # sort=desc + limit → las `limit` barras MÁS RECIENTES (no las primeras del rango);
        # luego revertimos a ascendente para los cálculos.
        resp = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
            params={"timeframe": "1Day", "limit": limit, "feed": ALPACA_FEED_BARS,
                    "sort": "desc", "start": start, "end": end},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        bars = resp.json().get("bars") or []
        return list(reversed(bars))
    except Exception as e:
        logger.warning(f"[Dips] Error barras {ticker}: {e}")
        return []


def spy_return_5d() -> float:
    """Retorno % de SPY en las últimas 5 ruedas (para separar caída macro vs idiosincrática)."""
    bars = fetch_daily_bars("SPY", limit=10)
    if len(bars) < 6:
        return 0.0
    closes = [float(b["c"]) for b in bars]
    return round((closes[-1] / closes[-6] - 1) * 100, 2)


# ── Helpers de cálculo ──────────────────────────────────────────────────────────

def _beta(tk_returns: list, spy_returns: list, window: int = 60) -> float:
    """Beta sobre retornos diarios YA ALINEADOS por fecha (cov/var). Fallback 1.0."""
    m = min(len(tk_returns), len(spy_returns), window)
    if m < 20:
        return 1.0
    tr, sr = tk_returns[-m:], spy_returns[-m:]
    mean_s, mean_t = sum(sr) / m, sum(tr) / m
    cov = sum((sr[i] - mean_s) * (tr[i] - mean_t) for i in range(m)) / m
    var = sum((sr[i] - mean_s) ** 2 for i in range(m)) / m
    if var == 0:
        return 1.0
    beta = cov / var
    # acotar a un rango sensato para evitar betas absurdos por datos ruidosos
    return max(0.2, min(beta, 4.0))


def _sma(values: list, n: int) -> float:
    if len(values) < n or n <= 0:
        return 0.0
    return sum(values[-n:]) / n


def _sma_series(values: list, n: int) -> list:
    if len(values) < n:
        return []
    return [sum(values[i - n + 1:i + 1]) / n for i in range(n - 1, len(values))]


def _swing_lows(lows: list, k: int, window: int = None) -> list:
    """Mínimos pivote: low[i] es el menor entre k velas a cada lado.
    Devuelve [(idx, price)]. `window` limita la búsqueda a las últimas N velas."""
    start = k
    if window is not None:
        start = max(k, len(lows) - window)
    out = []
    for i in range(start, len(lows) - k):
        seg = lows[i - k:i + k + 1]
        if lows[i] == min(seg):
            out.append((i, lows[i]))
    return out


def _volume_poc(highs: list, lows: list, closes: list, vols: list, lookback: int = 120, bins: int = 40) -> float:
    """Punto de control de volumen (precio donde más se negoció) en las últimas `lookback` velas."""
    h, l, c, v = highs[-lookback:], lows[-lookback:], closes[-lookback:], vols[-lookback:]
    lo, hi = min(l), max(h)
    if hi <= lo:
        return 0.0
    width = (hi - lo) / bins
    buckets = [0.0] * bins
    for i in range(len(c)):
        typ = (h[i] + l[i] + c[i]) / 3
        idx = min(int((typ - lo) / width), bins - 1)
        buckets[idx] += v[i]
    poc_idx = max(range(bins), key=lambda i: buckets[i])
    return lo + (poc_idx + 0.5) * width


def _fib_levels(lows: list, highs: list, lookback: int = 180) -> list:
    """Niveles Fibonacci (0.382/0.5/0.618) del último gran swing (mín->máx) en `lookback` velas."""
    l, h = lows[-lookback:], highs[-lookback:]
    swing_low = min(l)
    # máximo posterior al mínimo (swing al alza desde donde medimos retrocesos)
    lo_idx = l.index(swing_low)
    after = h[lo_idx:]
    swing_high = max(after) if after else max(h)
    rng = swing_high - swing_low
    if rng <= 0:
        return []
    return [
        ("Fib 0.382", swing_high - rng * 0.382),
        ("Fib 0.5",   swing_high - rng * 0.5),
        ("Fib 0.618", swing_high - rng * 0.618),
    ]


def _value_area_low(highs: list, lows: list, closes: list, vols: list,
                    lookback: int = 120, bins: int = 40, va: float = 0.70) -> float:
    """Límite INFERIOR del área de valor (zona que concentra el `va`% del volumen alrededor del POC).
    Es un soporte estructural: por debajo del VAL hay 'poco interés' previo → suele frenar caídas."""
    h, l, c, v = highs[-lookback:], lows[-lookback:], closes[-lookback:], vols[-lookback:]
    lo, hi = min(l), max(h)
    if hi <= lo:
        return 0.0
    width = (hi - lo) / bins
    buckets = [0.0] * bins
    for i in range(len(c)):
        typ = (h[i] + l[i] + c[i]) / 3
        idx = min(int((typ - lo) / width), bins - 1)
        buckets[idx] += v[i]
    total = sum(buckets)
    if total <= 0:
        return 0.0
    poc_i = max(range(bins), key=lambda i: buckets[i])
    lo_i = hi_i = poc_i
    acc = buckets[poc_i]
    target = total * va
    while acc < target and (lo_i > 0 or hi_i < bins - 1):
        left = buckets[lo_i - 1] if lo_i > 0 else -1.0
        right = buckets[hi_i + 1] if hi_i < bins - 1 else -1.0
        if right >= left:
            hi_i += 1; acc += buckets[hi_i]
        else:
            lo_i -= 1; acc += buckets[lo_i]
    return lo + lo_i * width


def _round_levels(price: float, n: int = 2) -> list:
    """Niveles redondos / psicológicos por DEBAJO del precio (paso según magnitud)."""
    step = 1 if price < 20 else 5 if price < 100 else 10 if price < 500 else 50
    base = math.floor(price / step) * step
    out, lvl = [], base
    while len(out) < n and lvl > 0:
        if lvl < price * 0.999:
            out.append(round(lvl, 2))
        lvl -= step
    return out


def _reaction_pct(level: float, lows: list, highs: list, tol: float, fwd: int = 10) -> float:
    """Calidad del rebote (mediana): cuánto subió el precio en `fwd` velas tras TOCAR el nivel.
    Mide la fuerza histórica del soporte por su REACCIÓN, no solo por el nº de toques."""
    bounces = []
    n = len(lows)
    for i in range(n - 1):
        if abs(lows[i] - level) <= tol:
            end = min(i + fwd, n - 1)
            fwd_max = max(highs[i:end + 1]) if end >= i else highs[i]
            if lows[i] > 0:
                bounces.append((fwd_max / lows[i] - 1) * 100)
    if not bounces:
        return 0.0
    bounces.sort()
    return round(bounces[len(bounces) // 2], 1)


def _weekly_supports(bars: list) -> list:
    """Soportes del marco SEMANAL (contexto swing): EMA20 sem, SMA50 sem, mínimo de N semanas.
    Resamplea las barras diarias a semanas ISO. Devuelve candidatos 'major'."""
    weeks = {}
    order = []
    for b in bars:
        try:
            d = datetime.strptime(b["t"][:10], "%Y-%m-%d")
        except Exception:
            continue
        key = d.isocalendar()[:2]
        if key not in weeks:
            weeks[key] = {"h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"])}
            order.append(key)
        else:
            w = weeks[key]
            w["h"] = max(w["h"], float(b["h"])); w["l"] = min(w["l"], float(b["l"])); w["c"] = float(b["c"])
    wk = [weeks[k] for k in order]
    if len(wk) < 6:
        return []
    wc = [w["c"] for w in wk]; wl = [w["l"] for w in wk]
    out = []
    ema = _ema(wc, P["WEEKLY_EMA"])
    if ema:
        out.append({"price": ema[-1], "type": "EMA20 sem", "major": True})
    if len(wc) >= P["WEEKLY_SMA"]:
        out.append({"price": sum(wc[-P["WEEKLY_SMA"]:]) / P["WEEKLY_SMA"], "type": "SMA50 sem", "major": True})
    win = min(P["WEEKLY_LOW_WIN"], len(wl))
    out.append({"price": min(wl[-win:]), "type": f"mín {win}sem", "major": True})
    return out


# ── Clustering de niveles cercanos en zonas ─────────────────────────────────────

def _cluster(levels: list, tol: float) -> list:
    """Agrupa niveles dentro de `tol`. levels = [{price,type,major}]. Devuelve zonas."""
    if not levels:
        return []
    levels = sorted(levels, key=lambda x: x["price"])
    clusters = [{"ref": levels[0]["price"], "members": [levels[0]]}]
    for lv in levels[1:]:
        if abs(lv["price"] - clusters[-1]["ref"]) <= tol:
            clusters[-1]["members"].append(lv)
            prices = [m["price"] for m in clusters[-1]["members"]]
            clusters[-1]["ref"] = sum(prices) / len(prices)
        else:
            clusters.append({"ref": lv["price"], "members": [lv]})
    return clusters


def _build_supports(candidates: list, price: float, atr14: float, all_pivots: list,
                    poc: float, lows: list, highs: list, max_out: int) -> list:
    """Convierte candidatos en zonas de soporte con fuerza 1-5. Solo por debajo del precio.
    La fuerza premia: confluencia + zona testeada (toques) + CALIDAD del rebote histórico +
    ancla mayor + respaldo de volumen. La calidad del rebote (improvement 3) usa la reacción
    mediana tras tocar la zona, no solo cuántas veces se tocó."""
    tol = max(atr14 * P["CLUSTER_ATR_MULT"], price * P["CLUSTER_PCT"])
    cand = [c for c in candidates if 0 < c["price"] < price * 0.999]
    clusters = _cluster(cand, tol)

    supports = []
    for cl in clusters:
        ref = cl["ref"]
        types = []
        for m in cl["members"]:
            if m["type"] not in types:
                types.append(m["type"])
        is_major = any(m.get("major") for m in cl["members"])
        touches = sum(1 for (_, pv) in all_pivots if abs(pv - ref) <= tol)
        vol_confirm = poc > 0 and abs(poc - ref) <= tol
        reaction = _reaction_pct(ref, lows, highs, tol, P["REACTION_FWD"])
        reaction_strong = reaction >= P["REACTION_STRONG_PCT"]

        # Hasta 6 bonificadores, fuerza acotada 1-5
        strength = 1
        if len(types) >= 2:
            strength += 1            # confluencia
        if touches >= 2:
            strength += 1            # zona testeada
        if reaction_strong:
            strength += 1            # rebote de calidad (improvement 3)
        if is_major:
            strength += 1            # ancla mayor
        if vol_confirm:
            strength += 1            # respaldado por volumen
        strength = min(strength, 5)

        supports.append({
            "price": round(ref, 2),
            "dist_pct": round((price - ref) / price * 100, 2),
            "label": " + ".join(types),
            "strength": strength,
            "touches": touches,
            "reaction_pct": reaction,
        })

    supports.sort(key=lambda s: s["dist_pct"])
    return supports[:max_out]


# ── Análisis principal por ticker ───────────────────────────────────────────────

def analyze_ticker(ticker: str, bars: list, spy_map: dict = None,
                   live_price: float = None) -> dict:
    """Calcula soportes + chips de riesgo para un ticker. Devuelve None si datos insuficientes.
    spy_map: {fecha(YYYY-MM-DD): cierre SPY} para beta + abnormal return alineados por fecha."""
    if len(bars) < 30:
        return None

    closes = [float(b["c"]) for b in bars]
    highs  = [float(b["h"]) for b in bars]
    lows   = [float(b["l"]) for b in bars]
    opens  = [float(b["o"]) for b in bars]
    vols   = [float(b.get("v") or 0) for b in bars]

    price = closes[-1]                      # base reproducible = último cierre diario
    prev  = closes[-2] if len(closes) >= 2 else price
    atr14 = _atr(highs, lows, closes, 14) or price * 0.03

    ema20 = _ema(closes, P["EMA_SHORT"])[-1]
    ema50 = _ema(closes, P["EMA_MID"])[-1] if len(closes) >= P["EMA_MID"] else 0.0
    _, bb_lower = _bollinger(closes, P["BB_LEN"])
    sma200 = _sma(closes, P["SMA_LONG"])
    poc = _volume_poc(highs, lows, closes, vols, P["POC_LOOKBACK"], P["POC_BINS"])
    val = _value_area_low(highs, lows, closes, vols, P["POC_LOOKBACK"], P["POC_BINS"], P["VALUE_AREA"])

    piv_recent = _swing_lows(lows, k=P["SWING_RECENT_K"], window=P["SWING_RECENT_WIN"])
    piv_major  = _swing_lows(lows, k=P["SWING_MAJOR_K"])
    all_pivots = piv_recent + piv_major

    # ── Candidatos CORTO PLAZO ──
    st = [
        {"price": ema20, "type": "EMA20", "major": False},
        {"price": min(lows[-P["RECENT_LOW"]:]), "type": "mín 10d", "major": False},
    ]
    if bb_lower:
        st.append({"price": bb_lower, "type": "BB inf", "major": False})
    for _, pv in piv_recent:
        st.append({"price": pv, "type": "swing reciente", "major": False})
    for rl in _round_levels(price, P["ROUND_N"]):   # niveles redondos (improvement 4)
        st.append({"price": rl, "type": "redondo", "major": False})

    # ── Candidatos ESTRUCTURALES ──
    stc = []
    if sma200:
        stc.append({"price": sma200, "type": "SMA200", "major": True})
    if ema50:
        stc.append({"price": ema50, "type": "EMA50", "major": True})
    for _, pv in piv_major:
        stc.append({"price": pv, "type": "swing mayor", "major": True})
    for name, lvl in _fib_levels(lows, highs, P["FIB_LOOKBACK"]):
        stc.append({"price": lvl, "type": name, "major": False})
    if poc:
        stc.append({"price": poc, "type": "vol POC", "major": False})
    if val:
        stc.append({"price": val, "type": "VAL", "major": False})   # value-area low (improvement 4)
    stc.extend(_weekly_supports(bars))                              # semanal (improvement 2)

    short_sup = _build_supports(st, price, atr14, all_pivots, poc, lows, highs, max_out=3)
    struct_sup = _build_supports(stc, price, atr14, all_pivots, poc, lows, highs, max_out=3)

    # ── Chips de riesgo (solo datos) ──
    rsi14 = _rsi(closes, 14)
    high60 = max(highs[-60:])
    drawdown = round((price / high60 - 1) * 100, 1)        # negativo

    sma50_series = _sma_series(closes, 50)
    slope_up = len(sma50_series) >= 11 and sma50_series[-1] > sma50_series[-11]
    above_200 = sma200 > 0 and price > sma200
    trend_healthy = bool(above_200 and slope_up)

    # Abnormal return AJUSTADO POR BETA: cuánto cayó el ticker MÁS (o menos) de lo que su
    # volatilidad explica. expected = beta × retorno_SPY; excess = retorno_ticker − expected.
    # Así un volátil (β>1) que cae proporcional al mercado NO se marca como débil.
    # Retornos alineados por FECHA con SPY (evita beta ruidosa por días desfasados).
    ret5 = round((price / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else 0.0
    beta = 1.0
    spy_ret5 = 0.0
    if spy_map:
        dates = [b["t"][:10] for b in bars]
        aligned = [(closes[i], spy_map[d]) for i, d in enumerate(dates) if d in spy_map]
        if len(aligned) >= 26:
            t_cl = [a[0] for a in aligned]
            s_cl = [a[1] for a in aligned]
            t_ret = [t_cl[i] / t_cl[i - 1] - 1 for i in range(1, len(t_cl))]
            s_ret = [s_cl[i] / s_cl[i - 1] - 1 for i in range(1, len(s_cl))]
            beta = _beta(t_ret, s_ret)
            if len(s_cl) >= 6:
                spy_ret5 = (s_cl[-1] / s_cl[-6] - 1) * 100
    expected = beta * spy_ret5
    vs_spy = round(ret5 - expected, 1)                     # excess ajustado por beta
    idiosyncratic = vs_spy <= -3.0                         # cae más de lo que su beta explica

    avg_vol20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else (sum(vols) / len(vols) if vols else 1)
    vol_ratio = round(vols[-1] / avg_vol20, 1) if avg_vol20 else 1.0

    gap_pct = round((opens[-1] / prev - 1) * 100, 1) if prev else 0.0
    is_gap = abs(gap_pct) >= 2.0
    change_today = round((price / prev - 1) * 100, 2) if prev else 0.0

    # % al soporte más cercano (clave de ranking). Si no hay corto, cae al estructural.
    nearest_sup = short_sup[0] if short_sup else (struct_sup[0] if struct_sup else None)
    nearest = nearest_sup["dist_pct"] if nearest_sup else None

    # ── Score de ACCIONABILIDAD 0-100 (improvement 1) ──
    # Combina fuerza del soporte más cercano + tendencia sana + caída macro (no idiosincrática)
    # − penalización por lejanía al soporte. Surfacea los MEJORES dip-buys, no solo el más cercano.
    # Idéntico en el Pine para mantener alineación.
    actionability = None
    if nearest_sup is not None:
        sc = 15 * nearest_sup["strength"]              # 15..75 por fuerza (1-5)
        if trend_healthy:
            sc += 15                                   # tendencia de fondo sana
        if not idiosyncratic:
            sc += 10                                   # caída macro (más sana que problema propio)
        sc -= min(max(nearest if nearest is not None else 0, 0), 10)   # lejanía resta hasta 10
        actionability = max(0, min(100, round(sc)))

    return {
        "ticker": ticker,
        "price": round(price, 2),
        "live_price": round(live_price, 2) if live_price else None,
        "change_today": change_today,
        "nearest_pct": nearest,
        "actionability": actionability,
        "short_supports": short_sup,
        "struct_supports": struct_sup,
        "risk": {
            "trend_healthy": trend_healthy,
            "above_200": above_200,
            "rsi": rsi14,
            "drawdown": drawdown,
            "vs_spy": vs_spy,
            "beta": round(beta, 2),
            "idiosyncratic": idiosyncratic,
            "vol_ratio": vol_ratio,
            "gap": is_gap,
            "gap_pct": gap_pct,
        },
    }
