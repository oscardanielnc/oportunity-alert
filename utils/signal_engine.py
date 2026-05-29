"""
Signal engine for the Ticker Watcher.
Computes technical indicators and emits actionable signals.
v3 — candlestick patterns, HTF trend, direction lock, improved exits.

Scoring system (max 9 pts):
  L1/S1: Bollinger Band extreme        — 1 pt
  L2/S2: RSI extreme (<35 / >65)       — 1 pt
  L3/S3: MACD histogram improving      — 1 pt
  L4/S4: Support/Resistance proximity  — 1 pt
  Patterns (replaces L5/S5)            — 0-4 pts (weighted by pattern strength)
  L6/S6: Volume confirmation (>1.2×)   — 1 pt

Entry thresholds by HTF trend (SIN_POSICION only):
  HTF BULLISH → LONG ≥ 4,  SHORT ≥ 8  (very selective against trend)
  HTF NEUTRAL → LONG ≥ 5,  SHORT ≥ 5  (SIP feed: 7/7)
  HTF BEARISH → LONG ≥ 8,  SHORT ≥ 4

Exit thresholds (already in position, no HTF applied):
  CERRAR     if opposite score ≥ 3   (was 4 — faster exits)
  RSI OVERRIDE: RSI<30 while SHORT → CERRAR_SHORT; RSI>70 while LONG → CERRAR_LONG
  PRECAUCION if opposite score ≥ 2

Pattern momentum filter: if price moved >0.5% in last 10 bars in one direction,
  downweight bear_score by 2 during confirmed uptrend (and vice-versa).
"""
import os
import logging
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

ALPACA_BASE      = "https://data.alpaca.markets"
INTERVAL_TF      = {"1m": "1Min", "5m": "5Min", "15m": "15Min"}
INTERVAL_HOURS_BACK = {"1m": 4, "5m": 24, "15m": 72}

# Higher timeframe for trend context: interval → (Alpaca tf, hours to fetch)
_HTF_TF    = {"1m": "5Min", "5m": "15Min", "15m": "1Hour"}
_HTF_HOURS = {"1m": 8,      "5m": 24,       "15m": 72}

# After confirming an ENTRAR signal, block the opposite direction for N scans
DIRECTION_LOCK_SCANS = 5


# ── Data fetch ─────────────────────────────────────────────────────────────────

def _fetch_bars_feed(ticker: str, timeframe: str, start: str,
                     limit: int, feed: str, headers: dict) -> list:
    """Single feed bars fetch — returns chronological list or []."""
    try:
        r = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
            params={"timeframe": timeframe, "start": start,
                    "feed": feed, "limit": limit, "sort": "desc"},
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        bars = r.json().get("bars") or []
        bars.reverse()  # desc → chronological
        return [{"t": b["t"], "o": b["o"], "h": b["h"],
                 "l": b["l"], "c": b["c"], "v": b["v"]} for b in bars]
    except Exception as e:
        logger.warning("[SignalEngine] fetch_bars_feed %s %s %s: %s", ticker, feed, timeframe, e)
        return []


def fetch_bars(ticker: str, interval: str, limit: int = 60) -> list:
    """
    IEX during regular hours (real-time), SIP fallback outside.
    Falls back to SIP when IEX last bar is >30 min old.
    """
    timeframe  = INTERVAL_TF.get(interval, "1Min")
    hours_back = INTERVAL_HOURS_BACK.get(interval, 4)
    start = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    headers = {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    }

    iex_bars = _fetch_bars_feed(ticker, timeframe, start, limit, "iex", headers)

    iex_fresh = False
    if iex_bars:
        try:
            last_t = datetime.fromisoformat(iex_bars[-1]["t"].replace("Z", "+00:00"))
            iex_fresh = (datetime.now(timezone.utc) - last_t).total_seconds() / 60 <= 30
        except Exception:
            pass

    if iex_fresh:
        return iex_bars

    sip_bars = _fetch_bars_feed(ticker, timeframe, start, limit, "sip", headers)
    if sip_bars:
        logger.debug("[SignalEngine] %s IEX stale/empty → SIP (%d bars)", ticker, len(sip_bars))
        return sip_bars
    return iex_bars


