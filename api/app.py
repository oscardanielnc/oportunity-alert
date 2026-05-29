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
from utils.ticker_params_store import TickerParamsStore
from utils.segment_config import (
    TIMESTOP_SCANS, TIMESTOP_ADVERSE_PCT,
    SESSION_THRESHOLDS, TICKER_THRESHOLDS,
    TICKER_SEGMENT, SEGMENT_PARAMS, SEG_DEFAULT,
    get_segment_params as _get_seg_params_base,
)
from utils.signal_engine import (
    fetch_bars, fetch_htf_bars, compute_htf_trend,
    fetch_latest_price, compute_signal, compute_stops_targets, compute_pnl,
    DIRECTION_LOCK_SCANS,
)

import logging
logger = logging.getLogger(__name__)

# ── Tablas de calibracion (Level B/C) — deben coincidir con backtest_perticker_v2.py ──
_PATTERN_WEIGHTS_BASE = {
    "Bullish Engulfing":3, "Morning Star":3, "Three White Soldiers":2,
    "Hammer":2, "Piercing Line":2, "Dragonfly Doji":2,
    "Bullish Harami":1, "Inverted Hammer":1,
    "Bearish Engulfing":3, "Evening Star":3, "Three Black Crows":2,
    "Shooting Star":2, "Dark Cloud Cover":2, "Gravestone Doji":2,
    "Bearish Harami":1, "Hanging Man":1,
}
_PATTERN_SCHEME_MAPS = {
    "default":      {3:3, 2:2, 1:1},
    "disabled":     {3:0, 2:0, 1:0},
    "flat":         {3:1, 2:1, 1:1},
    "strong_only":  {3:3, 2:0, 1:0},
    "conservative": {3:2, 2:1, 1:0},
}
_ALL_CRITERIA_MASK = {"bb":True, "rsi":True, "macd":True, "sr":True, "vol":True}

# Macro context filter — QQQ+2 para 5m (backtest 2026-05-28: PF 1.03→1.13, PnL +4.21pp)
# Cuando QQQ (15m) es BEARISH, LONG necesita +2 pts extra. Si BULLISH, SHORT +2 pts.
# Solo activo en 5m. 15m backtest mostro que perjudica → desactivado.
_QQQ_MACRO_CACHE: dict = {"trend": "NEUTRAL", "updated_at": 0.0}
_QQQ_CACHE_TTL_SECS    = 900  # refrescar cada 15 min (igual que HTF 5m)

# ── Ticker segmentation — configs importadas desde utils/segment_config.py ────
# TIMESTOP_SCANS, TIMESTOP_ADVERSE_PCT, SESSION_THRESHOLDS, TICKER_THRESHOLDS,
# TICKER_SEGMENT, SEGMENT_PARAMS, SEG_DEFAULT → ya importados arriba.
# ── Ticker segmentation (backtest 2026-05-28: 2916 simulaciones) ──────────────
# Categorias basadas en comportamiento tecnico observado en backtest:
#   LARGE_CAP: alta liquidez, ATR moderado, patrones fiables → confirm_req=3 en 1m
#   QUANTUM:   volatilidad extrema → 5m NO OPERAR (PF max 0.44), 15m solo LONG
#   AI_SW:     AI/software/social → confirm_req=3 en 5m y 15m, SHORT bloqueado en 5m
TICKER_SEGMENT: dict[str, str] = {
    # LARGE_CAP
    "NVDA": "LARGE_CAP", "AMD":  "LARGE_CAP", "AVGO": "LARGE_CAP",
    "MU":   "LARGE_CAP", "MSFT": "LARGE_CAP", "INTC": "LARGE_CAP",
    "ARM":  "LARGE_CAP", "MRVL": "LARGE_CAP", "ANET": "LARGE_CAP",
    "VRT":  "LARGE_CAP", "SMCI": "LARGE_CAP", "SNDK": "LARGE_CAP",
    "LITE": "LARGE_CAP", "COHR": "LARGE_CAP", "WOLF": "LARGE_CAP",
    # QUANTUM
    "IONQ": "QUANTUM", "QBTS": "QUANTUM",
    "RGTI": "QUANTUM", "QUBT": "QUANTUM",
    # AI_SW (software, social, fintech con alta volatilidad news-driven)
    "PLTR": "AI_SW", "APP":  "AI_SW", "RDDT": "AI_SW",
    "SOUN": "AI_SW", "CRWD": "AI_SW", "SHOP": "AI_SW",
    "BBAI": "AI_SW", "INOD": "AI_SW", "APLD": "AI_SW",
    "HOOD": "AI_SW", "SOFI": "AI_SW",
    # Sin segmento: el resto usa parametros globales por defecto
}

