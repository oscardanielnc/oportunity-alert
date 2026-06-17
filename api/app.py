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
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import csv
import io
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from utils.metrics_store import MetricsStore
# Auto-ejecución eToro DESMANTELADA (2026-05-30) — sistema 100% MANUAL.
# Subsistema WATCHER ELIMINADO (2026-05-30): signal_engine / ticker_params_store /
# segment_config → _deprecated/. Motor de scalping net-negativo (audit fees). Ver REBUILD_PLAN §5.

import logging
logger = logging.getLogger(__name__)

# (Tablas de calibracion + helpers de segmento ELIMINADOS con el Watcher 2026-05-30)
LIMA = timezone(timedelta(hours=-5))

app = FastAPI(title="OportunityAlert", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
security = HTTPBasic(auto_error=False)   # auto_error=False evita el prompt nativo del navegador en local

_metrics       = MetricsStore()

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "oscar")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")
HTML_PATH = Path(__file__).parent / "dashboard.html"
PILOT_DASH_PATH = Path(__file__).parent.parent / "data" / "pilot_dashboard.json"


CAPITAL_CACHE_MAX_MIN = 60   # antiguedad maxima del cache de eToro para confiar en el cash real


def _capital_info() -> dict:
    """
    {"value": float, "source": str}. Capital disponible para evaluar trades.

    Prioriza el cash REAL de eToro, leido del cache que escribe el PositionTracker
    cada 10 min (sin llamar a la API en cada request). Si el cache no existe o esta
    viejo (>60 min, p.ej. el tracker no esta corriendo), cae al valor fijo de env.
    """
    try:
        from utils.position_strategy import read_account_cache, account_cache_age_minutes
        cache = read_account_cache()
        age   = account_cache_age_minutes()
        cash  = cache.get("available_cash")
        if cash is not None and age is not None and age <= CAPITAL_CACHE_MAX_MIN:
            return {"value": float(cash), "source": "etoro (cache)"}
    except Exception:
        pass
    return {"value": float(os.environ.get("TRADING_CAPITAL", "5000")),
            "source": "env:TRADING_CAPITAL"}


def _get_available_capital() -> float:
    return _capital_info()["value"]


# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_auth(creds: HTTPBasicCredentials = Depends(security)):
    # Sin password configurada (uso local) → acceso libre, SIN 401/WWW-Authenticate
    # (así el navegador no muestra su prompt nativo y no entra en loop).
    if not DASHBOARD_PASS:
        return creds.username if creds else "local"
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Auth requerida",
            headers={"WWW-Authenticate": "Basic"},
        )
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


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PATH.read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    now = datetime.now(LIMA)
    session = _market_session()
    cap = _capital_info()
    return {
        "ok":                True,
        "ts":                now.strftime("%Y-%m-%dT%H:%M:%S"),
        "market":            "ABIERTO" if _is_market_open() else "CERRADO",
        "market_session":    session,
        "feed_mode":         "IEX" if session == "REGULAR" else "SIP",
        "sip_delay_warning": session != "REGULAR",
        "capital_disponible": cap["value"],
        "capital_source":    cap["source"],
    }


@app.get("/api/alerts")
def get_alerts(
    hours:     int           = Query(24, ge=1, le=168),
    prioridad: Optional[str] = Query(None),
    ticker:    Optional[str] = Query(None),
    _: str = Depends(_require_auth),
):
    return _metrics.get_alerts_recent(hours=hours, prioridad=prioridad, ticker=ticker)


@app.get("/api/positions")
def get_positions(_: str = Depends(_require_auth)):
    rows = _metrics.get_latest_positions()
    # Enriquecer cada posición con estrategia + estado de salida en vivo.
    # El estado de salida (chandelier/Day+7) lo precalcula el PositionTracker y lo
    # deja en account_cache.json → el dashboard NO dispara fetches de Alpaca.
    from utils.position_strategy import resolve_strategy, read_account_cache
    evals = (read_account_cache() or {}).get("evals", {})
    for r in rows:
        # Estrategia SIEMPRE desde resolve_strategy (tag-aware) → el override del dashboard
        # se refleja AL INSTANTE. Antes salía del cache del tracker (10 min viejo), así que
        # un override volvía a "Manual" al refrescar (bug reportado 2026-06-01).
        try:
            res = resolve_strategy(r["ticker"])
            r["strategy"], r["strategy_origin"] = res["strategy"], res["origin"]
        except Exception:
            r["strategy"], r["strategy_origin"] = "manual", "default"
        # El estado de SALIDA (chandelier/Day+7) sí viene del cache del tracker (puede lagear
        # ~10 min tras un override; se recalcula en el próximo ciclo).
        ev = evals.get(r["ticker"])
        if ev:
            r["exit_signal"]     = ev.get("exit", False)
            r["exit_reason"]     = ev.get("reason", "")
            r["exit_detail"]     = ev.get("detail", "")
            r["stop_price"]      = ev.get("stop_price")
            r["days_held"]       = ev.get("days_held")
            r["entry_date"]      = ev.get("entry_date", "")
    snap_ts = rows[0]["ts"] if rows else None
    return {"positions": rows, "snapshot_ts": snap_ts}