def fetch_htf_bars(ticker: str, interval: str) -> list:
    """Fetch higher-timeframe bars for trend context (1m→5m, 5m→15m, 15m→1h)."""
    htf_tf     = _HTF_TF.get(interval, "5Min")
    hours_back = _HTF_HOURS.get(interval, 8)
    headers = {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    }
    start = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    bars = _fetch_bars_feed(ticker, htf_tf, start, 40, "iex", headers)
    if bars:
        try:
            last_t = datetime.fromisoformat(bars[-1]["t"].replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60 > 30:
                bars = []
        except Exception:
            pass
    if not bars:
        bars = _fetch_bars_feed(ticker, htf_tf, start, 40, "sip", headers)
    return bars


def fetch_latest_price(ticker: str) -> dict:
    """
    IEX real-time latest trade; SIP bar fallback when IEX is stale (>5 min).
    """
    headers = {
        "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    }
    price = None
    trade_age_min = None
    try:
        r = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/trades/latest",
            params={"feed": "iex"},
            headers=headers,
            timeout=8,
        )
        r.raise_for_status()
        trade = r.json().get("trade") or {}
        price = trade.get("p")
        trade_time_str = trade.get("t", "")
        if trade_time_str:
            try:
                from dateutil import parser as _dp
                trade_dt = _dp.parse(trade_time_str)
                now_utc  = datetime.now(timezone.utc)
                if trade_dt.tzinfo is None:
                    trade_dt = trade_dt.replace(tzinfo=timezone.utc)
                trade_age_min = (now_utc - trade_dt).total_seconds() / 60
            except Exception:
                pass
    except Exception as e:
        logger.warning("[SignalEngine] fetch_latest_price IEX %s: %s", ticker, e)

    if trade_age_min is None or trade_age_min > 5:
        try:
            sip_start = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            br = requests.get(
                f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                params={"timeframe": "1Min", "start": sip_start,
                        "feed": "sip", "limit": 1, "sort": "desc"},
                headers=headers,
                timeout=8,
            )
            if br.status_code == 200:
                sip_bars = br.json().get("bars") or []
                if sip_bars and sip_bars[0].get("c"):
                    price = float(sip_bars[0]["c"])
                    logger.debug("[SignalEngine] %s IEX stale → SIP bar $%.2f", ticker, price)
        except Exception as e:
            logger.debug("[SignalEngine] SIP bar fallback %s: %s", ticker, e)

    change_pct = None
    try:
        snap = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/snapshot",
            params={"feed": "iex"},
            headers=headers,
            timeout=8,
        )
        if snap.status_code == 200 and price:
            prev = snap.json().get("prevDailyBar", {}).get("c")
            if prev:
                change_pct = round((float(price) - float(prev)) / float(prev) * 100, 2)
    except Exception:
        pass

    return {"price": float(price) if price else None,
            "change_pct": change_pct, "error": None}


# ── VWAP ──────────────────────────────────────────────────────────────────────

def _compute_vwap(candles: list) -> float | None:
    """
    Calcula el VWAP del dia desde la apertura regular (9:30am ET).
    Usa timestamps de las barras — no requiere API adicional.
    Retorna None si no hay barras de la sesion actual.
    """
    if not candles:
        return None
    try:
        last_t   = datetime.fromisoformat(candles[-1]["t"].replace("Z", "+00:00"))
        is_edt   = 3 <= last_t.month <= 11
        et_off   = timedelta(hours=-4 if is_edt else -5)
        last_et  = last_t + et_off
        # Apertura de sesion regular en ET del mismo dia
        open_et  = last_et.replace(hour=9, minute=30, second=0, microsecond=0)
        # Solo si la ultima barra ya paso las 9:30am ET
        if last_et < open_et:
            return None
        open_utc = open_et - et_off
        session  = [c for c in candles
                    if datetime.fromisoformat(c["t"].replace("Z", "+00:00")) >= open_utc]
        if not session:
            return None
        cum_tpv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in session)
        cum_vol = sum(b["v"] for b in session)
        return round(cum_tpv / cum_vol, 4) if cum_vol > 0 else None
    except Exception:
        return None


# ── Math helpers ───────────────────────────────────────────────────────────────

def _ema_series(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)