# Parametros optimos por (segmento, tf) — resultado del grid-search
# threshold_adj : ajuste al umbral base de entrada (0=sin cambio, +1=+1pt requerido)
# n_binary_min  : minimo de criterios L1-L6/S1-S6 activos (sin contar patrones)
# pattern_cap   : limite al puntaje de patrones (2=cap actual, 4=sin limite)
# confirm_req   : scans consecutivos para confirmar una entrada (2=estandar, 3=mas selectivo)
# max_scans     : time-stop en scans (None=usar TIMESTOP_SCANS global)
# adverse_pct   : adversidad minima para time-stop (None=usar TIMESTOP_ADVERSE_PCT global)
# block_new     : True=bloquear todas las entradas en este TF (ej: QUANTUM 5m)
# block_short   : True=bloquear ENTRAR_SHORT en este segmento+TF
SEGMENT_PARAMS: dict = {
    # LARGE_CAP — confirm_req=3 en 1m: WR 58%→77%, PF 1.17→7.52
    ("LARGE_CAP", "1m"):  {"threshold_adj":0, "n_binary_min":2, "pattern_cap":4,
                            "confirm_req":3,   "max_scans":15,  "adverse_pct":0.7,
                            "block_new":False, "block_short":False},
    ("LARGE_CAP", "5m"):  {"threshold_adj":0, "n_binary_min":3, "pattern_cap":2,
                            "confirm_req":2,   "max_scans":16,  "adverse_pct":0.5,
                            "block_new":False, "block_short":False},
    ("LARGE_CAP", "15m"): {"threshold_adj":1, "n_binary_min":2, "pattern_cap":4,
                            "confirm_req":2,   "max_scans":3,   "adverse_pct":1.0,
                            "block_new":False, "block_short":True},  # SHORT PF=0.61

    # QUANTUM — 5m NO OPERAR (mejor PF posible: 0.44, estructuralmente no-rentable)
    ("QUANTUM", "1m"):    {"threshold_adj":0, "n_binary_min":1, "pattern_cap":2,
                            "confirm_req":2,   "max_scans":15,  "adverse_pct":0.3,
                            "block_new":False, "block_short":False},
    ("QUANTUM", "5m"):    {"threshold_adj":0, "n_binary_min":2, "pattern_cap":4,
                            "confirm_req":2,   "max_scans":12,  "adverse_pct":0.3,
                            "block_new":True,  "block_short":False},  # BLOQUEAR TODO
    ("QUANTUM", "15m"):   {"threshold_adj":0, "n_binary_min":2, "pattern_cap":4,
                            "confirm_req":3,   "max_scans":4,   "adverse_pct":1.0,
                            "block_new":False, "block_short":True},  # SHORT PF=0.29

    # AI_SW — confirm_req=3 clave: 15m PF 0.59→6.78, 5m PF 2.34→12.13
    ("AI_SW", "1m"):      {"threshold_adj":0, "n_binary_min":3, "pattern_cap":2,
                            "confirm_req":2,   "max_scans":15,  "adverse_pct":0.5,
                            "block_new":False, "block_short":False},
    ("AI_SW", "5m"):      {"threshold_adj":0, "n_binary_min":1, "pattern_cap":2,
                            "confirm_req":3,   "max_scans":8,   "adverse_pct":0.0,
                            "block_new":False, "block_short":True},   # SHORT PF=0.00
    ("AI_SW", "15m"):     {"threshold_adj":0, "n_binary_min":1, "pattern_cap":4,
                            "confirm_req":3,   "max_scans":3,   "adverse_pct":1.0,
                            "block_new":False, "block_short":False},
}

_SEG_DEFAULT = {"threshold_adj":0, "n_binary_min":2, "pattern_cap":2,
                "confirm_req":2,   "max_scans":None, "adverse_pct":None,
                "block_new":False, "block_short":False}


