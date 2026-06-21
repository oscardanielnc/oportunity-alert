"""
utils/scoreboard.py — libro mayor PERMANENTE de outcomes de señales (los 3 brazos).

Cada señal (noticia / trump / earnings / momentum) se registra al dispararse (status=open) y un
resolver horario la mide con la maquinaria de reacción (retorno anormal vs QQQ + MFE/MAE + acierto
direccional), cerrando a +48h. Es la Fase 1 (forward) del NEWS_REACTION_PLAN generalizada: el
empírico de "¿esta señal funcionó?" que vuelve data-driven el SMS y valida cada brazo.

READ-ONLY sobre el mercado (no ejecuta). Precio DUAL: Binance perp (24/7, para overnight/Trump y
tickers con perp líquido) con fallback a Alpaca SIP (horario+extended). Benchmark QQQ (Alpaca).

API:
  ensure_table(db_path)
  record_signal(db_path, arm, ticker, direction, category, score, source, age_min, t0_iso, price_t0)
  resolve_open(db_path)                 # correr cada hora; idempotente desde barras
  summary_by_category(db_path, days)    # agregados para el panel del dashboard
"""
from __future__ import annotations

import os
import sqlite3
import logging
import bisect
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

HORIZON_H = 48                         # cierre a +48h (earnings/Trump driftean lento)
CHECKPOINTS_MIN = [15, 60, 240, 1440, 2880]   # +15m,1h,4h,24h,48h
ALPACA_BASE = "https://data.alpaca.markets"
BENCH = "QQQ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    arm           TEXT NOT NULL,          -- noticias | market_movers | earnings | momentum
    ts            TEXT NOT NULL,          -- t0 (UTC ISO)
    ticker        TEXT NOT NULL,
    direccion     TEXT,                   -- LONG | SHORT
    categoria     TEXT,
    scope         TEXT,                   -- ticker | sector | macro  (macro => medir crudo+índice)
    event_key     TEXT,                   -- agrupa la cesta de tickers de UNA misma declaración
    score         INTEGER,
    source        TEXT,
    age_min       REAL,
    alert_id      INTEGER,                -- FK opcional a alerts.id / evento
    price_t0      REAL,
    venue_precio  TEXT,                   -- alpaca | binance
    abn_15m  REAL, abn_1h REAL, abn_4h REAL, abn_24h REAL, abn_48h REAL,
    abn_final REAL,                       -- retorno (anormal/crudo) AL EXIT real del trade (a horizon_h)
    horizon_h REAL,                       -- horizonte de medición/cierre en horas (default 48; earnings multi-día)
    idx_24h  REAL,                        -- reacción del índice QQQ a 24h (contexto, clave en macro)
    mfe_abn  REAL, mae_abn REAL, time_to_peak_min INTEGER,
    dir_correct  INTEGER,                 -- 1/0/NULL
    status       TEXT DEFAULT 'open',     -- open | closed | no_data
    updated_at   TEXT,
    UNIQUE(arm, ticker, ts)
);
"""


def ensure_table(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    con.execute(SCHEMA)
    # Migración idempotente: añadir columnas nuevas a tablas ya creadas (CREATE IF NOT EXISTS
    # no las agrega). Cada ALTER falla si la columna ya existe -> se ignora.
    for col, decl in (("scope", "TEXT"), ("event_key", "TEXT"), ("idx_24h", "REAL"),
                      ("abn_final", "REAL"), ("horizon_h", "REAL")):
        try:
            con.execute(f"ALTER TABLE signal_outcomes ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    con.commit()
    con.close()


def record_signal(db_path, arm, ticker, direction, category, score, source,
                  age_min, t0_iso, price_t0, alert_id=None, scope="ticker", event_key=None,
                  horizon_h=HORIZON_H) -> None:
    """Inserta una señal al dispararse (status=open). Idempotente por (arm,ticker,ts).
    scope: ticker|sector|macro (macro => el resolver mide CRUDO, no anormal). event_key agrupa
    la cesta de tickers de una misma declaración (market movers uno-a-muchos).
    horizon_h: horas hasta el cierre/medición (default 48; earnings multi-día pasan 120-192h)."""
    try:
        # Normaliza ts a aware-UTC para que TODAS las fuentes guarden el mismo formato
        # (market_movers pasaba naive) y el resolver no mezcle naive/aware.
        try:
            _dt = datetime.fromisoformat(t0_iso)
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=timezone.utc)
            t0_iso = _dt.isoformat()
        except Exception:
            pass
        con = sqlite3.connect(db_path)
        con.execute(SCHEMA)
        con.execute(
            "INSERT OR IGNORE INTO signal_outcomes "
            "(arm,ts,ticker,direccion,categoria,scope,event_key,score,source,age_min,alert_id,price_t0,horizon_h,status,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
            (arm, t0_iso, str(ticker).upper(), direction, category, scope, event_key, score, source,
             age_min, alert_id, price_t0, horizon_h, _now()),
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.debug(f"[scoreboard] record_signal: {e}")


# ── Precio dual (Binance perp 24/7 -> fallback Alpaca SIP) + benchmark QQQ ────────
_perp_markets = None
_bench_cache = {}   # (símbolo) -> (times[epoch], closes)


def _has_perp(ticker):
    global _perp_markets
    if _perp_markets is None:
        try:
            import ccxt
            _perp_markets = ccxt.binanceusdm({"enableRateLimit": True}).load_markets()
        except Exception:
            _perp_markets = {}
    return f"{ticker}/USDT:USDT" in _perp_markets


def _binance_bars(ticker, start, end):
    import ccxt
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    since = int(start.timestamp() * 1000); endms = int(end.timestamp() * 1000); out = []
    while since < endms:
        o = ex.fetch_ohlcv(f"{ticker}/USDT:USDT", "1m", since=since, limit=1000)
        if not o:
            break
        out += o; since = o[-1][0] + 1
        if len(o) < 1000:
            break
    return [(b[0] // 1000, b[4]) for b in out]


def _alpaca_bars(ticker, start, end):
    key = os.environ.get("ALPACA_API_KEY", ""); sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key:
        return []
    # Alpaca SIP free da 403 si el rango toca los últimos ~15 min -> capar end a now-20min.
    cap = datetime.now(timezone.utc) - timedelta(minutes=20)
    if end > cap:
        end = cap
    if start >= end:
        return []
    params = {"timeframe": "1Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "feed": "sip", "limit": 10000, "sort": "asc"}
    out = []
    url = f"{ALPACA_BASE}/v2/stocks/{ticker}/bars"
    h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}
    while True:
        r = requests.get(url, headers=h, params=params, timeout=40)
        if r.status_code != 200:
            break
        d = r.json()
        for b in (d.get("bars") or []):
            t = datetime.strptime(b["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            out.append((int(t.timestamp()), b["c"]))
        if not d.get("next_page_token"):
            break
        params["page_token"] = d["next_page_token"]
    return out


def _series(ticker, start, end, prefer_perp):
    """Devuelve (times, closes) ordenado. Perp si conviene (overnight/perp), si no Alpaca."""
    if prefer_perp and _has_perp(ticker):
        b = _binance_bars(ticker, start, end)
        if b:
            return [x[0] for x in b], [x[1] for x in b], "binance"
    b = _alpaca_bars(ticker, start, end)
    return [x[0] for x in b], [x[1] for x in b], "alpaca"


def _at(series, when):
    times, closes = series
    if not times:
        return None
    i = bisect.bisect_right(times, int(when.timestamp())) - 1
    if i < 0 or int(when.timestamp()) - times[i] > 90 * 60:
        return None
    return closes[i]


def _anchor(series, t0, max_h):
    """Precio de anclaje de la señal: en t0 si hay barra; si no (overnight / mercado cerrado y
    ticker sin perp), la PRIMERA barra después de t0 dentro de la ventana = entrada al próximo
    open (cuando recién se puede operar). Devuelve (t_efectivo, precio)."""
    p0 = _at(series, t0)
    if p0 is not None:
        return t0, p0
    times, closes = series
    if not times:
        return None, None
    end = int(t0.timestamp()) + max_h * 3600
    i = bisect.bisect_left(times, int(t0.timestamp()))
    if i < len(times) and times[i] <= end:
        return datetime.fromtimestamp(times[i], timezone.utc), closes[i]
    return None, None


def _parse_ts(s):
    """Parsea un ts ISO tolerando naive y aware: naive => se asume UTC. Evita el crash
    'can't compare offset-naive and offset-aware datetimes' cuando conviven señales de
    distintas fuentes (market_movers guardaba naive; noticias/earnings, aware)."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def resolve_open(db_path: str) -> int:
    """Mide y cierra señales abiertas cuya ventana de 48h ya pasó (o actualiza las en curso).
    Idempotente desde barras. Devuelve nº de filas actualizadas."""
    ensure_table(db_path)   # garantiza columnas nuevas (horizon_h/abn_final) en DBs preexistentes
    con = sqlite3.connect(db_path); con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    rows = con.execute("SELECT * FROM signal_outcomes WHERE status='open'").fetchall()
    if not rows:
        con.close(); return 0
    # QQQ una vez para toda la tanda
    g_start = min(_parse_ts(r["ts"]) for r in rows) - timedelta(minutes=30)
    g_end = datetime.now(timezone.utc)
    _q = _series(BENCH, g_start, g_end, prefer_perp=False)
    qqq = (_q[0], _q[1])
    n = 0
    for r in rows:
        t0 = _parse_ts(r["ts"])
        H = r["horizon_h"] if (r["horizon_h"] or 0) > 0 else HORIZON_H   # horizonte por-señal
        elapsed_h = (datetime.now(timezone.utc) - t0).total_seconds() / 3600
        prefer_perp = _has_perp(r["ticker"])   # 24/7 para overnight (Trump) y perp líquido
        s = _series(r["ticker"], t0 - timedelta(minutes=30), g_end, prefer_perp)
        t0_eff, p0 = _anchor((s[0], s[1]), t0, H)   # ancla a t0 o al próximo open
        if p0 is None:
            if elapsed_h > H + 6:
                con.execute("UPDATE signal_outcomes SET status='no_data', updated_at=? WHERE id=?",
                            (_now(), r["id"]))
                n += 1
            continue
        macro = (r["scope"] == "macro")   # evento market-wide => medir CRUDO (no anormal vs QQQ)
        upd = {"price_t0": p0, "venue_precio": s[2]}
        for mins, col in zip(CHECKPOINTS_MIN, ["abn_15m", "abn_1h", "abn_4h", "abn_24h", "abn_48h"]):
            upd[col] = _abn((s[0], s[1]), qqq, t0_eff, mins, p0, macro=macro)
        # retorno AL EXIT real del trade (a horizon_h) — la "P&L" de la señal multi-día
        upd["abn_final"] = _abn((s[0], s[1]), qqq, t0_eff, H * 60, p0, macro=macro)
        # reacción del índice (QQQ) a 24h — contexto, y la señal real en macro
        q0i = _at(qqq, t0_eff); q1i = _at(qqq, t0_eff + timedelta(minutes=1440))
        upd["idx_24h"] = round((q1i / q0i - 1) * 100, 3) if (q0i and q1i) else None
        upd["mfe_abn"], upd["mae_abn"], upd["time_to_peak_min"] = _mfe_mae((s[0], s[1]), qqq, t0_eff, p0, macro=macro, horizon_h=H)
        # dir_correct según el retorno AL EXIT si ya cerró; si no, lectura temprana (abn_4h)
        ref = upd.get("abn_final") if upd.get("abn_final") is not None else upd.get("abn_4h")
        d = (r["direccion"] or "").upper()
        upd["dir_correct"] = (None if ref is None or d not in ("LONG", "SHORT")
                              else int((ref > 0) == (d == "LONG")))
        status = "closed" if elapsed_h >= H else "open"
        cols = ", ".join(f"{k}=?" for k in upd) + ", status=?, updated_at=?"
        con.execute(f"UPDATE signal_outcomes SET {cols} WHERE id=?",
                    (*upd.values(), status, _now(), r["id"]))
        n += 1
    con.commit(); con.close()
    logger.info(f"[scoreboard] resueltas/actualizadas {n} señales")
    return n