def _atr(candles: list, period: int = 14):
    if len(candles) < period + 1:
        return None
    trs = [
        max(candles[i]["h"] - candles[i]["l"],
            abs(candles[i]["h"] - candles[i - 1]["c"]),
            abs(candles[i]["l"] - candles[i - 1]["c"]))
        for i in range(1, len(candles))
    ]
    return sum(trs[-period:]) / period


def _find_support_resistance(candles: list, window: int = 3):
    recent = candles[-50:]
    n = len(recent)
    soportes, resistencias = [], []
    for i in range(window, n - window):
        neighbors = [j for j in range(i - window, i + window + 1) if j != i]
        lo = recent[i]["l"]
        hi = recent[i]["h"]
        if all(lo <= recent[j]["l"] for j in neighbors):
            soportes.append(lo)
        if all(hi >= recent[j]["h"] for j in neighbors):
            resistencias.append(hi)
    return (sorted(set(round(s, 4) for s in soportes)),
            sorted(set(round(r, 4) for r in resistencias)))


# ── Candlestick pattern detector ───────────────────────────────────────────────

def _detect_candle_patterns(candles: list) -> dict:
    """
    Detect classic reversal/continuation patterns in the last 3 candles.

    Pattern weights:
      3 — Strong reversal  (Engulfing, Morning Star, Evening Star)
      2 — Moderate         (Hammer, Shooting Star, Three Soldiers/Crows,
                            Piercing Line, Dark Cloud, Dragonfly/Gravestone Doji)
      1 — Weak / needs conf (Harami, Inverted Hammer, Hanging Man)

    Score capped at 4 per direction (strongest patterns dominate).
    """
    result = {"bull_patterns": [], "bear_patterns": [], "bull_score": 0, "bear_score": 0}
    n = len(candles) - 1
    if n < 2:
        return result
    c = candles

    def body(i):  return abs(c[i]["c"] - c[i]["o"])
    def rng(i):   return max(c[i]["h"] - c[i]["l"], 1e-8)
    def upper(i): return c[i]["h"] - max(c[i]["o"], c[i]["c"])
    def lower(i): return min(c[i]["o"], c[i]["c"]) - c[i]["l"]
    def grn(i):   return c[i]["c"] > c[i]["o"]
    def red(i):   return c[i]["c"] < c[i]["o"]

    # Preceding trend from last 5 bars before the pattern candle
    trend_start  = max(0, n - 5)
    prev_closes  = [c[i]["c"] for i in range(trend_start, n)]
    downtrend    = len(prev_closes) >= 3 and prev_closes[-1] < prev_closes[0]
    uptrend      = len(prev_closes) >= 3 and prev_closes[-1] > prev_closes[0]

    bull, bear = [], []

    # ── Bullish patterns ──────────────────────────────────────────────────────

    # Bullish Engulfing (3): red candle followed by green that fully wraps red body
    if n >= 1 and red(n - 1) and grn(n):
        if c[n]["c"] > c[n-1]["o"] and c[n]["o"] < c[n-1]["c"]:
            bull.append(("Bullish Engulfing", 3))

    # Morning Star (3): red, small/doji body, green closing above red midpoint
    if n >= 2 and red(n - 2) and grn(n):
        mid1 = (c[n-2]["o"] + c[n-2]["c"]) / 2
        if body(n-1) < 0.4 * max(body(n-2), 1e-8) and c[n]["c"] > mid1:
            bull.append(("Morning Star", 3))

    # Three White Soldiers (2): 3 consecutive green candles, each closing higher
    if n >= 2 and grn(n) and grn(n - 1) and grn(n - 2):
        if c[n]["c"] > c[n-1]["c"] > c[n-2]["c"]:
            bull.append(("Three White Soldiers", 2))

    # Hammer (2): after downtrend — lower wick ≥ 2×body, upper wick ≤ 0.5×body
    if downtrend:
        b, lw, uw = body(n), lower(n), upper(n)
        if b > 0 and lw >= 2 * b and uw <= 0.5 * b:
            bull.append(("Hammer", 2))

    # Piercing Line (2): red→green, green opens below red close, closes above red midpoint
    if n >= 1 and red(n - 1) and grn(n):
        mid_p = (c[n-1]["o"] + c[n-1]["c"]) / 2
        if c[n]["o"] < c[n-1]["c"] and c[n]["c"] > mid_p:
            bull.append(("Piercing Line", 2))

    # Dragonfly Doji (2): body<10% range, upper wick<15%, lower wick>60%
    if body(n) / rng(n) < 0.10 and upper(n) / rng(n) < 0.15 and lower(n) / rng(n) > 0.60:
        bull.append(("Dragonfly Doji", 2))

    # Bullish Harami (1): small green body inside large red body
    if n >= 1 and red(n - 1) and grn(n):
        prev_lo = min(c[n-1]["o"], c[n-1]["c"])
        prev_hi = max(c[n-1]["o"], c[n-1]["c"])
        if c[n]["o"] > prev_lo and c[n]["c"] < prev_hi and body(n) < 0.5 * body(n - 1):
            bull.append(("Bullish Harami", 1))

    # Inverted Hammer (1): after downtrend — upper wick ≥ 2×body, lower wick ≤ 0.5×body
    if downtrend:
        b, uw, lw = body(n), upper(n), lower(n)
        if b > 0 and uw >= 2 * b and lw <= 0.5 * b:
            bull.append(("Inverted Hammer", 1))

    # ── Bearish patterns ──────────────────────────────────────────────────────

    # Bearish Engulfing (3): green candle followed by red that fully wraps green body
    if n >= 1 and grn(n - 1) and red(n):
        if c[n]["c"] < c[n-1]["o"] and c[n]["o"] > c[n-1]["c"]:
            bear.append(("Bearish Engulfing", 3))

    # Evening Star (3): green, small/doji body, red closing below green midpoint
    if n >= 2 and grn(n - 2) and red(n):
        mid1 = (c[n-2]["o"] + c[n-2]["c"]) / 2
        if body(n-1) < 0.4 * max(body(n-2), 1e-8) and c[n]["c"] < mid1:
            bear.append(("Evening Star", 3))

    # Three Black Crows (2): 3 consecutive red candles, each closing lower
    if n >= 2 and red(n) and red(n - 1) and red(n - 2):
        if c[n]["c"] < c[n-1]["c"] < c[n-2]["c"]:
            bear.append(("Three Black Crows", 2))

    # Shooting Star (2): after uptrend — upper wick ≥ 2×body, lower wick ≤ 0.5×body
    if uptrend:
        b, uw, lw = body(n), upper(n), lower(n)
        if b > 0 and uw >= 2 * b and lw <= 0.5 * b:
            bear.append(("Shooting Star", 2))

    # Dark Cloud Cover (2): green→red, red opens above green high, closes below green midpoint
    if n >= 1 and grn(n - 1) and red(n):
        mid_p = (c[n-1]["o"] + c[n-1]["c"]) / 2
        if c[n]["o"] > c[n-1]["h"] and c[n]["c"] < mid_p:
            bear.append(("Dark Cloud Cover", 2))

    # Gravestone Doji (2): body<10% range, lower wick<15%, upper wick>60%
    if body(n) / rng(n) < 0.10 and lower(n) / rng(n) < 0.15 and upper(n) / rng(n) > 0.60:
        bear.append(("Gravestone Doji", 2))

    # Bearish Harami (1): small red body inside large green body
    if n >= 1 and grn(n - 1) and red(n):
        prev_lo = min(c[n-1]["o"], c[n-1]["c"])
        prev_hi = max(c[n-1]["o"], c[n-1]["c"])
        if c[n]["o"] < prev_hi and c[n]["c"] > prev_lo and body(n) < 0.5 * body(n - 1):
            bear.append(("Bearish Harami", 1))

    # Hanging Man (1): after uptrend — looks like hammer (lower wick ≥ 2×body)
    if uptrend:
        b, lw, uw = body(n), lower(n), upper(n)
        if b > 0 and lw >= 2 * b and uw <= 0.5 * b:
            bear.append(("Hanging Man", 1))

    # Sort by weight, cap score at 4
    bull.sort(key=lambda x: -x[1])
    bear.sort(key=lambda x: -x[1])
    result["bull_patterns"] = [name for name, _ in bull]
    result["bear_patterns"] = [name for name, _ in bear]
    result["bull_score"]    = min(4, sum(w for _, w in bull))
    result["bear_score"]    = min(4, sum(w for _, w in bear))
    return result


