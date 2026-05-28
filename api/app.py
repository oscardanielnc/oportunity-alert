"""
OportunityAlert — Dashboard API v1.0
FastAPI + HTTP Basic Auth. Puerto 8081 por defecto (8080 = Sentinel).

Variables de entorno:
  DASHBOARD_PORT  (default: 8081)
  DASHBOARD_USER  (default: oscar)
  DASHBOARD_PASS  (requerida para habilitar auth; vacía = sin protección)
"""
import os
import secrets
import threading
from dotenv import load_dotenv
load_dotenv()
import time as _time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from utils.metrics_store import MetricsStore
from utils.signal_engine import (
    fetch_bars, fetch_htf_bars, compute_htf_trend,
    fetch_latest_price, compute_signal, compute_stops_targets, compute_pnl,
    DIRECTION_LOCK_SCANS,
)

import logging
logger = logging.getLogger(__name__)

# Time-stop config (scans in position before auto-CERRAR if adverse >= TIMESTOP_ADVERSE_PCT)
TIMESTOP_SCANS       = {"1m": 30, "5m": 12, "15m": 6}
TIMESTOP_ADVERSE_PCT = 0.5   # % move against position required to trigger

# ── Session-aware entry thresholds ────────────────────────────────────────────
# Minimum score/9 required for ENTRAR_LONG / ENTRAR_SHORT signals.
# Backtest showed early pre-market (03-08h Lima, SIP 15min delay) is noisier:
#   WR 45% vs 62% regular → raise threshold to filter weak signals.
# Edit freely: add/modify entries; "default" applies to all other windows.
SESSION_THRESHOLDS = {
    "pre_market_early": {
        "session":    "PRE_MARKET",   # must match _market_session() output
        "hour_start": 3,              # Lima hour (inclusive)
        "hour_end":   8,              # Lima hour (exclusive)
        "threshold":  6,              # min score/9 for ENTRAR
        "label":      "Pre-market temprano 03-08h",
    },
    "default": {
        "threshold":  5,
        "label":      "Estándar",
    },
}


def _get_entry_threshold() -> int:
    """Return the minimum ENTRAR score for the current session window."""
    session = _market_session()
    now_h   = datetime.now(LIMA).hour
    pm = SESSION_THRESHOLDS["pre_market_early"]
    if session == pm["session"] and pm["hour_start"] <= now_h < pm["hour_end"]:
        return pm["threshold"]
    return SESSION_THRESHOLDS["default"]["threshold"]

LIMA = timezone(timedelta(hours=-5))

app = FastAPI(title="OportunityAlert", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
security = HTTPBasic()

_metrics = MetricsStore()

# ── Watcher state ─────────────────────────────────────────────────────────────
_watchers: dict = {}
_watchers_lock  = threading.Lock()
MAX_WATCHERS    = 5
INTERVAL_SECS   = {"1m": 60, "5m": 300, "15m": 900}
LOG_MAX         = 50

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "oscar")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")
HTML_PATH = Path(__file__).parent / "dashboard.html"


# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_auth(creds: HTTPBasicCredentials = Depends(security)):
    if not DASHBOARD_PASS:
        return creds.username
    ok_user = secrets.compare_digest(creds.username.encode(), DASHBOARD_USER.encode())
    ok_pass = secrets.compare_digest(creds.password.encode(), DASHBOARD_PASS.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_market_open() -> bool:
    now = datetime.now(LIMA)
    wd = now.weekday()
    mins = now.hour * 60 + now.minute
    if wd == 5:
        return False
    if wd == 6 and mins < 19 * 60:
        return False
    if wd == 4 and mins >= 20 * 60:
        return False
    return True


def _is_regular_hours() -> bool:
    return _market_session() == "REGULAR"


def _market_session() -> str:
    """
    Returns the current NYSE market session:
      REGULAR      — 9:30am–4:00pm ET (Mon-Fri)
      PRE_MARKET   — 4:00am–9:30am ET
      AFTER_HOURS  — 4:00pm–8:00pm ET
      CLOSED       — 8:00pm–4:00am ET (and all weekend)
    EDT (Mar-Nov) = UTC-4. EST (Nov-Mar) = UTC-5. Lima = UTC-5 always.
    """
    now_utc = datetime.now(timezone.utc)
    wd = now_utc.weekday()   # 0=Mon … 6=Sun
    if wd >= 5:              # Saturday or Sunday
        return "CLOSED"

    is_edt = 3 <= now_utc.month <= 11
    m = now_utc.hour * 60 + now_utc.minute  # minutes since UTC midnight

    if is_edt:
        # All windows fit within a single UTC calendar day
        if   m <  8 * 60:           return "CLOSED"       # 00:00–08:00 UTC
        elif m < 13 * 60 + 30:      return "PRE_MARKET"   # 08:00–13:30 UTC
        elif m < 20 * 60:           return "REGULAR"      # 13:30–20:00 UTC
        else:                       return "AFTER_HOURS"  # 20:00–24:00 UTC
    else:
        # EST: AH (4pm–8pm ET) spans midnight UTC (21:00 UTC Mon → 01:00 UTC Tue)
        # Monday at 00:00–01:00 UTC = Sunday 7–8pm ET = CLOSED (no Sunday trading)
        if wd == 0 and m < 60:      return "CLOSED"
        if   m <  60:               return "AFTER_HOURS"  # 00:00–01:00 UTC (prev-day AH tail)
        elif m <  9 * 60:           return "CLOSED"       # 01:00–09:00 UTC
        elif m < 14 * 60 + 30:      return "PRE_MARKET"   # 09:00–14:30 UTC
        elif m < 21 * 60:           return "REGULAR"      # 14:30–21:00 UTC
        else:                       return "AFTER_HOURS"  # 21:00–24:00 UTC


def _next_premarket() -> str:
    """Retorna el próximo scan 6am Lima como string legible."""
    now = datetime.now(LIMA)
    target = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now.hour >= 7:
        target = target + timedelta(days=1)
    while target.weekday() >= 5:
        target = target + timedelta(days=1)
    diff = target - now
    hours = int(diff.total_seconds() // 3600)
    mins  = int((diff.total_seconds() % 3600) // 60)
    return f"{hours}h {mins}min" if hours > 0 else f"{mins}min"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PATH.read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    now = datetime.now(LIMA)
    session = _market_session()
    threshold = _get_entry_threshold()
    return {
        "ok":               True,
        "ts":               now.strftime("%Y-%m-%dT%H:%M:%S"),
        "market":           "ABIERTO" if _is_market_open() else "CERRADO",
        "market_session":   session,
        "next_premarket":   _next_premarket(),
        "feed_mode":        "IEX" if session == "REGULAR" else "SIP",
        "sip_delay_warning": session != "REGULAR",
        "entry_threshold":  threshold,
        "default_threshold": SESSION_THRESHOLDS["default"]["threshold"],
    }


@app.get("/api/alerts")
def get_alerts(
    hours:     int           = Query(24, ge=1, le=168),
    prioridad: Optional[str] = Query(None),
    ticker:    Optional[str] = Query(None),
    _: str = Depends(_require_auth),
):
    return _metrics.get_alerts_recent(hours=hours, prioridad=prioridad, ticker=ticker)


@app.get("/api/premarket")
def get_premarket(
    date: Optional[str] = Query(None),
    _: str = Depends(_require_auth),
):
    scan_date = date or datetime.now(LIMA).strftime("%Y-%m-%d")
    return _metrics.get_premarket_scans(date=scan_date)


@app.get("/api/positions")
def get_positions(_: str = Depends(_require_auth)):
    rows = _metrics.get_latest_positions()
    # Añadir ts del snapshot para saber la antigüedad
    snap_ts = rows[0]["ts"] if rows else None
    return {"positions": rows, "snapshot_ts": snap_ts}


@app.get("/api/watchlist")
def get_watchlist(_: str = Depends(_require_auth)):
    return _metrics.get_watchlist()


@app.post("/api/watchlist", status_code=201)
def add_ticker(body: dict, _: str = Depends(_require_auth)):
    ticker   = (body.get("ticker") or "").upper().strip()
    category = body.get("category", "extended")
    notes    = body.get("notes", "")
    if not ticker or len(ticker) > 10 or not ticker.isalpha():
        raise HTTPException(status_code=400, detail="Ticker inválido")
    if category not in ("primary", "extended"):
        category = "extended"
    ok = _metrics.add_ticker(ticker, category=category, notes=notes)
    if not ok:
        raise HTTPException(status_code=409, detail=f"{ticker} ya está en la watchlist")
    return {"ok": True, "ticker": ticker, "category": category}


@app.delete("/api/watchlist/{ticker}")
def remove_ticker(ticker: str, _: str = Depends(_require_auth)):
    ticker = ticker.upper().strip()
    ok = _metrics.remove_ticker(ticker)
    if not ok:
        raise HTTPException(status_code=404, detail=f"{ticker} no encontrado")
    return {"ok": True, "ticker": ticker}


@app.get("/api/stats")
def get_stats(_: str = Depends(_require_auth)):
    today = datetime.now(LIMA).strftime("%Y-%m-%d")
    summary = _metrics.get_daily_summary(today)
    global_s = _metrics.get_summary_stats()
    return {"today": summary, "global": global_s}


# ── Watcher thread ────────────────────────────────────────────────────────────

def _build_log_reason(result: dict, signal: str) -> str:
    """Build a detailed log reason string from indicators and criteria."""
    ind      = result.get("indicators", {})
    criteria = result.get("criteria", {})
    sl       = result.get("score_long", 0)
    ss       = result.get("score_short", 0)

    parts = []

    # Score summary (max 9 pts: 5 binary + 4 pattern)
    if "LONG" in signal:
        parts.append(f"LONG {sl}/9")
    elif "SHORT" in signal:
        parts.append(f"SHORT {ss}/9")
    elif signal == "ESPERAR":
        parts.append(f"L:{sl} S:{ss}/9")
    elif "CERRAR" in signal or "MANTENER" in signal or "PRECAUCION" in signal:
        parts.append(f"L:{sl} S:{ss}/9")

    # RSI
    rsi = ind.get("rsi")
    if rsi is not None:
        parts.append(f"RSI {rsi:.0f}")

    # Bollinger band position
    bb_pos = ind.get("bb_position", "inside")
    if bb_pos == "lower":
        parts.append("BB banda baja")
    elif bb_pos == "upper":
        parts.append("BB banda alta")

    # MACD trend
    if criteria.get("L3"):
        parts.append("MACD mejorando")
    elif criteria.get("S3"):
        parts.append("MACD deteriorando")

    # Support / resistance proximity
    if criteria.get("L4"):
        sup = ind.get("support")
        parts.append(f"en soporte ${sup:.2f}" if sup else "en soporte")
    elif criteria.get("S4"):
        res = ind.get("resistance")
        parts.append(f"en resistencia ${res:.2f}" if res else "en resistencia")

    # Candlestick patterns
    bull_pats = criteria.get("bull_patterns", [])
    bear_pats = criteria.get("bear_patterns", [])
    if bull_pats:
        parts.append(bull_pats[0])
    elif bear_pats:
        parts.append(bear_pats[0])

    # Volume
    vol = ind.get("vol_ratio", 1.0)
    if vol >= 1.2:
        parts.append(f"vol {vol:.1f}×")

    # EMA relationship
    ema8  = ind.get("ema8")
    ema20 = ind.get("ema20")
    price = result.get("price")
    if ema8 and ema20 and price:
        if price > ema8 > ema20:
            parts.append("precio > EMA8 > EMA20")
        elif price < ema8 < ema20:
            parts.append("precio < EMA8 < EMA20")

    # HTF context
    htf = result.get("htf_trend", "NEUTRAL")
    if htf != "NEUTRAL":
        parts.append(f"HTF {htf}")

    return " · ".join(parts) if parts else result.get("reason", "")


def _run_full_scan(ticker: str) -> None:
    """Fetch bars, compute signal, update all watcher state."""
    scan_start = _time.time()

    with _watchers_lock:
        if ticker not in _watchers or not _watchers[ticker]["active"]:
            return
        w              = _watchers[ticker]
        interval       = w["interval"]
        pos_state      = w["position_state"]
        entry_px       = w["entry_price"]
        amount         = w["amount"]
        isec           = INTERVAL_SECS.get(interval, 60)
        lock_remaining = w.get("direction_lock_remaining", 0)
        lock_dir       = w.get("direction_lock_dir")
        w["scanning"]     = True
        w["next_scan_at"] = scan_start + isec

    try:
        candles     = fetch_bars(ticker, interval, limit=100)
        htf_candles = fetch_htf_bars(ticker, interval)
        htf_trend   = compute_htf_trend(htf_candles)
        result      = compute_signal(candles, pos_state, entry_px, htf_trend)
        signal      = result["signal"]
        bar_price   = result.get("price")

        # ── Time-stop check (bypasses confirmation — fires immediately) ────────
        time_stop_fired = False
        with _watchers_lock:
            if ticker in _watchers:
                w = _watchers[ticker]
                if pos_state in ("LONG", "SHORT") and entry_px:
                    scans_held = w.get("position_scans_held", 0) + 1
                    w["position_scans_held"] = scans_held
                    cur_price_ts = result.get("price") or bar_price or entry_px
                    if pos_state == "LONG":
                        adverse = (entry_px - cur_price_ts) / entry_px * 100
                    else:
                        adverse = (cur_price_ts - entry_px) / entry_px * 100
                    w["position_adverse_pct"]  = round(adverse, 2)
                    max_scans = TIMESTOP_SCANS.get(interval, 30)
                    if scans_held >= max_scans and adverse >= TIMESTOP_ADVERSE_PCT:
                        signal = "CERRAR_LONG" if pos_state == "LONG" else "CERRAR_SHORT"
                        time_stop_fired = True
                        logger.warning("[Watcher] TIME-STOP %s %s — %d scans, adverso %.2f%%",
                                       ticker, pos_state, scans_held, adverse)
                else:
                    w["position_scans_held"]   = 0
                    w["position_adverse_pct"]  = None

        # ── Session-aware entry threshold filter ──────────────────────────────
        if not time_stop_fired and signal in ("ENTRAR_LONG", "ENTRAR_SHORT"):
            entry_thresh = _get_entry_threshold()
            sl = result.get("score_long", 0)
            ss = result.get("score_short", 0)
            if signal == "ENTRAR_LONG"  and sl < entry_thresh:
                signal = "ESPERAR"
            elif signal == "ENTRAR_SHORT" and ss < entry_thresh:
                signal = "ESPERAR"

        # ── Apply direction lock (entry only) ─────────────────────────────────
        if not time_stop_fired and lock_remaining > 0 and lock_dir:
            if (lock_dir == "SHORT" and signal == "ENTRAR_SHORT") or \
               (lock_dir == "LONG"  and signal == "ENTRAR_LONG"):
                logger.debug("[Watcher] %s dir-lock blocks %s (%d scans left)",
                             ticker, signal, lock_remaining)
                signal = "ESPERAR"

        # Live price — overwrites bar close with real last trade
        live       = fetch_latest_price(ticker)
        price      = live["price"] if live["price"] else bar_price
        change_pct = live["change_pct"]

        pnl_pct, pnl_usd = compute_pnl(pos_state, entry_px, price, amount)
        st = (compute_stops_targets(pos_state, entry_px, candles)
              if pos_state != "SIN_POSICION" else {})

        now     = _time.time()
        now_str = datetime.now(LIMA).strftime("%H:%M")

        with _watchers_lock:
            if ticker not in _watchers:
                return
            w         = _watchers[ticker]
            candidate = w["candidate_signal"]
            count     = w["candidate_count"]

            # Time-stop bypasses the 2-scan confirmation — fires immediately
            if time_stop_fired:
                w["current_signal"]   = signal
                w["signal_confirmed"] = True
                w["confirmed_at"]     = now
                w["candidate_signal"] = None
                w["candidate_count"]  = 0
                scans_n = w.get("position_scans_held", 0)
                adverse_n = w.get("position_adverse_pct", 0)
                max_scans = TIMESTOP_SCANS.get(interval, 30)
                w["log"].insert(0, {
                    "time":      now_str,
                    "signal":    signal,
                    "confirmed": True,
                    "reason":    (f"TIME-STOP: {scans_n}/{max_scans} scans, "
                                  f"adverso {adverse_n:.2f}% >= {TIMESTOP_ADVERSE_PCT}%"),
                    "time_stop": True,
                })
                if len(w["log"]) > LOG_MAX:
                    w["log"] = w["log"][:LOG_MAX]
                w["direction_lock_dir"]       = None
                w["direction_lock_remaining"] = 0
                w["position_scans_held"]      = 0

            elif signal == candidate:
                count += 1
                if count >= w["confirmation_required"]:
                    # Signal confirmed — only log if it changed from current
                    if signal != w["current_signal"]:
                        w["current_signal"]   = signal
                        w["signal_confirmed"] = True
                        w["confirmed_at"]     = now
                        w["log"].insert(0, {
                            "time":      now_str,
                            "signal":    signal,
                            "confirmed": True,
                            "reason":    _build_log_reason(result, signal),
                        })
                        if len(w["log"]) > LOG_MAX:
                            w["log"] = w["log"][:LOG_MAX]
                        # Set direction lock on ENTRAR signals
                        if signal == "ENTRAR_LONG":
                            w["direction_lock_dir"]       = "SHORT"
                            w["direction_lock_remaining"] = DIRECTION_LOCK_SCANS
                        elif signal == "ENTRAR_SHORT":
                            w["direction_lock_dir"]       = "LONG"
                            w["direction_lock_remaining"] = DIRECTION_LOCK_SCANS
                        elif signal in ("CERRAR_LONG", "CERRAR_SHORT", "ESPERAR"):
                            w["direction_lock_dir"]       = None
                            w["direction_lock_remaining"] = 0
                            w["position_scans_held"]      = 0
                    # same signal repeating → silent, no log entry
                    w["candidate_signal"] = None
                    w["candidate_count"]  = 0
                else:
                    w["candidate_signal"] = signal
                    w["candidate_count"]  = count
            else:
                # New candidate different from previous
                prev_candidate = w["candidate_signal"]
                w["candidate_signal"] = signal
                w["candidate_count"]  = 1
                if signal != w["current_signal"] and signal != prev_candidate:
                    w["log"].insert(0, {
                        "time":      now_str,
                        "signal":    signal,
                        "confirmed": False,
                        "reason":    result.get("reason", ""),
                    })
                    if len(w["log"]) > LOG_MAX:
                        w["log"] = w["log"][:LOG_MAX]

            # Calculate data lag from last candle timestamp
            bar_lag_min = None
            if candles:
                try:
                    from datetime import datetime as _dt
                    last_bar_t = _dt.fromisoformat(
                        candles[-1]["t"].replace("Z", "+00:00")
                    )
                    bar_lag_min = round(
                        (_dt.now(timezone.utc) - last_bar_t).total_seconds() / 60, 1
                    )
                except Exception:
                    pass

            # Decrement lock counter each scan
            if w.get("direction_lock_remaining", 0) > 0:
                w["direction_lock_remaining"] -= 1
                if w["direction_lock_remaining"] == 0:
                    w["direction_lock_dir"] = None

            w["scanning"]          = False
            w["price"]             = price
            w["change_pct"]        = change_pct
            w["score_long"]        = result.get("score_long", 0)
            w["score_short"]       = result.get("score_short", 0)
            w["indicators"]        = result.get("indicators", {})
            w["pnl_pct"]           = pnl_pct
            w["pnl_usd"]           = pnl_usd
            w["stop_suggested"]    = st.get("stop")
            w["stop_label"]        = st.get("stop_label", "")
            w["target1_suggested"] = st.get("t1")
            w["target1_label"]     = st.get("t1_label", "")
            w["target2_suggested"] = st.get("t2")
            w["target2_label"]     = st.get("t2_label", "")
            w["last_scan_at"]      = now
            w["candles"]           = candles[-100:]
            w["bar_lag_min"]       = bar_lag_min
            w["criteria"]          = result.get("criteria", {})
            w["htf_trend"]         = result.get("htf_trend", "NEUTRAL")
            w["patterns_bull"]     = result.get("patterns", {}).get("bull_patterns", [])
            w["patterns_bear"]     = result.get("patterns", {}).get("bear_patterns", [])
            w["error"]             = result.get("error")

    except Exception:
        with _watchers_lock:
            if ticker in _watchers:
                _watchers[ticker]["scanning"] = False
        raise


def _refresh_price_only(ticker: str) -> None:
    """Lightweight price + P&L refresh between full scans."""
    with _watchers_lock:
        if ticker not in _watchers or not _watchers[ticker]["active"]:
            return
        w         = _watchers[ticker]
        pos_state = w["position_state"]
        entry_px  = w["entry_price"]
        amount    = w["amount"]

    live = fetch_latest_price(ticker)
    if live["price"] is None:
        return

    pnl_pct, pnl_usd = compute_pnl(pos_state, entry_px, live["price"], amount)

    with _watchers_lock:
        if ticker not in _watchers:
            return
        _watchers[ticker]["price"]      = live["price"]
        _watchers[ticker]["change_pct"] = live["change_pct"]
        _watchers[ticker]["pnl_pct"]    = pnl_pct
        _watchers[ticker]["pnl_usd"]    = pnl_usd


PRICE_REFRESH_SECS = 10


def _watcher_loop(ticker: str, stop_event: threading.Event):
    import logging as _log
    _logger = _log.getLogger(__name__)

    while not stop_event.is_set():
        try:
            _run_full_scan(ticker)
        except Exception as exc:
            _logger.error("[Watcher] %s scan error: %s", ticker, exc)
            with _watchers_lock:
                if ticker in _watchers:
                    _watchers[ticker]["error"]    = str(exc)
                    _watchers[ticker]["scanning"] = False

        with _watchers_lock:
            if ticker not in _watchers:
                break
            isec = _watchers[ticker]["interval_seconds"]

        # Wait isec seconds total, refreshing price every 10s
        waited = 0
        while waited < isec and not stop_event.is_set():
            sleep_chunk = min(PRICE_REFRESH_SECS, isec - waited)
            stop_event.wait(timeout=sleep_chunk)
            waited += sleep_chunk
            if waited < isec and not stop_event.is_set():
                try:
                    _refresh_price_only(ticker)
                except Exception as exc:
                    _logger.warning("[Watcher] %s price refresh error: %s", ticker, exc)


# ── Watcher endpoints ─────────────────────────────────────────────────────────

@app.post("/api/watcher/start")
def watcher_start(body: dict, _: str = Depends(_require_auth)):
    ticker   = (body.get("ticker") or "").upper().strip()
    interval = body.get("interval", "1m")
    if not ticker:
        raise HTTPException(400, "ticker requerido")
    if interval not in INTERVAL_SECS:
        raise HTTPException(400, "interval debe ser 1m, 5m o 15m")

    with _watchers_lock:
        active = sum(1 for w in _watchers.values() if w["active"])
        if active >= MAX_WATCHERS and ticker not in _watchers:
            raise HTTPException(400, f"Límite de {MAX_WATCHERS} tickers simultáneos")
        if ticker in _watchers and _watchers[ticker]["active"]:
            return {"ok": True, "message": f"Watcher {ticker} ya activo"}

        stop_event = threading.Event()
        _watchers[ticker] = {
            "ticker":               ticker,
            "active":               True,
            "interval":             interval,
            "interval_seconds":     INTERVAL_SECS[interval],
            "position_state":       body.get("position_state", "SIN_POSICION"),
            "entry_price":          body.get("entry_price"),
            "amount":               body.get("amount"),
            "sms_enabled":          bool(body.get("sms_enabled", False)),
            "current_signal":       "ESPERAR",
            "signal_confirmed":     False,
            "confirmed_at":         None,
            "candidate_signal":     None,
            "candidate_count":      0,
            "confirmation_required": 2,
            "scanning":             False,
            "price":                None,
            "change_pct":           None,
            "score_long":           0,
            "score_short":          0,
            "indicators":           {},
            "pnl_pct":              None,
            "pnl_usd":              None,
            "stop_suggested":       None,
            "stop_label":           "",
            "target1_suggested":    None,
            "target1_label":        "",
            "target2_suggested":    None,
            "target2_label":        "",
            "last_scan_at":         None,
            "next_scan_at":         None,
            "candles":                  [],
            "criteria":                 {},
            "htf_trend":                "NEUTRAL",
            "patterns_bull":            [],
            "patterns_bear":            [],
            "direction_lock_remaining": 0,
            "direction_lock_dir":       None,
            "position_scans_held":      0,
            "position_adverse_pct":     None,
            "log":                      [],
            "error":                    None,
            "_stop_event":              stop_event,
        }

    t = threading.Thread(target=_watcher_loop, args=(ticker, stop_event), daemon=True)
    t.start()
    return {"ok": True, "message": f"Watcher {ticker} iniciado"}


@app.post("/api/watcher/stop")
def watcher_stop(body: dict, _: str = Depends(_require_auth)):
    ticker = (body.get("ticker") or "").upper().strip()
    with _watchers_lock:
        if ticker not in _watchers:
            raise HTTPException(404, f"Watcher {ticker} no encontrado")
        w  = _watchers.pop(ticker)
        ev = w.get("_stop_event")
    if ev:
        ev.set()
    return {"ok": True, "message": f"Watcher {ticker} detenido"}


@app.post("/api/watcher/update")
def watcher_update(body: dict, _: str = Depends(_require_auth)):
    ticker = (body.get("ticker") or "").upper().strip()
    with _watchers_lock:
        if ticker not in _watchers:
            raise HTTPException(404, f"Watcher {ticker} no encontrado")
        w = _watchers[ticker]
        for key in ("position_state", "entry_price", "amount", "sms_enabled"):
            if key in body:
                if key == "position_state" and body[key] != w.get("position_state"):
                    w["position_scans_held"]  = 0
                    w["position_adverse_pct"] = None
                w[key] = body[key]
        if "interval" in body and body["interval"] in INTERVAL_SECS:
            new_iv = body["interval"]
            if new_iv != w["interval"]:
                w["interval"]         = new_iv
                w["interval_seconds"] = INTERVAL_SECS[new_iv]
                w["candidate_signal"] = None
                w["candidate_count"]  = 0
                w["log"]              = []
    return {"ok": True, "message": f"Watcher {ticker} actualizado"}


@app.get("/api/watcher/status")
def watcher_status(_: str = Depends(_require_auth)):
    now = _time.time()
    session = _market_session()
    regular = session == "REGULAR"
    out = []
    with _watchers_lock:
        for ticker, w in _watchers.items():
            if not w["active"]:
                continue
            ca   = w.get("confirmed_at")
            ns   = w.get("next_scan_at")
            out.append({
                "ticker":            ticker,
                "active":            True,
                "interval":          w["interval"],
                "interval_seconds":  w["interval_seconds"],
                "price":             w.get("price"),
                "change_pct":        w.get("change_pct"),
                "signal":            w["current_signal"],
                "signal_confirmed":  w["signal_confirmed"],
                "signal_age_seconds": int(now - ca) if ca else None,
                "candidate_signal":  w.get("candidate_signal"),
                "score_long":        w["score_long"],
                "score_short":       w["score_short"],
                "indicators":        w["indicators"],
                "criteria":          w.get("criteria", {}),
                "position_state":    w["position_state"],
                "entry_price":       w["entry_price"],
                "amount":            w["amount"],
                "pnl_pct":           w["pnl_pct"],
                "pnl_usd":           w["pnl_usd"],
                "stop_suggested":    w["stop_suggested"],
                "stop_label":        w.get("stop_label", ""),
                "target1_suggested": w["target1_suggested"],
                "target1_label":     w.get("target1_label", ""),
                "target2_suggested": w["target2_suggested"],
                "target2_label":     w.get("target2_label", ""),
                "sms_enabled":       w["sms_enabled"],
                "scanning":          w.get("scanning", False),
                "next_scan_seconds": max(0, int(ns - now)) if ns else None,
                "candles":           w.get("candles", []),
                "bar_lag_min":              w.get("bar_lag_min"),
                "feed_mode":               "IEX" if regular else "SIP",
                "sip_delay_warning":       not regular,
                "market_session":          session,
                "htf_trend":               w.get("htf_trend", "NEUTRAL"),
                "patterns_bull":           w.get("patterns_bull", []),
                "patterns_bear":           w.get("patterns_bear", []),
                "direction_lock_remaining": w.get("direction_lock_remaining", 0),
                "direction_lock_dir":      w.get("direction_lock_dir"),
                "position_scans_held":     w.get("position_scans_held", 0),
                "position_adverse_pct":    w.get("position_adverse_pct"),
                "timestop_max_scans":      TIMESTOP_SCANS.get(w["interval"], 30),
                "timestop_adverse_pct":    TIMESTOP_ADVERSE_PCT,
                "log":                     w["log"][:20],
                "error":                   w.get("error"),
            })
    return {"watchers": out}


# ── Entry point (usado por el thread en main.py) ──────────────────────────────

def run_dashboard():
    import uvicorn
    port = int(os.environ.get("DASHBOARD_PORT", "8081"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