def _abn(series, qqq, t0, mins, p0, macro=False):
    p1 = _at(series, t0 + timedelta(minutes=mins))
    if p1 is None or not p0:
        return None
    raw = (p1 / p0 - 1) * 100
    if macro:
        return round(raw, 3)   # macro: el movimiento CRUDO es la señal (todo el mercado se mueve)
    q0, q1 = _at(qqq, t0), _at(qqq, t0 + timedelta(minutes=mins))
    if q0 and q1:
        return round(raw - (q1 / q0 - 1) * 100, 3)
    return round(raw, 3)   # overnight sin QQQ: retorno crudo (perp 24/7)


def _mfe_mae(series, qqq, t0, p0, macro=False, horizon_h=HORIZON_H):
    times, closes = series
    q0 = _at(qqq, t0)
    if not times or not p0:
        return None, None, None
    t0s = int(t0.timestamp()); end = t0s + horizon_h * 3600
    i = bisect.bisect_right(times, t0s); mfe = mae = 0.0; tpeak = None
    while i < len(times) and times[i] <= end:
        when = datetime.fromtimestamp(times[i], timezone.utc)
        q = _at(qqq, when)
        if macro or not (q0 and q):
            a = (closes[i] / p0 - 1) * 100          # macro/overnight: excursión CRUDA
        else:
            a = ((closes[i] / p0) - (q / q0)) * 100  # anormal
        if a > mfe:
            mfe, tpeak = a, (times[i] - t0s) // 60
        if a < mae:
            mae = a
        i += 1
    return round(mfe, 3), round(mae, 3), tpeak