def _get_entry_threshold() -> int:
    """Return the minimum ENTRAR score for the current session + hour window."""
    session = _market_session()
    now_h   = datetime.now(LIMA).hour
    for key, cfg in SESSION_THRESHOLDS.items():
        if key == "default":
            continue
        if cfg.get("session") and cfg["session"] != session:
            continue
        if cfg.get("hour_start", 0) <= now_h < cfg.get("hour_end", 24):
            return cfg["threshold"]
    return SESSION_THRESHOLDS["default"]["threshold"]


def _get_qqq_macro_trend() -> str:
    """Retorna BULLISH/NEUTRAL/BEARISH de QQQ en 15m. Resultado cacheado 15 min."""
    now = _time.time()
    if now - _QQQ_MACRO_CACHE["updated_at"] < _QQQ_CACHE_TTL_SECS:
        return _QQQ_MACRO_CACHE["trend"]
    try:
        bars  = fetch_htf_bars("QQQ", "5m")   # fetch_htf_bars("QQQ","5m") = 15m bars
        trend = compute_htf_trend(bars) if len(bars) >= 20 else "NEUTRAL"
    except Exception as exc:
        logger.warning("[Watcher] QQQ macro fetch error: %s", exc)
        trend = "NEUTRAL"
    _QQQ_MACRO_CACHE["trend"]      = trend
    _QQQ_MACRO_CACHE["updated_at"] = now
    logger.debug("[Watcher] QQQ macro trend actualizado: %s", trend)
    return trend


def _apply_qqq_macro_filter(result: dict, qqq_trend: str, adj: int = 2) -> str:
    """
    Filtra entradas 5m usando el trend macro de QQQ.
    Si QQQ BEARISH: LONG necesita +adj puntos. Si BULLISH: SHORT +adj puntos.
    Salidas y posiciones abiertas no se tocan.
    """
    if qqq_trend == "NEUTRAL":
        return result["signal"]
    score_long  = result["score_long"]
    score_short = result["score_short"]
    htf         = result.get("htf_trend", "NEUTRAL")
    if htf == "BULLISH":
        long_thresh, short_thresh = 4, 8
    elif htf == "BEARISH":
        long_thresh, short_thresh = 8, 4
    else:
        long_thresh, short_thresh = 5, 5
    if qqq_trend == "BEARISH":
        long_thresh  += adj
    elif qqq_trend == "BULLISH":
        short_thresh += adj
    if score_long  >= long_thresh:  return "ENTRAR_LONG"
    if score_short >= short_thresh: return "ENTRAR_SHORT"
    return "ESPERAR"

def _get_segment_params(ticker: str, interval: str) -> dict:
    """Jerarquia: DB calibrado > TICKER_PARAMS dict > SEGMENT_PARAMS > SEG_DEFAULT."""
    db_params = _ticker_params.get_params(ticker, interval)
    if db_params:
        return db_params
    return _get_seg_params_base(ticker, interval)


def _get_confirm_req(ticker: str, interval: str) -> int:
    return _get_segment_params(ticker, interval)["confirm_req"]


def _compute_masked_entry_score(result: dict, is_long: bool, nb: int, pc: int,
                                 mask: dict, scheme_map: dict) -> tuple:
    """
    Re-calcula score de entrada aplicando criteria_mask y pattern_scheme.
    L3/S3 ya vienen condicionados por c3_vol_min desde compute_signal().
    L7/S7 (VWAP) se incluyen sin mask separado (controlados por use_vwap).
    Retorna (score, n_bin) con los valores modificados.
    """
    crit = result.get("criteria", {})
    if is_long:
        n_bin = sum([
            bool(crit.get("L1")) and mask.get("bb",   True),
            bool(crit.get("L2")) and mask.get("rsi",  True),
            bool(crit.get("L3")) and mask.get("macd", True),  # ya es L3 efectivo
            bool(crit.get("L4")) and mask.get("sr",   True),
            bool(crit.get("L6")) and mask.get("vol",  True),
            bool(crit.get("L7")),  # VWAP: sin mask adicional, controlado por use_vwap
        ])
        patterns = crit.get("bull_patterns", [])
    else:
        n_bin = sum([
            bool(crit.get("S1")) and mask.get("bb",   True),
            bool(crit.get("S2")) and mask.get("rsi",  True),
            bool(crit.get("S3")) and mask.get("macd", True),
            bool(crit.get("S4")) and mask.get("sr",   True),
            bool(crit.get("S6")) and mask.get("vol",  True),
            bool(crit.get("S7")),  # VWAP
        ])
        patterns = crit.get("bear_patterns", [])

    raw_pat = min(4, sum(
        scheme_map.get(_PATTERN_WEIGHTS_BASE.get(p, 1), 0) for p in patterns
    ))
    score = n_bin + min(raw_pat, pc)
    return score, n_bin