@app.post("/api/positions/{ticker}/strategy")
def set_position_strategy(ticker: str, body: dict, _: str = Depends(_require_auth)):
    """Override manual de la estrategia de una posición (modelo híbrido)."""
    from utils.position_strategy import set_tag, clear_tag
    strategy = (body.get("strategy") or "").lower().strip()
    note = body.get("note", "")
    if strategy == "auto":
        clear_tag(ticker)
        return {"ok": True, "ticker": ticker.upper(), "strategy": "auto"}
    if not set_tag(ticker, strategy, note):
        raise HTTPException(status_code=400, detail="Estrategia inválida (marea|ped|manual|auto)")
    return {"ok": True, "ticker": ticker.upper(), "strategy": strategy}


@app.get("/api/pilot")
def get_pilot(_: str = Depends(_require_auth)):
    """Estado del piloto de momentum swing (paper trading)."""
    import json as _json
    if not PILOT_DASH_PATH.exists():
        return {"available": False}
    try:
        d = _json.loads(PILOT_DASH_PATH.read_text(encoding="utf-8"))
        d["available"] = True
        return d
    except Exception as e:
        return {"available": False, "error": str(e)}


@app.get("/api/daily-brief")
def get_daily_brief(
    refresh: bool = Query(False, description="Regenera la narrativa IA (ignora cache del día)"),
    _: str = Depends(_require_auth),
):
    """
    Brief Diario: consolida régimen + centinela macro + sectores + posiciones cerca
    de su chandelier + líderes Marea, y lo redacta con Gemini (cacheado 1 vez/día).
    Decision support — NO da órdenes de compra/venta. Ver utils/daily_brief.py.
    """
    from utils.daily_brief import get_brief
    return get_brief(force_narrative=refresh)


@app.get("/api/equity-history")
def get_equity_history_api(
    days: int = Query(365, ge=1, le=730),
    _: str = Depends(_require_auth),
):
    """Curva de equity REAL de eToro (snapshot diario que escribe el PositionTracker)."""
    from utils.equity_history import get_equity_history
    return get_equity_history(days=days)


@app.get("/api/watchlist")
def get_watchlist(_: str = Depends(_require_auth)):
    # Anota el flag 24/5 desde la única fuente de verdad (LARGE_CAP_24X5 del scanner).
    # Los tickers 24/5 son los tradeables en madrugada; el resto = solo monitoreo.
    from utils.universe import LARGE_CAP_24X5
    rows = _metrics.get_watchlist()
    for r in rows:
        r["tradeable_24x5"] = (r.get("ticker") or "").upper() in LARGE_CAP_24X5
    return rows


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


# (Watcher: thread + endpoints ELIMINADOS 2026-05-30 — signal_engine en _deprecated/)

# ── Log endpoints ─────────────────────────────────────────────────────────────

NOTICIAS_STAGE_LABELS = {
    "keyword_filtered": "Filtro keywords/edad",
    "gate1_blocked":    "Gate1 BLOQUEADO (catalizador priceado)",
    "gates_blocked":    "Gates BLOQUEADO (score<4)",
    "market_closed":    "Mercado cerrado (digest domingo)",
    "ai_baja":          "IA — BAJA prioridad",
    "ai_media":         "IA — MEDIA (sin SMS)",
    "ai_alta":          "IA — ALTA (sin SMS?)",
    "sent":             "SMS ENVIADO",
}

def _rows_to_csv(rows: list, fieldnames: list) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


@app.get("/api/logs/noticias")
def get_noticias_log(
    date: Optional[str] = Query(None),
    _: str = Depends(_require_auth),
):
    d = date or datetime.now(LIMA).strftime("%Y-%m-%d")
    rows = _metrics.get_article_filter_log(date=d)
    # Añadir label legible de cada stage
    for r in rows:
        r["stage_label"] = NOTICIAS_STAGE_LABELS.get(r.get("stage", ""), r.get("stage", ""))
    # Resumen por stage
    from collections import Counter
    summary = Counter(r["stage"] for r in rows)
    return {"date": d, "total": len(rows), "summary": dict(summary), "rows": rows}


@app.get("/api/logs/noticias/download")
def download_noticias_log(
    date: Optional[str] = Query(None),
    _: str = Depends(_require_auth),
):
    d = date or datetime.now(LIMA).strftime("%Y-%m-%d")
    rows = _metrics.get_article_filter_log(date=d)
    for r in rows:
        r["stage_label"] = NOTICIAS_STAGE_LABELS.get(r.get("stage", ""), r.get("stage", ""))
    fields = ["ts", "ticker", "source", "stage", "stage_label", "reason",
              "age_minutes", "keyword_score", "g1", "g2", "g3", "conv",
              "skip_ai", "ai_score", "prioridad", "sms_sent", "event_mode", "title"]
    csv_data = _rows_to_csv(rows, fields)
    filename = f"noticias_log_{d}.csv"
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# (Endpoints /api/logs/premarket ELIMINADOS con el premarket scanner 2026-05-30)


# ── Entry point (usado por el thread en main.py) ──────────────────────────────

def run_dashboard():
    import uvicorn
    port = int(os.environ.get("DASHBOARD_PORT", "8081"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