def summary_by_category(db_path: str, days: int = 60) -> list[dict]:
    """Agregados por brazo+categoría para el panel: n, win-rate direccional, abn4h/24h, MFE medianos."""
    con = sqlite3.connect(db_path); con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = con.execute("SELECT * FROM signal_outcomes WHERE status='closed' AND ts>=?",
                       (cutoff,)).fetchall()
    con.close()
    from collections import defaultdict
    import statistics as st
    g = defaultdict(list)
    for r in rows:
        g[(r["arm"], r["categoria"] or "?")].append(r)
    out = []
    for (arm, cat), rs in sorted(g.items(), key=lambda x: -len(x[1])):
        hits = [r["dir_correct"] for r in rs if r["dir_correct"] is not None]
        def med(col):
            v = [r[col] for r in rs if r[col] is not None]
            return round(st.median(v), 2) if v else None
        srcs = sorted({r["source"] for r in rs if r["source"]})
        out.append({"arm": arm, "categoria": cat, "n": len(rs),
                    "win_rate": round(100 * sum(hits) / len(hits)) if hits else None,
                    "abn_4h_med": med("abn_4h"), "abn_24h_med": med("abn_24h"),
                    "abn_final_med": med("abn_final"), "mfe_med": med("mfe_abn"), "sources": srcs})
    return out


def _now():
    return datetime.now(timezone.utc).isoformat()