# ── HTF trend ─────────────────────────────────────────────────────────────────

def compute_htf_trend(htf_candles: list) -> str:
    """
    Returns 'BULLISH', 'BEARISH', or 'NEUTRAL' from higher-timeframe bars.
    Uses EMA20 position (>0.3% above/below) + MACD histogram sign.
    """
    if len(htf_candles) < 20:
        return "NEUTRAL"
    closes = [c["c"] for c in htf_candles]
    price  = closes[-1]

    ema20_s = _ema_series(closes, 20)
    if not ema20_s:
        return "NEUTRAL"
    ema20 = ema20_s[-1]

    macd_hist = 0.0
    ema12_s = _ema_series(closes, 12)
    ema26_s = _ema_series(closes, 26)
    if ema12_s and ema26_s:
        offset    = 26 - 12
        macd_line = [ema12_s[i + offset] - ema26_s[i] for i in range(len(ema26_s))]
        sig_s     = _ema_series(macd_line, 9)
        if sig_s:
            macd_hist = macd_line[-1] - sig_s[-1]

    pct = (price - ema20) / ema20 * 100 if ema20 else 0
    if pct > 0.3 and macd_hist > 0:
        return "BULLISH"
    if pct < -0.3 and macd_hist < 0:
        return "BEARISH"
    return "NEUTRAL"


