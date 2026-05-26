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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from utils.metrics_store import MetricsStore

LIMA = timezone(timedelta(hours=-5))

app = FastAPI(title="OportunityAlert", docs_url=None, redoc_url=None)
security = HTTPBasic()

_metrics = MetricsStore()

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
    return {
        "ok":             True,
        "ts":             now.strftime("%Y-%m-%dT%H:%M:%S"),
        "market":         "ABIERTO" if _is_market_open() else "CERRADO",
        "next_premarket": _next_premarket(),
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


# ── Entry point (usado por el thread en main.py) ──────────────────────────────

def run_dashboard():
    import uvicorn
    port = int(os.environ.get("DASHBOARD_PORT", "8081"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