def _segment_entry_decision(result: dict, ticker: str, interval: str) -> str:
    """
    Re-evalua la decision de entrada con parametros especificos del ticker.
    Aplica: block_new, block_short, threshold_adj, n_binary_min, pattern_cap,
            criteria_mask (Level B) y pattern_scheme (Level C) si estan calibrados.
    Salidas y ESPERAR pasan sin cambio.
    """
    params = _get_segment_params(ticker, interval)

    if params["block_new"]:
        logger.debug("[Watcher] %s/%s BLOCK_NEW", ticker, interval)
        return "ESPERAR"

    sig = result.get("signal", "ESPERAR")
    if sig not in ("ENTRAR_LONG", "ENTRAR_SHORT"):
        return sig

    if params["block_short"] and sig == "ENTRAR_SHORT":
        logger.debug("[Watcher] %s/%s SHORT bloqueado", ticker, interval)
        return "ESPERAR"

    htf     = result.get("htf_trend", "NEUTRAL")
    is_long = (sig == "ENTRAR_LONG")
    th      = params["threshold_adj"]
    nb      = params["n_binary_min"]
    pc      = params["pattern_cap"]

    # Obtener mask y scheme (Level B/C) — None/default = comportamiento original
    mask        = params.get("criteria_mask")  or _ALL_CRITERIA_MASK
    scheme_name = params.get("pattern_scheme") or "default"
    scheme_map  = _PATTERN_SCHEME_MAPS.get(scheme_name, _PATTERN_SCHEME_MAPS["default"])

    score, n_bin = _compute_masked_entry_score(result, is_long, nb, pc, mask, scheme_map)

    if htf == "BULLISH":
        thresh = max(3, 4 + th) if is_long else min(9, 8 + th)
    elif htf == "BEARISH":
        thresh = min(9, 8 + th) if is_long else max(3, 4 + th)
    else:
        thresh = max(3, 5 + th)

    if score >= thresh and n_bin >= nb:
        return sig
    return "ESPERAR"


LIMA = timezone(timedelta(hours=-5))

app = FastAPI(title="OportunityAlert", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
security = HTTPBasic()

_metrics       = MetricsStore()
_ticker_params = TickerParamsStore()

# ── Watcher state ─────────────────────────────────────────────────────────────
_watchers: dict = {}
_watchers_lock  = threading.Lock()
MAX_WATCHERS    = 5
INTERVAL_SECS   = {"1m": 60, "5m": 300, "15m": 900}
LOG_MAX         = 50

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "oscar")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")
HTML_PATH = Path(__file__).parent / "dashboard.html"


