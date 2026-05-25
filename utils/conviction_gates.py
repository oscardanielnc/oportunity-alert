"""
Conviction Gates v2.0 — evaluación técnica pura antes de llamar IA.
Calcula score 0-7 usando código (sin tokens). Si score < 4 → skip_ai = True.
"""
import os
import logging
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

ALPACA_BASE = "https://data.alpaca.markets"
ALPACA_FEED = "iex"

# Gate 1: umbral de cambio % para considerar catalizador ya priceado
PRICED_THRESHOLDS = {
    "QBTS": 30, "IONQ": 30, "RGTI": 30, "QUBT": 30,
    "GME": 30, "AMC": 30,
    "MARA": 25, "RIOT": 25, "MSTR": 25,
    "SOUN": 25, "BBAI": 25, "INOD": 25, "APLD": 25, "IREN": 25,
    "RKLB": 20, "LUNR": 20, "JOBY": 20, "ACHR": 20,
    "NVDA": 15, "TSLA": 15, "SMCI": 15, "RIVN": 15,
    "LCID": 15, "WOLF": 15, "PLTR": 15, "APP": 15,
}
DEFAULT_THRESHOLD = 12

# ATR multipliers por sector: (stop_mult, target_mult)
ATR_MULT = {
    "QUANTUM":  (2.0, 4.0),
    "SMALL_AI": (2.0, 3.5),
    "SPACE":    (1.8, 3.0),
    "HIGHVOL":  (1.5, 2.5),
    "DEFAULT":  (1.5, 2.5),
}

SECTOR_FOR_ATR = {
    "QBTS": "QUANTUM", "IONQ": "QUANTUM", "RGTI": "QUANTUM", "QUBT": "QUANTUM",
    "SOUN": "SMALL_AI", "BBAI": "SMALL_AI", "INOD": "SMALL_AI",
    "APLD": "SMALL_AI", "IREN": "SMALL_AI",
    "RKLB": "SPACE", "LUNR": "SPACE", "JOBY": "SPACE", "ACHR": "SPACE",
    "NVDA": "HIGHVOL", "TSLA": "HIGHVOL", "PLTR": "HIGHVOL",
    "APP": "HIGHVOL", "SMCI": "HIGHVOL",
}

GATE_SKIP_THRESHOLD = 4


# ── Indicadores técnicos (funciones puras, sin librerías externas) ─────────────

def _ema(values: list, n: int) -> list:
    if not values or n <= 0:
        return values
    k = 2 / (n + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _rsi(closes: list, n: int = 14) -> float:
    if len(closes) < n + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _atr(highs: list, lows: list, closes: list, n: int = 14) -> float:
    if len(closes) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr_val = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        atr_val = (atr_val * (n - 1) + trs[i]) / n
    return round(atr_val, 4)


def _bollinger(closes: list, n: int = 20, std_mult: float = 2.0):
    if len(closes) < n:
        return None, None
    window = closes[-n:]
    sma = sum(window) / n
    variance = sum((c - sma) ** 2 for c in window) / n
    std = variance ** 0.5
    return round(sma + std_mult * std, 4), round(sma - std_mult * std, 4)


# ── Llamada Alpaca: barras diarias ─────────────────────────────────────────────

def _get_daily_bars(ticker: str) -> list:
    """Retorna últimas 60 barras diarias de Alpaca IEX. Lista vacía si falla."""
    try:
        key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            logger.warning("[Gates] Alpaca keys no configuradas — Gate 2 degradado")
            return []
        headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        }
        resp = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
            params={"timeframe": "1Day", "limit": 60, "feed": ALPACA_FEED, "sort": "asc"},
            headers=headers,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json().get("bars") or []
    except Exception as e:
        logger.warning(f"[Gates] Error barras diarias {ticker}: {e}")
        return []


# ── Gates ──────────────────────────────────────────────────────────────────────

def _gate1(ticker: str, price_data: dict) -> tuple:
    """Retorna (score, reasoning_fragment)."""
    change = price_data.get("change_pct")
    if change is None:
        return 2, "change_pct no disponible"
    threshold = PRICED_THRESHOLDS.get(ticker, DEFAULT_THRESHOLD)
    if abs(change) < threshold:
        return 3, f"cambio {change:+.1f}% < umbral {threshold}%"
    return 0, f"cambio {change:+.1f}% >= umbral {threshold}% → ya priceado"