# ── Core signal computation ────────────────────────────────────────────────────

def compute_signal(candles: list, position_state: str = "SIN_POSICION",
                   entry_price=None, htf_trend: str = "NEUTRAL",
                   signal_params: dict | None = None) -> dict:
    """
    Evaluate criteria and return a signal dict.
    Requires at least 30 bars.

    signal_params (calibratable per ticker, all optional):
      c3_vol_min     (float, default 1.2): vol_ratio minimo para que MACD (C3/S3)
                     cuente en el score. 0.0 = original (sin condicion de volumen).
      use_vwap       (bool,  default False): activar criterio VWAP (L7/S7).
      vwap_tolerance (float, default 0.0):  % de tolerancia bajo el VWAP para LONG
                     y sobre el VWAP para SHORT. 0.005 = 0.5%.
    """
    p              = signal_params or {}
    c3_vol_min     = float(p.get("c3_vol_min",     1.2))
    use_vwap       = bool( p.get("use_vwap",       False))
    vwap_tolerance = float(p.get("vwap_tolerance", 0.0))
    if len(candles) < 30:
        return {
            "signal": "ESPERAR", "score_long": 0, "score_short": 0,
            "indicators": {}, "reason": "datos insuficientes",
            "error": "need_more_bars",
            "patterns": {"bull_patterns": [], "bear_patterns": [],
                         "bull_score": 0, "bear_score": 0},
            "htf_trend": htf_trend,
        }

    closes = [c["c"] for c in candles]
    vols   = [c["v"] for c in candles]
    price  = closes[-1]

    # EMA 8 / 20
    ema8  = (_ema_series(closes, 8)  or [None])[-1]
    ema20 = (_ema_series(closes, 20) or [None])[-1]

    # RSI 14
    rsi = _rsi(closes)

    # MACD (12, 26, 9)
    macd_hist = macd_hist_prev = 0.0
    ema12_s = _ema_series(closes, 12)
    ema26_s = _ema_series(closes, 26)
    if ema12_s and ema26_s:
        offset    = 26 - 12
        macd_line = [ema12_s[i + offset] - ema26_s[i] for i in range(len(ema26_s))]
        sig_s     = _ema_series(macd_line, 9)
        if len(sig_s) >= 2:
            macd_hist      = macd_line[-1] - sig_s[-1]
            macd_hist_prev = macd_line[-2] - sig_s[-2]
        elif len(sig_s) == 1:
            macd_hist = macd_hist_prev = macd_line[-1] - sig_s[-1]

    # Bollinger Bands (20, 2σ)
    bb_upper = bb_lower = bb_mid = None
    if len(closes) >= 20:
        win    = closes[-20:]
        bb_mid = sum(win) / 20
        std    = (sum((x - bb_mid) ** 2 for x in win) / 20) ** 0.5
        bb_upper = bb_mid + 2 * std
        bb_lower = bb_mid - 2 * std

    # ATR 14
    atr = _atr(candles) or 0.0

    # Volume ratio (last bar vs avg of previous 20)
    vol_ratio = 1.0
    if len(vols) >= 21:
        avg_v = sum(vols[-21:-1]) / 20
        if avg_v > 0:
            vol_ratio = vols[-1] / avg_v

    # Swing support / resistance
    soportes, resistencias = _find_support_resistance(candles)
    support    = (max((s for s in soportes if s < price), default=None)
                  or min(c["l"] for c in candles[-20:]))
    resistance = (min((r for r in resistencias if r > price), default=None)
                  or max(c["h"] for c in candles[-20:]))

    # Candlestick patterns (replaces L5/S5)
    patterns    = _detect_candle_patterns(candles)
    bull_pscore = patterns["bull_score"]   # 0-4
    bear_pscore = patterns["bear_score"]   # 0-4

    # ── Binary criteria ────────────────────────────────────────────────────────
    L1 = bb_lower is not None and price <= bb_lower * 1.003
    L2 = rsi < 35
    # C3 raw (MACD improving from negative)
    L3_raw = macd_hist < 0 and macd_hist > macd_hist_prev
    L4 = atr > 0 and price <= support + 0.5 * atr
    L6 = vol_ratio > 1.2
    # C3 effective: condicionado a volumen si c3_vol_min > 0 (C3+C6 refinement)
    # DNA: C3 solo → WR −9pp. C3 con vol_ratio >= c3_vol_min → WR 59%, PF 1.64
    L3 = L3_raw if c3_vol_min == 0 else (L3_raw and vol_ratio >= c3_vol_min)

    S1 = bb_upper is not None and price >= bb_upper * 0.997
    S2 = rsi > 65
    S3_raw = macd_hist > 0 and macd_hist < macd_hist_prev
    S4 = atr > 0 and price >= resistance - 0.5 * atr
    S6 = vol_ratio > 1.2
    S3 = S3_raw if c3_vol_min == 0 else (S3_raw and vol_ratio >= c3_vol_min)

    # ── VWAP (L7/S7) ──────────────────────────────────────────────────────────
    # L7: precio >= VWAP × (1 − tolerance)  → flujo institucional alcista
    # S7: precio <= VWAP × (1 + tolerance)  → flujo institucional bajista
    vwap   = _compute_vwap(candles) if use_vwap else None
    L7     = bool(use_vwap and vwap and price >= vwap * (1.0 - vwap_tolerance))
    S7     = bool(use_vwap and vwap and price <= vwap * (1.0 + vwap_tolerance))

    # ── Momentum context filter ────────────────────────────────────────────────
    # If price moved strongly in one direction in last 10 bars,
    # downweight weak opposite patterns to avoid false reversals.
    momentum_adj_bull = 0
    momentum_adj_bear = 0
    if len(closes) >= 10:
        move_10 = (closes[-1] - closes[-10]) / closes[-10] * 100 if closes[-10] else 0
        if move_10 > 0.5:    # strong uptrend — penalize weak bear patterns
            momentum_adj_bear = min(2, bear_pscore)
        elif move_10 < -0.5: # strong downtrend — penalize weak bull patterns
            momentum_adj_bull = min(2, bull_pscore)

    # Cap pattern score at 2pts for entry scoring (DNA analysis 2026-05-28:
    # strong patterns score 3-4 → PF 0.98 net-negative vs weak 1-2 → PF 1.66)
    bull_pscore_entry = min(bull_pscore, 2)
    bear_pscore_entry = min(bear_pscore, 2)

    score_long  = sum([L1, L2, L3, L4, L6, L7]) + max(0, bull_pscore_entry - momentum_adj_bull)
    score_short = sum([S1, S2, S3, S4, S6, S7]) + max(0, bear_pscore_entry - momentum_adj_bear)

    # Binary criteria count (without patterns) for quality gate
    n_binary_long  = sum([L1, L2, L3, L4, L6, L7])
    n_binary_short = sum([S1, S2, S3, S4, S6, S7])

    # ── Decision ──────────────────────────────────────────────────────────────
    if position_state == "SIN_POSICION":
        # HTF trend adjusts thresholds: aligned = easier entry, opposed = very hard.
        # Note: DNA showed BULLISH/LONG PF 1.03 vs NEUTRAL PF 2.39, but removing
        # the alignment discount collapsed trade count by 90% — too aggressive combined
        # with n_binary >= 2. Keeping original thresholds; n_binary filter is enough.
        if htf_trend == "BULLISH":
            long_thresh, short_thresh = 4, 8
        elif htf_trend == "BEARISH":
            long_thresh, short_thresh = 8, 4
        else:  # NEUTRAL
            long_thresh, short_thresh = 5, 5

        # Require at least 2 binary criteria active (DNA 2026-05-28: n=1 → PF 0.64).
        # Prevents pattern score alone driving an entry without technical confirmation.
        can_long  = score_long  >= long_thresh  and n_binary_long  >= 2
        can_short = score_short >= short_thresh and n_binary_short >= 2

        if   can_long:  signal = "ENTRAR_LONG"
        elif can_short: signal = "ENTRAR_SHORT"
        else:           signal = "ESPERAR"

    elif position_state == "LONG":
        # RSI extreme override: overbought while long → exit immediately
        if rsi > 70:
            signal = "CERRAR_LONG"
        elif score_short >= 3: signal = "CERRAR_LONG"   # was 4
        elif score_short >= 2: signal = "PRECAUCION"
        else:                  signal = "MANTENER"

    elif position_state == "SHORT":
        # RSI extreme override: oversold while short → exit immediately
        if rsi < 30:
            signal = "CERRAR_SHORT"
        elif score_long >= 3: signal = "CERRAR_SHORT"   # was 4
        elif score_long >= 2: signal = "PRECAUCION"
        else:                 signal = "MANTENER"

    else:
        signal = "ESPERAR"

    # ── BB position label ──────────────────────────────────────────────────────
    if bb_upper and bb_lower:
        if price >= bb_upper * 0.997:   bb_pos = "upper"
        elif price <= bb_lower * 1.003: bb_pos = "lower"
        else:                            bb_pos = "inside"
    else:
        bb_pos = "unknown"

    # ── Human-readable reason ──────────────────────────────────────────────────
    parts = [f"RSI {rsi:.0f}"]
    if bb_pos != "inside":
        parts.append(f"BB {bb_pos}")
    if L3:  parts.append("MACD+vol mejorando" if c3_vol_min > 0 else "MACD mejorando")
    if S3:  parts.append("MACD+vol deteriorando" if c3_vol_min > 0 else "MACD deteriorando")
    if L3_raw and not L3 and c3_vol_min > 0:
        parts.append(f"MACD sin vol (fil.)")   # MACD activo pero filtrado por volumen
    if L4: parts.append("en soporte")
    if S4: parts.append("en resistencia")
    if patterns["bull_patterns"]:
        parts.append(patterns["bull_patterns"][0])
    if patterns["bear_patterns"]:
        parts.append(patterns["bear_patterns"][0])
    if L6 or S6:
        parts.append(f"vol {vol_ratio:.1f}×")
    if L7: parts.append(f"sobre VWAP ${vwap:.2f}" if vwap else "sobre VWAP")
    if S7: parts.append(f"bajo VWAP ${vwap:.2f}" if vwap else "bajo VWAP")
    if htf_trend != "NEUTRAL":
        parts.append(f"HTF {htf_trend}")
    reason = ", ".join(parts)

    return {
        "signal":      signal,
        "score_long":  score_long,
        "score_short": score_short,
        "price":       round(price, 4),
        "reason":      reason,
        "htf_trend":   htf_trend,
        "patterns":    patterns,
        "indicators": {
            "rsi":         round(rsi, 1),
            "macd_hist":   round(macd_hist, 5),
            "bb_position": bb_pos,
            "bb_upper":    round(bb_upper, 4) if bb_upper else None,
            "bb_lower":    round(bb_lower, 4) if bb_lower else None,
            "vol_ratio":   round(vol_ratio, 2),
            "support":     round(support, 2),
            "resistance":  round(resistance, 2),
            "atr":         round(atr, 4) if atr else None,
            "ema8":        round(ema8, 2) if ema8 else None,
            "ema20":       round(ema20, 2) if ema20 else None,
        },
        "criteria": {
            "L1": L1, "L2": L2, "L3": L3, "L4": L4, "L6": L6, "L7": L7,
            "S1": S1, "S2": S2, "S3": S3, "S4": S4, "S6": S6, "S7": S7,
            "L3_raw":             L3_raw,   # MACD sin condicion de volumen
            "S3_raw":             S3_raw,
            "bull_patterns":      patterns["bull_patterns"],
            "bear_patterns":      patterns["bear_patterns"],
            "bull_pscore":        bull_pscore,
            "bear_pscore":        bear_pscore,
            "bull_pscore_entry":  bull_pscore_entry,
            "bear_pscore_entry":  bear_pscore_entry,
            "n_binary_long":      n_binary_long,
            "n_binary_short":     n_binary_short,
            "vwap":               vwap,
            "c3_vol_min_used":    c3_vol_min,
        },
    }