def _get_available_capital() -> float:
    """
    Retorna el capital disponible para evaluar rentabilidad de trades.

    AHORA: valor fijo leido de env TRADING_CAPITAL (default $5,000).

    FUTURO (sistema automatico eToro):
        from utils.etoro_client import get_portfolio
        try:
            p = get_portfolio()
            return float(p.get("credit", 5000))
        except Exception:
            return float(os.environ.get("TRADING_CAPITAL", "5000"))

    Este es el UNICO lugar donde cambiar cuando se active la automatizacion.
    El resto del sistema (watcher, signal evaluation, frontend) ya lo consume
    correctamente a traves de /api/health y watcher/status.
    """
    return float(os.environ.get("TRADING_CAPITAL", "5000"))


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
        "ok":                True,
        "ts":                now.strftime("%Y-%m-%dT%H:%M:%S"),
        "market":            "ABIERTO" if _is_market_open() else "CERRADO",
        "market_session":    session,
        "next_premarket":    _next_premarket(),
        "feed_mode":         "IEX" if session == "REGULAR" else "SIP",
        "sip_delay_warning": session != "REGULAR",
        "entry_threshold":   threshold,
        "default_threshold": SESSION_THRESHOLDS["default"]["threshold"],
        "capital_disponible": _get_available_capital(),
        "capital_source":    "env:TRADING_CAPITAL",  # cambiara a "etoro:live" cuando sea automatico
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
        # Build signal_params from calibration (c3_vol_min, use_vwap, vwap_tolerance)
        cal_p         = _get_segment_params(ticker, interval)
        signal_params = {
            "c3_vol_min":     cal_p.get("c3_vol_min",     1.2),
            "use_vwap":       bool(cal_p.get("use_vwap",  False)),
            "vwap_tolerance": cal_p.get("vwap_tolerance", 0.0),
        }
        result      = compute_signal(candles, pos_state, entry_px, htf_trend, signal_params)
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
                    _sp = _get_segment_params(ticker, interval)
                    max_scans   = (_sp["max_scans"]   if _sp["max_scans"]   is not None
                                   else TIMESTOP_SCANS.get(interval, 20))
                    adverse_pct = (_sp["adverse_pct"] if _sp["adverse_pct"] is not None
                                   else TIMESTOP_ADVERSE_PCT.get(interval, 0.5))
                    if scans_held >= max_scans and adverse >= adverse_pct:
                        signal = "CERRAR_LONG" if pos_state == "LONG" else "CERRAR_SHORT"
                        time_stop_fired = True
                        logger.warning("[Watcher] TIME-STOP %s %s — %d scans, adverso %.2f%%",
                                       ticker, pos_state, scans_held, adverse)
                else:
                    w["position_scans_held"]   = 0
                    w["position_adverse_pct"]  = None

        # ── Segment entry decision (category-specific params) ────────────────
        # Aplica: block_new, block_short, threshold_adj, n_binary_min, pattern_cap
        if not time_stop_fired and pos_state == "SIN_POSICION" and signal in ("ENTRAR_LONG", "ENTRAR_SHORT"):
            signal = _segment_entry_decision(result, ticker, interval)

        # ── Session-aware entry threshold filter ──────────────────────────────
        if not time_stop_fired and signal in ("ENTRAR_LONG", "ENTRAR_SHORT"):
            entry_thresh = _get_entry_threshold()
            sl = result.get("score_long", 0)
            ss = result.get("score_short", 0)
            if signal == "ENTRAR_LONG"  and sl < entry_thresh:
                signal = "ESPERAR"
            elif signal == "ENTRAR_SHORT" and ss < entry_thresh:
                signal = "ESPERAR"

        # ── Ticker-specific threshold (DNA analysis 2026-05-28) ───────────────
        if not time_stop_fired and signal in ("ENTRAR_LONG", "ENTRAR_SHORT"):
            ticker_min = TICKER_THRESHOLDS.get(ticker)
            if ticker_min is not None:
                sc = (result.get("score_long", 0) if signal == "ENTRAR_LONG"
                      else result.get("score_short", 0))
                if sc < ticker_min:
                    logger.debug("[Watcher] %s ticker-threshold %d blocks %s (score=%d)",
                                 ticker, ticker_min, signal, sc)
                    signal = "ESPERAR"

        # ── Macro QQQ+2 filter (solo 5m, backtest 2026-05-28) ─────────────────
        # QQQ BEARISH → LONG necesita +2pts. QQQ BULLISH → SHORT +2pts.
        if not time_stop_fired and interval == "5m" and signal in ("ENTRAR_LONG", "ENTRAR_SHORT"):
            qqq_trend = _get_qqq_macro_trend()
            if qqq_trend != "NEUTRAL":
                new_sig = _apply_qqq_macro_filter(result, qqq_trend, adj=2)
                if new_sig != signal:
                    logger.debug("[Watcher] QQQ macro %s bloqueó %s en %s",
                                 qqq_trend, signal, ticker)
                    signal = new_sig

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
                scans_n   = w.get("position_scans_held", 0)
                adverse_n = w.get("position_adverse_pct", 0)
                _sp2 = _get_segment_params(ticker, interval)
                max_scans   = (_sp2["max_scans"]   if _sp2["max_scans"]   is not None
                               else TIMESTOP_SCANS.get(interval, 20))
                adverse_pct = (_sp2["adverse_pct"] if _sp2["adverse_pct"] is not None
                               else TIMESTOP_ADVERSE_PCT.get(interval, 0.5))
                w["log"].insert(0, {
                    "time":      now_str,
                    "signal":    signal,
                    "confirmed": True,
                    "reason":    (f"TIME-STOP: {scans_n}/{max_scans} scans, "
                                  f"adverso {adverse_n:.2f}% >= {adverse_pct}%"),
                    "time_stop": True,
                })
                if len(w["log"]) > LOG_MAX:
                    w["log"] = w["log"][:LOG_MAX]
                w["direction_lock_dir"]       = None
                w["direction_lock_remaining"] = 0
                w["position_scans_held"]      = 0

            elif signal == candidate:
                count += 1
                if count >= _get_confirm_req(ticker, interval):
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

            # ── Profitability check at current capital ─────────────────────────
            # Usa fee_analysis cacheado en el estado del watcher (cargado en start).
            # Hoy = TRADING_CAPITAL env var.
            # Futuro: _get_available_capital() leerá eToro en vivo.
            signal_profitable = True
            if signal in ("ENTRAR_LONG", "ENTRAR_SHORT"):
                capital  = _get_available_capital()
                fa_cache = w.get("_fee_analysis_cache", {})
                fa       = fa_cache.get(interval, {})
                if signal == "ENTRAR_LONG" and "long" in fa:
                    gross   = fa["long"].get("gross_avg_pnl", 0)
                    net_usd = gross / 100 * capital - 2.0
                    signal_profitable = net_usd > 0
                elif signal == "ENTRAR_SHORT" and "short" in fa:
                    gross   = fa["short"].get("gross_avg_pnl", 0)
                    net_pct = gross - 0.30
                    signal_profitable = net_pct > 0

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
            w["signal_profitable"] = signal_profitable
            w["capital_checked"]   = _get_available_capital()

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
    if not _ticker_params.is_calibrated(ticker):
        raise HTTPException(400,
            f"{ticker} no calibrado — corre: python backtest_perticker_v2.py {ticker}")

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
            "signal_profitable":        True,
            "capital_checked":          _get_available_capital(),
            # Fee analysis cacheado en start — evita DB read en cada scan.
            # Se recarga si el usuario sube una nueva calibracion.
            "_fee_analysis_cache":      _ticker_params.get_fee_analysis(ticker),
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
                "timestop_max_scans":      TIMESTOP_SCANS.get(w["interval"], 20),
                "timestop_adverse_pct":    TIMESTOP_ADVERSE_PCT.get(w["interval"], 0.5),
                "log":                     w["log"][:20],
                "error":                   w.get("error"),
                "signal_profitable":       w.get("signal_profitable", True),
                "capital_checked":         w.get("capital_checked", _get_available_capital()),
            })
    return {"watchers": out}


