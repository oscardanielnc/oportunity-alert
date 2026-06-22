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
import logging
import requests
from datetime import datetime, timezone, timedelta

# Reutilizamos los indicadores puros ya auditados en conviction_gates.
from utils.conviction_gates import _ema, _rsi, _atr, _bollinger

logger = logging.getLogger(__name__)

ALPACA_BASE = "https://data.alpaca.markets"
ALPACA_FEED_BARS = "sip"


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


def _build_supports(candidates: list, price: float, atr14: float,
                    all_pivots: list, poc: float, max_out: int) -> list:
    """Convierte candidatos en zonas de soporte con fuerza 1-5. Solo por debajo del precio."""
    tol = max(atr14 * 0.5, price * 0.012)
    # Solo niveles válidos: por debajo del precio y a una distancia sensata
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
        # toques: pivotes históricos que cayeron dentro de la zona
        touches = sum(1 for (_, pv) in all_pivots if abs(pv - ref) <= tol)
        vol_confirm = poc > 0 and abs(poc - ref) <= tol

        strength = 1
        if len(types) >= 2:
            strength += 1            # confluencia
        if touches >= 2:
            strength += 1            # zona testeada
        if is_major:
            strength += 1            # ancla mayor (SMA200/EMA50/swing mayor)
        if vol_confirm:
            strength += 1            # respaldado por volumen
        strength = min(strength, 5)

        supports.append({
            "price": round(ref, 2),
            "dist_pct": round((price - ref) / price * 100, 2),
            "label": " + ".join(types),
            "strength": strength,
            "touches": touches,
        })

    supports.sort(key=lambda s: s["dist_pct"])
    return supports[:max_out]


# ── Análisis principal por ticker ───────────────────────────────────────────────

def analyze_ticker(ticker: str, bars: list, spy_ret5: float = 0.0,
                   live_price: float = None) -> dict:
    """Calcula soportes + chips de riesgo para un ticker. Devuelve None si datos insuficientes."""
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

    ema20 = _ema(closes, 20)[-1]
    ema50 = _ema(closes, 50)[-1] if len(closes) >= 50 else 0.0
    _, bb_lower = _bollinger(closes, 20)
    sma200 = _sma(closes, 200)
    poc = _volume_poc(highs, lows, closes, vols)

    piv_recent = _swing_lows(lows, k=3, window=45)
    piv_major  = _swing_lows(lows, k=7)
    all_pivots = piv_recent + piv_major

    # ── Candidatos CORTO PLAZO ──
    st = [
        {"price": ema20, "type": "EMA20", "major": False},
        {"price": min(lows[-10:]), "type": "mín 10d", "major": False},
    ]
    if bb_lower:
        st.append({"price": bb_lower, "type": "BB inf", "major": False})
    for _, pv in piv_recent:
        st.append({"price": pv, "type": "swing reciente", "major": False})

    # ── Candidatos ESTRUCTURALES ──
    stc = []
    if sma200:
        stc.append({"price": sma200, "type": "SMA200", "major": True})
    if ema50:
        stc.append({"price": ema50, "type": "EMA50", "major": True})
    for _, pv in piv_major:
        stc.append({"price": pv, "type": "swing mayor", "major": True})
    for name, lvl in _fib_levels(lows, highs):
        stc.append({"price": lvl, "type": name, "major": False})
    if poc:
        stc.append({"price": poc, "type": "vol POC", "major": False})

    short_sup = _build_supports(st, price, atr14, all_pivots, poc, max_out=3)
    struct_sup = _build_supports(stc, price, atr14, all_pivots, poc, max_out=3)

    # ── Chips de riesgo (solo datos) ──
    rsi14 = _rsi(closes, 14)
    high60 = max(highs[-60:])
    drawdown = round((price / high60 - 1) * 100, 1)        # negativo

    sma50_series = _sma_series(closes, 50)
    slope_up = len(sma50_series) >= 11 and sma50_series[-1] > sma50_series[-11]
    above_200 = sma200 > 0 and price > sma200
    trend_healthy = bool(above_200 and slope_up)

    ret5 = round((price / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else 0.0
    vs_spy = round(ret5 - spy_ret5, 1)                     # excess vs mercado
    idiosyncratic = vs_spy <= -3.0                         # cae bastante más que SPY

    avg_vol20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else (sum(vols) / len(vols) if vols else 1)
    vol_ratio = round(vols[-1] / avg_vol20, 1) if avg_vol20 else 1.0

    gap_pct = round((opens[-1] / prev - 1) * 100, 1) if prev else 0.0
    is_gap = abs(gap_pct) >= 2.0
    change_today = round((price / prev - 1) * 100, 2) if prev else 0.0

    # % al soporte más cercano (clave de ranking). Si no hay corto, cae al estructural.
    if short_sup:
        nearest = short_sup[0]["dist_pct"]
    elif struct_sup:
        nearest = struct_sup[0]["dist_pct"]
    else:
        nearest = None

    return {
        "ticker": ticker,
        "price": round(price, 2),
        "live_price": round(live_price, 2) if live_price else None,
        "change_today": change_today,
        "nearest_pct": nearest,
        "short_supports": short_sup,
        "struct_supports": struct_sup,
        "risk": {
            "trend_healthy": trend_healthy,
            "above_200": above_200,
            "rsi": rsi14,
            "drawdown": drawdown,
            "vs_spy": vs_spy,
            "idiosyncratic": idiosyncratic,
            "vol_ratio": vol_ratio,
            "gap": is_gap,
            "gap_pct": gap_pct,
        },
    }