# ── Stop / target ──────────────────────────────────────────────────────────────

def compute_stops_targets(position_state: str, entry_price, candles: list) -> dict:
    """Return stop and targets using swing levels with ATR fallback."""
    empty = {"stop": None, "stop_label": "",
             "t1": None, "t1_label": "",
             "t2": None, "t2_label": ""}
    if not entry_price or position_state not in ("LONG", "SHORT"):
        return empty

    atr = _atr(candles) or 0.0
    soportes, resistencias = _find_support_resistance(candles)

    stop = t1 = t2 = None
    stop_label = t1_label = t2_label = ""

    if position_state == "LONG":
        below = [s for s in soportes if s < entry_price]
        if below:
            stop       = round(max(below) - 0.3 * atr, 2)
            stop_label = "soporte detectado"
        elif atr:
            stop       = round(entry_price - 1.5 * atr, 2)
            stop_label = "1.5×ATR"

        above = [r for r in resistencias if r > entry_price]
        if above:
            t1       = round(min(above), 2)
            t1_label = "resistencia detectada"
        elif atr:
            t1       = round(entry_price + 2.0 * atr, 2)
            t1_label = "2.0×ATR"

        if len(above) >= 2:
            t2       = round(sorted(above)[1], 2)
            t2_label = "resistencia detectada"
        elif atr:
            t2       = round(entry_price + 3.5 * atr, 2)
            t2_label = "3.5×ATR"

    else:  # SHORT
        above = [r for r in resistencias if r > entry_price]
        if above:
            stop       = round(min(above) + 0.3 * atr, 2)
            stop_label = "resistencia detectada"
        elif atr:
            stop       = round(entry_price + 1.5 * atr, 2)
            stop_label = "1.5×ATR"

        below = [s for s in soportes if s < entry_price]
        if below:
            t1       = round(max(below), 2)
            t1_label = "soporte detectado"
        elif atr:
            t1       = round(entry_price - 2.0 * atr, 2)
            t1_label = "2.0×ATR"

        if len(below) >= 2:
            t2       = round(sorted(below)[0], 2)
            t2_label = "soporte detectado"
        elif atr:
            t2       = round(entry_price - 3.5 * atr, 2)
            t2_label = "3.5×ATR"

    return {"stop": stop, "stop_label": stop_label,
            "t1": t1, "t1_label": t1_label,
            "t2": t2, "t2_label": t2_label}


# ── P&L ────────────────────────────────────────────────────────────────────────

def compute_pnl(position_state: str, entry_price, current_price, amount=None):
    """Returns (pnl_pct, pnl_usd) or (None, None) if not in a position."""
    if not entry_price or not current_price or position_state == "SIN_POSICION":
        return None, None
    pnl_pct = (current_price - entry_price) / entry_price * 100
    if position_state == "SHORT":
        pnl_pct = -pnl_pct
    pnl_usd = None
    if amount:
        shares  = amount / entry_price
        pnl_usd = shares * (current_price - entry_price)
        if position_state == "SHORT":
            pnl_usd = -pnl_usd
    return round(pnl_pct, 2), (round(pnl_usd, 2) if pnl_usd is not None else None)