def _gate2(ticker: str, price_data: dict, direction: str) -> tuple:
    """Retorna (score 0-3, rsi, ema20, ema50, bb_upper, bb_lower, atr14)."""
    bars = _get_daily_bars(ticker)
    if len(bars) < 20:
        logger.debug(f"[Gates] {ticker}: barras insuficientes ({len(bars)}) → Gate 2 degradado")
        return 1, 50.0, 0.0, 0.0, 0.0, 0.0, 0.0

    closes = [float(b["c"]) for b in bars]
    highs  = [float(b["h"]) for b in bars]
    lows   = [float(b["l"]) for b in bars]

    rsi14    = _rsi(closes, 14)
    ema20    = _ema(closes, 20)[-1]
    ema50    = _ema(closes, 50)[-1] if len(closes) >= 50 else ema20
    bb_upper, bb_lower = _bollinger(closes, 20)
    atr14    = _atr(highs, lows, closes, 14)

    price = price_data.get("current_price") or closes[-1]

    if direction == "LONG":
        rsi_ok = rsi14 < 72
        ema_ok = price > ema20 * 0.97
        bb_ok  = bb_upper is None or price < bb_upper * 0.99
    else:
        rsi_ok = rsi14 > 28
        ema_ok = price < ema20 * 1.03
        bb_ok  = bb_lower is None or price > bb_lower * 1.01

    score = int(rsi_ok) + int(ema_ok) + int(bb_ok)
    return score, rsi14, round(ema20, 2), round(ema50, 2), round(bb_upper or 0, 2), round(bb_lower or 0, 2), atr14


def _gate3(price_data: dict) -> int:
    """Retorna 0 o 1."""
    last_trade_age = price_data.get("last_trade_age_minutes") or 0
    candles = price_data.get("candles_1m", [])

    if last_trade_age > 30 or len(candles) < 5:
        return 1

    vols = [c["v"] for c in candles if c.get("v", 0) > 0]
    if not vols:
        return 1
    recent_vol = sum(vols[:3]) / max(len(vols[:3]), 1)
    avg_vol    = sum(vols) / len(vols)
    return 1 if recent_vol > avg_vol * 1.3 else 0


# ── Función principal ──────────────────────────────────────────────────────────

def evaluate_conviction(
    ticker: str,
    price_data: dict,
    direction: str = "LONG",
) -> dict:
    """
    Evalúa los 3 conviction gates y calcula stop/target basados en ATR.

    Retorna dict con:
      conviction_score (0-7), skip_ai (bool),
      gate1_score, gate2_score, gate3_score,
      gate2_rsi, gate2_ema20, gate2_ema50, gate2_bb_upper, gate2_bb_lower,
      atr14, entry_price, stop_code, target_code, reasoning
    """
    result = {
        "conviction_score": 0,
        "skip_ai": True,
        "gate1_score": 0,
        "gate2_score": 0,
        "gate3_score": 0,
        "gate2_rsi": 50.0,
        "gate2_ema20": 0.0,
        "gate2_ema50": 0.0,
        "gate2_bb_upper": 0.0,
        "gate2_bb_lower": 0.0,
        "atr14": 0.0,
        "entry_price": 0.0,
        "stop_code": 0.0,
        "target_code": 0.0,
        "reasoning": "",
    }

    # Gate 1
    g1_score, g1_reason = _gate1(ticker, price_data)
    result["gate1_score"] = g1_score

    if g1_score == 0:
        result["skip_ai"] = True
        result["reasoning"] = f"Gate1 FAIL: {g1_reason}"
        return result

    # Gate 2
    g2_score, rsi14, ema20, ema50, bb_upper, bb_lower, atr14 = _gate2(ticker, price_data, direction)
    result["gate2_score"]    = g2_score
    result["gate2_rsi"]      = rsi14
    result["gate2_ema20"]    = ema20
    result["gate2_ema50"]    = ema50
    result["gate2_bb_upper"] = bb_upper
    result["gate2_bb_lower"] = bb_lower
    result["atr14"]          = atr14

    # Gate 3
    g3_score = _gate3(price_data)
    result["gate3_score"] = g3_score

    # Score total
    total = g1_score + g2_score + g3_score
    result["conviction_score"] = total
    result["skip_ai"] = total < GATE_SKIP_THRESHOLD

    # Stop y target basados en ATR
    entry = price_data.get("current_price") or 0.0
    result["entry_price"] = round(entry, 2)

    if atr14 > 0 and entry > 0:
        sector = SECTOR_FOR_ATR.get(ticker, "DEFAULT")
        stop_m, target_m = ATR_MULT[sector]
        if direction == "LONG":
            result["stop_code"]   = round(entry - stop_m * atr14, 2)
            result["target_code"] = round(entry + target_m * atr14, 2)
        else:
            result["stop_code"]   = round(entry + stop_m * atr14, 2)
            result["target_code"] = round(entry - target_m * atr14, 2)

    # Reasoning legible
    gate_str = (
        f"G1={g1_score} G2={g2_score}/3 G3={g3_score} | "
        f"RSI={rsi14:.1f} EMA20=${ema20:.2f} ATR=${atr14:.2f}"
    )
    result["reasoning"] = (
        f"{'PASA' if not result['skip_ai'] else 'DESCARTADO'} ({total}/7) — {gate_str}"
    )

    return result