# ── Ticker calibration endpoints ──────────────────────────────────────────────

@app.post("/api/ticker-params/{ticker}", status_code=201)
def upload_ticker_params(ticker: str, body: dict, _: str = Depends(_require_auth)):
    """
    Recibe el JSON generado por backtest_perticker_v2.py y lo guarda en DB.
    Formato esperado: { "ticker":"NVDA", "tfs": { "1m":{params+stats}, ... } }
    """
    ticker = ticker.upper()
    if "tfs" not in body:
        raise HTTPException(400, "JSON debe contener campo 'tfs'")
    body["ticker"] = ticker
    ok, msg = _ticker_params.save_config(ticker, body)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "ticker": ticker, "message": msg}


@app.get("/api/ticker-params/{ticker}")
def get_ticker_params(ticker: str, _: str = Depends(_require_auth)):
    """Devuelve stats por TF y calibrated_at para el ticker calibrado."""
    ticker = ticker.upper()
    rows   = _ticker_params.get_all_calibrated()
    row    = next((r for r in rows if r["ticker"] == ticker), None)
    if not row:
        raise HTTPException(404, f"{ticker} no calibrado")
    return {
        "ticker":        ticker,
        "calibrated":    True,
        "calibrated_at": row.get("calibrated_at"),
        "tfs":           row.get("tfs", {}),
    }


@app.delete("/api/ticker-params/{ticker}")
def delete_ticker_params(ticker: str, _: str = Depends(_require_auth)):
    """Elimina la calibracion de un ticker de la DB."""
    ticker = ticker.upper()
    ok = _ticker_params.delete_config(ticker)
    if not ok:
        raise HTTPException(404, f"{ticker} no encontrado")
    return {"ok": True, "ticker": ticker}


@app.get("/api/calibrated-tickers")
def get_calibrated_tickers(_: str = Depends(_require_auth)):
    """Lista todos los tickers calibrados con sus stats por TF."""
    return {"tickers": _ticker_params.get_all_calibrated()}


# ── Entry point (usado por el thread en main.py) ──────────────────────────────

def run_dashboard():
    import uvicorn
    port = int(os.environ.get("DASHBOARD_PORT", "8081"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
