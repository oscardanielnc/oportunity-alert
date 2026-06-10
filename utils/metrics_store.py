"""
Métricas del sistema — SQLite dedicado (data/metrics.db).
Capa de datos pura: sin lógica de negocio, solo persistencia.

Tablas:
  alerts           → cada alerta generada por la IA
  gate_events      → cada artículo que llegó a los gates (filtrado o no)
  position_snapshots → snapshots periódicos de eToro para detectar trades
  trades           → trades cerrados detectados de los snapshots
  daily_summary    → resumen diario precalculado
"""
import sqlite3
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

LIMA = timezone(timedelta(hours=-5))
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "metrics.db")


def _conn(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class MetricsStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with _conn(self.db_path) as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id         TEXT UNIQUE,
                    ticker           TEXT NOT NULL,
                    ts               TEXT NOT NULL,
                    date_lima        TEXT NOT NULL,
                    prioridad        TEXT,
                    tipo_catalizador TEXT,
                    direccion        TEXT,
                    pct_estimado     REAL,
                    conviction_score INTEGER,
                    gate1_score      INTEGER,
                    gate2_score      INTEGER,
                    gate3_score      INTEGER,
                    gate2_rsi        REAL,
                    atr14            REAL,
                    entry_price      REAL,
                    stop_code        REAL,
                    target_code      REAL,
                    precio_al_alerta REAL,
                    source           TEXT,
                    ai_engine        TEXT,
                    sms_enviado      INTEGER DEFAULT 0,
                    playbook_matches TEXT,
                    portfolio_can_enter INTEGER,
                    horizonte_tiempo TEXT,
                    resumen_cataliz  TEXT,
                    raw_json         TEXT
                );

                CREATE TABLE IF NOT EXISTS gate_events (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker           TEXT NOT NULL,
                    ts               TEXT NOT NULL,
                    date_lima        TEXT NOT NULL,
                    source           TEXT,
                    passed_keywords  INTEGER DEFAULT 1,
                    gate1_score      INTEGER,
                    gate2_score      INTEGER,
                    gate3_score      INTEGER,
                    conviction_total INTEGER,
                    skip_ai          INTEGER,
                    skip_reason      TEXT,
                    ai_called        INTEGER DEFAULT 0,
                    ai_prioridad     TEXT,
                    article_title    TEXT
                );

                CREATE TABLE IF NOT EXISTS position_snapshots (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          TEXT NOT NULL,
                    ticker      TEXT NOT NULL,
                    direction   TEXT,
                    open_rate   REAL,
                    current_rate REAL,
                    units       REAL,
                    invested    REAL,
                    net_profit  REAL,
                    net_profit_pct REAL,
                    open_datetime TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker               TEXT NOT NULL,
                    direction            TEXT,
                    open_rate            REAL,
                    close_rate           REAL,
                    units                REAL,
                    invested             REAL,
                    net_profit           REAL,
                    net_profit_pct       REAL,
                    open_ts              TEXT,
                    close_ts             TEXT,
                    hold_hours           REAL,
                    matched_alert_id     TEXT,
                    matched_prioridad    TEXT,
                    matched_conviction   INTEGER,
                    matched_pct_estimado REAL,
                    outcome              TEXT,
                    strategy             TEXT DEFAULT '',
                    exit_reason          TEXT DEFAULT '',
                    UNIQUE(ticker, open_rate, open_ts)
                );

                CREATE TABLE IF NOT EXISTS daily_summary (
                    date                  TEXT PRIMARY KEY,
                    articles_to_gates     INTEGER DEFAULT 0,
                    gates_filtered        INTEGER DEFAULT 0,
                    ai_called             INTEGER DEFAULT 0,
                    alerts_alta           INTEGER DEFAULT 0,
                    alerts_media          INTEGER DEFAULT 0,
                    alerts_baja           INTEGER DEFAULT 0,
                    sms_sent              INTEGER DEFAULT 0,
                    trades_closed         INTEGER DEFAULT 0,
                    pnl_usd               REAL DEFAULT 0,
                    wins                  INTEGER DEFAULT 0,
                    losses                INTEGER DEFAULT 0,
                    gate_filter_rate      REAL DEFAULT 0,
                    win_rate              REAL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS premarket_scans (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_date        TEXT NOT NULL,
                    pass_number      INTEGER DEFAULT 1,
                    ticker           TEXT NOT NULL,
                    direction        TEXT,
                    code_score       INTEGER,
                    ai_conviccion    INTEGER,
                    ai_continuacion  TEXT,
                    tipo_catalizador TEXT,
                    resumen_cataliz  TEXT,
                    entry_style      TEXT,
                    change_pct       REAL,
                    rvol             REAL,
                    total_vol        INTEGER,
                    stop_pct         REAL,
                    target_pct       REAL,
                    prioridad        TEXT,
                    sms_sent         INTEGER DEFAULT 0,
                    timestamp        TEXT
                );

                CREATE TABLE IF NOT EXISTS watchlist (
                    ticker      TEXT PRIMARY KEY,
                    category    TEXT DEFAULT 'extended',
                    added_at    TEXT NOT NULL,
                    notes       TEXT DEFAULT '',
                    active      INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS article_filter_log (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts            TEXT NOT NULL,
                    date_lima     TEXT NOT NULL,
                    ticker        TEXT NOT NULL,
                    source        TEXT,
                    title         TEXT,
                    age_minutes   REAL,
                    stage         TEXT NOT NULL,
                    reason        TEXT,
                    keyword_score INTEGER,
                    g1            INTEGER,
                    g2            INTEGER,
                    g3            INTEGER,
                    conv          INTEGER,
                    skip_ai       INTEGER DEFAULT 0,
                    ai_score      INTEGER,
                    prioridad     TEXT,
                    sms_sent      INTEGER DEFAULT 0,
                    event_mode    INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS premarket_filter_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          TEXT NOT NULL,
                    date_lima   TEXT NOT NULL,
                    ticker      TEXT NOT NULL,
                    direction   TEXT DEFAULT 'N/A',
                    stage       TEXT NOT NULL,
                    reason      TEXT,
                    change_pct  REAL,
                    total_vol   INTEGER,
                    rvol        REAL,
                    code_score  INTEGER,
                    consistency REAL,
                    max_adverse REAL,
                    ai_score    INTEGER,
                    prioridad   TEXT,
                    sms_sent    INTEGER DEFAULT 0,
                    pass_number INTEGER DEFAULT 1,
                    data_source TEXT DEFAULT 'alpaca'
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_ticker  ON alerts(ticker);
                CREATE INDEX IF NOT EXISTS idx_alerts_date    ON alerts(date_lima);
                CREATE INDEX IF NOT EXISTS idx_alerts_prio    ON alerts(prioridad);
                CREATE INDEX IF NOT EXISTS idx_gate_ticker    ON gate_events(ticker);
                CREATE INDEX IF NOT EXISTS idx_gate_date      ON gate_events(date_lima);
                CREATE INDEX IF NOT EXISTS idx_trades_ticker  ON trades(ticker);
                CREATE INDEX IF NOT EXISTS idx_snap_ticker    ON position_snapshots(ticker);
                CREATE INDEX IF NOT EXISTS idx_pm_date        ON premarket_scans(scan_date);
                CREATE INDEX IF NOT EXISTS idx_pm_ticker      ON premarket_scans(ticker);
                CREATE INDEX IF NOT EXISTS idx_afl_date       ON article_filter_log(date_lima);
                CREATE INDEX IF NOT EXISTS idx_afl_ticker     ON article_filter_log(ticker);
                CREATE INDEX IF NOT EXISTS idx_pmfl_date      ON premarket_filter_log(date_lima);
            """)
            c.commit()
            # Migración 2026-06-10 (DBs existentes en la VM): columnas nuevas para que
            # la tabla trades sea AUDITABLE (P&L por estrategia + razón de salida + open real).
            self._add_column_if_missing(c, "position_snapshots", "open_datetime", "TEXT DEFAULT ''")
            self._add_column_if_missing(c, "trades", "strategy", "TEXT DEFAULT ''")
            self._add_column_if_missing(c, "trades", "exit_reason", "TEXT DEFAULT ''")

    @staticmethod
    def _add_column_if_missing(c, table: str, column: str, decl: str):
        try:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                c.commit()
                logger.info(f"[Metrics] Migración: {table}.{column} añadida")
        except Exception as e:
            logger.error(f"[Metrics] Migración {table}.{column}: {e}")

    # ── Alerts ────────────────────────────────────────────────────────────────

    def log_alert(self, result: dict, conviction: dict = None, sms_enviado: bool = False):
        now = datetime.now(LIMA)
        conviction = conviction or {}
        playbook = json.dumps(result.get("playbook_matches", []))
        portfolio = result.get("portfolio_gate", {})

        try:
            with _conn(self.db_path) as c:
                c.execute("""
                    INSERT OR IGNORE INTO alerts (
                        alert_id, ticker, ts, date_lima,
                        prioridad, tipo_catalizador, direccion, pct_estimado,
                        conviction_score, gate1_score, gate2_score, gate3_score,
                        gate2_rsi, atr14, entry_price, stop_code, target_code,
                        precio_al_alerta, source, ai_engine, sms_enviado,
                        playbook_matches, portfolio_can_enter,
                        horizonte_tiempo, resumen_cataliz, raw_json
                    ) VALUES (
                        ?,?,?,?,
                        ?,?,?,?,
                        ?,?,?,?,
                        ?,?,?,?,?,
                        ?,?,?,?,
                        ?,?,
                        ?,?,?
                    )
                """, (
                    result.get("article_id", ""),
                    result.get("ticker", ""),
                    now.strftime("%Y-%m-%dT%H:%M:%S"),
                    now.strftime("%Y-%m-%d"),
                    result.get("prioridad"),
                    result.get("tipo_catalizador"),
                    result.get("direccion"),
                    result.get("pct_estimado"),
                    conviction.get("conviction_score") or result.get("conviction_score"),
                    conviction.get("gate1_score"),
                    conviction.get("gate2_score"),
                    conviction.get("gate3_score"),
                    conviction.get("gate2_rsi"),
                    conviction.get("atr14"),
                    conviction.get("entry_price"),
                    conviction.get("stop_code"),
                    conviction.get("target_code"),
                    result.get("precio_al_alerta"),
                    result.get("source"),
                    result.get("ai_engine"),
                    1 if sms_enviado else 0,
                    playbook,
                    1 if portfolio.get("can_enter", True) else 0,
                    result.get("horizonte_tiempo"),
                    result.get("resumen_cataliz", "")[:300],
                    json.dumps(result, ensure_ascii=False)[:3000],
                ))
                c.commit()
        except Exception as e:
            logger.error(f"[Metrics] Error log_alert: {e}")

    # ── Gate events ───────────────────────────────────────────────────────────

    def log_gate_event(
        self,
        ticker: str,
        source: str,
        conviction: dict,
        ai_called: bool = False,
        ai_prioridad: str = None,
        article_title: str = "",
    ):
        now = datetime.now(LIMA)
        try:
            with _conn(self.db_path) as c:
                c.execute("""
                    INSERT INTO gate_events (
                        ticker, ts, date_lima, source,
                        gate1_score, gate2_score, gate3_score, conviction_total,
                        skip_ai, skip_reason, ai_called, ai_prioridad, article_title
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    ticker,
                    now.strftime("%Y-%m-%dT%H:%M:%S"),
                    now.strftime("%Y-%m-%d"),
                    source,
                    conviction.get("gate1_score"),
                    conviction.get("gate2_score"),
                    conviction.get("gate3_score"),
                    conviction.get("conviction_score"),
                    1 if conviction.get("skip_ai") else 0,
                    conviction.get("reasoning", "")[:200],
                    1 if ai_called else 0,
                    ai_prioridad,
                    article_title[:150],
                ))
                c.commit()
        except Exception as e:
            logger.error(f"[Metrics] Error log_gate_event: {e}")

    # ── Position snapshots ────────────────────────────────────────────────────

    def log_position_snapshot(self, positions: list):
        if not positions:
            return
        now = datetime.now(LIMA).strftime("%Y-%m-%dT%H:%M:%S")
        rows = [
            (
                now,
                p["ticker"],
                p.get("direction"),
                p.get("open_rate"),
                p.get("current_rate"),
                p.get("units"),
                p.get("invested_amount"),
                p.get("net_profit"),
                p.get("net_profit_pct"),
                p.get("open_datetime", ""),
            )
            for p in positions
        ]
        try:
            with _conn(self.db_path) as c:
                c.executemany("""
                    INSERT INTO position_snapshots
                    (ts, ticker, direction, open_rate, current_rate,
                     units, invested, net_profit, net_profit_pct, open_datetime)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, rows)
                c.commit()
        except Exception as e:
            logger.error(f"[Metrics] Error log_position_snapshot: {e}")

    def get_open_tickers_last_snapshot(self) -> dict:
        """Retorna {ticker: {open_rate, ts, ...}} del último snapshot."""
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT ticker, open_rate, current_rate, direction, units,
                       invested, net_profit_pct, ts, open_datetime
                FROM position_snapshots
                WHERE ts = (SELECT MAX(ts) FROM position_snapshots)
            """).fetchall()
        return {r["ticker"]: dict(r) for r in rows}

    # ── Trades (detectados de snapshots) ──────────────────────────────────────

    def record_closed_trade(self, ticker: str, open_snap: dict, close_rate: float, close_ts: str,
                            strategy: str = "", exit_reason: str = ""):
        """Registra un trade cerrado. Busca alerta matching por ticker + timing.

        2026-06-10: open_ts ahora usa el openDateTime REAL de eToro (persistido en el
        snapshot) — antes usaba el ts del ÚLTIMO snapshot (10 min antes del cierre), por
        lo que hold_hours daba ~0.2h siempre y el matching de alertas buscaba en la
        ventana equivocada. + columnas strategy/exit_reason → P&L por estrategia."""
        open_rate   = open_snap.get("open_rate", 0)
        # openDateTime real de eToro > ts del snapshot (fallback para datos viejos)
        open_ts     = open_snap.get("open_datetime") or open_snap.get("ts", "")
        direction   = open_snap.get("direction", "BUY")
        units       = open_snap.get("units", 0)
        invested    = open_snap.get("invested", 0)

        pnl_pct = round((close_rate - open_rate) / open_rate * 100, 2) if open_rate else 0
        if direction == "SELL":
            pnl_pct = -pnl_pct
        net_profit = round(invested * pnl_pct / 100, 2)

        try:
            from datetime import datetime
            # eToro entrega openDateTime con sufijo Z / offset; normalizar para fromisoformat
            _open_iso  = open_ts.replace("Z", "+00:00")
            dt_open  = datetime.fromisoformat(_open_iso)
            dt_close = datetime.fromisoformat(close_ts)
            if dt_open.tzinfo is not None and dt_close.tzinfo is None:
                dt_close = dt_close.replace(tzinfo=LIMA)
            elif dt_open.tzinfo is None and dt_close.tzinfo is not None:
                dt_close = dt_close.replace(tzinfo=None)
            hold_h   = round((dt_close - dt_open).total_seconds() / 3600, 1)
        except Exception:
            hold_h = 0

        # Buscar alerta matching: mismo ticker, dentro de 4h antes de open_ts
        matched_id    = None
        matched_prio  = None
        matched_conv  = None
        matched_pct   = None
        try:
            with _conn(self.db_path) as c:
                alert = c.execute("""
                    SELECT alert_id, prioridad, conviction_score, pct_estimado
                    FROM alerts
                    WHERE ticker = ?
                      AND ts <= ?
                      AND ts >= datetime(?, '-4 hours')
                    ORDER BY ts DESC LIMIT 1
                """, (ticker, open_ts, open_ts)).fetchone()
                if alert:
                    matched_id   = alert["alert_id"]
                    matched_prio = alert["prioridad"]
                    matched_conv = alert["conviction_score"]
                    matched_pct  = alert["pct_estimado"]
        except Exception:
            pass

        outcome = "WIN" if pnl_pct > 0 else "LOSS"

        try:
            with _conn(self.db_path) as c:
                c.execute("""
                    INSERT OR IGNORE INTO trades (
                        ticker, direction, open_rate, close_rate, units,
                        invested, net_profit, net_profit_pct,
                        open_ts, close_ts, hold_hours,
                        matched_alert_id, matched_prioridad,
                        matched_conviction, matched_pct_estimado, outcome,
                        strategy, exit_reason
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    ticker, direction, open_rate, close_rate, units,
                    invested, net_profit, pnl_pct,
                    open_ts, close_ts, hold_h,
                    matched_id, matched_prio, matched_conv, matched_pct, outcome,
                    strategy, exit_reason,
                ))
                c.commit()
                logger.info(
                    f"[Metrics] Trade cerrado: {ticker} {direction} "
                    f"{pnl_pct:+.1f}% ${net_profit:+.0f} | "
                    f"estrategia: {strategy or '?'} | salida: {exit_reason or '?'} | "
                    f"alerta: {matched_prio or 'no matched'}"
                )
        except Exception as e:
            logger.error(f"[Metrics] Error record_closed_trade: {e}")

    def get_pnl_by_strategy(self, days: int = 90) -> list:
        """P&L agregado por estrategia (la pregunta '¿qué brazo gana dinero?')."""
        cutoff = (datetime.now(LIMA) - timedelta(days=days)).strftime("%Y-%m-%d")
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT COALESCE(NULLIF(strategy, ''), 'desconocida') AS strategy,
                       COUNT(*) AS trades,
                       SUM(outcome='WIN') AS wins,
                       ROUND(SUM(net_profit), 2) AS pnl_usd,
                       ROUND(AVG(net_profit_pct), 2) AS avg_pct,
                       ROUND(AVG(hold_hours) / 24.0, 1) AS avg_hold_days
                FROM trades
                WHERE date(close_ts) >= ?
                GROUP BY 1 ORDER BY pnl_usd DESC
            """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    # ── Daily summary ─────────────────────────────────────────────────────────

    def refresh_daily_summary(self, date: str = None):
        """Recalcula el resumen del día desde las tablas base."""
        d = date or datetime.now(LIMA).strftime("%Y-%m-%d")
        try:
            with _conn(self.db_path) as c:
                g = c.execute("""
                    SELECT
                        COUNT(*) total,
                        SUM(skip_ai) filtered,
                        SUM(ai_called) ai_called
                    FROM gate_events WHERE date_lima = ?
                """, (d,)).fetchone()

                a = c.execute("""
                    SELECT
                        SUM(prioridad='ALTA') alta,
                        SUM(prioridad='MEDIA') media,
                        SUM(prioridad='BAJA') baja,
                        SUM(sms_enviado) sms
                    FROM alerts WHERE date_lima = ?
                """, (d,)).fetchone()

                t = c.execute("""
                    SELECT
                        COUNT(*) cnt,
                        SUM(net_profit) pnl,
                        SUM(outcome='WIN') wins,
                        SUM(outcome='LOSS') losses
                    FROM trades WHERE date(close_ts) = ?
                """, (d,)).fetchone()

                total   = g["total"] or 0
                filtered = g["filtered"] or 0
                ai_called = g["ai_called"] or 0
                filter_rate = round(filtered / total, 3) if total else 0

                wins   = t["wins"] or 0
                losses = t["losses"] or 0
                total_trades = wins + losses
                win_rate = round(wins / total_trades, 3) if total_trades else 0

                c.execute("""
                    INSERT OR REPLACE INTO daily_summary (
                        date, articles_to_gates, gates_filtered, ai_called,
                        alerts_alta, alerts_media, alerts_baja, sms_sent,
                        trades_closed, pnl_usd, wins, losses,
                        gate_filter_rate, win_rate
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    d, total, filtered, ai_called,
                    a["alta"] or 0, a["media"] or 0, a["baja"] or 0, a["sms"] or 0,
                    t["cnt"] or 0, t["pnl"] or 0, wins, losses,
                    filter_rate, win_rate,
                ))
                c.commit()
        except Exception as e:
            logger.error(f"[Metrics] Error refresh_daily_summary: {e}")

    # ── Queries para el dashboard ──────────────────────────────────────────────

    def get_accuracy_summary(self, days: int = 30) -> dict:
        """Accuracy de alertas vs trades reales en los últimos N días."""
        cutoff = (datetime.now(LIMA) - timedelta(days=days)).strftime("%Y-%m-%d")
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT
                    t.matched_prioridad  prioridad,
                    t.outcome,
                    COUNT(*)             cnt,
                    AVG(t.net_profit_pct) avg_pct,
                    SUM(t.net_profit)    total_pnl
                FROM trades t
                WHERE date(t.close_ts) >= ?
                  AND t.matched_prioridad IS NOT NULL
                GROUP BY t.matched_prioridad, t.outcome
                ORDER BY t.matched_prioridad, t.outcome
            """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def get_gate_efficiency(self, days: int = 7) -> dict:
        """Tasa de filtrado y ahorro de tokens por día."""
        cutoff = (datetime.now(LIMA) - timedelta(days=days)).strftime("%Y-%m-%d")
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT date_lima,
                    COUNT(*) total,
                    SUM(skip_ai) filtered,
                    SUM(ai_called) ai_called,
                    ROUND(100.0 * SUM(skip_ai) / COUNT(*), 1) filter_pct
                FROM gate_events
                WHERE date_lima >= ?
                GROUP BY date_lima
                ORDER BY date_lima DESC
            """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def get_recent_news_for_ticker(self, ticker: str, minutes: int = 90) -> list:
        """Retorna alertas/gate_events recientes para un ticker (para explicar moves)."""
        cutoff = (datetime.now(LIMA) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT ts, prioridad, direccion, resumen_cataliz, sms_enviado
                FROM alerts
                WHERE ticker = ? AND ts >= ?
                ORDER BY ts DESC LIMIT 3
            """, (ticker, cutoff)).fetchall()
        return [dict(r) for r in rows]

    def get_recent_filtered_headlines(self, ticker: str, minutes: int = 90) -> list:
        """Titulares que el FILTRO descartó recientemente para un ticker (2026-06-10).
        El explain-move solo miraba `alerts` (lo que pasó a IA) → en el crash 06-04/06-10
        decía "sin catalizador identificado" teniendo "Why Is MU Falling" descartado en
        article_filter_log. Titulares filtrados = contexto válido para explicar un move."""
        cutoff = (datetime.now(LIMA) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT ts, title, stage, reason
                FROM article_filter_log
                WHERE ticker = ? AND ts >= ? AND title != ''
                ORDER BY ts DESC LIMIT 3
            """, (ticker, cutoff)).fetchall()
        return [dict(r) for r in rows]

    def cleanup(self, snapshot_days: int = 90, filter_log_days: int = 90,
                gate_events_days: int = 180) -> dict:
        """Purga periódica (2026-06-10): position_snapshots crece 144 filas/día/posición
        y article_filter_log ~300/día — sin purga degradan disco y queries con los meses.
        alerts/trades/daily_summary se conservan SIEMPRE (son el historial auditable).
        Llamar 1 vez/día (lo hace el heartbeat)."""
        deleted = {}
        try:
            with _conn(self.db_path) as c:
                for table, col, days in (
                    ("position_snapshots", "ts", snapshot_days),
                    ("article_filter_log", "ts", filter_log_days),
                    ("gate_events", "ts", gate_events_days),
                ):
                    cutoff = (datetime.now(LIMA) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
                    cur = c.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                    deleted[table] = cur.rowcount
                c.commit()
            if any(deleted.values()):
                logger.info(f"[Metrics] Purga: {deleted}")
        except Exception as e:
            logger.error(f"[Metrics] Error en cleanup: {e}")
        return deleted

    def get_recent_trades(self, limit: int = 20) -> list:
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT ticker, direction, open_rate, close_rate,
                       net_profit_pct, net_profit, hold_hours,
                       matched_prioridad, matched_conviction,
                       outcome, close_ts
                FROM trades
                ORDER BY close_ts DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── Pre-market scans ──────────────────────────────────────────────────────

    def log_premarket_scan(self, scan: dict):
        now = datetime.now(LIMA)
        try:
            with _conn(self.db_path) as c:
                c.execute("""
                    INSERT INTO premarket_scans (
                        scan_date, pass_number, ticker, direction,
                        code_score, ai_conviccion, ai_continuacion,
                        tipo_catalizador, resumen_cataliz, entry_style,
                        change_pct, rvol, total_vol, stop_pct, target_pct,
                        prioridad, sms_sent, timestamp
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    scan.get("scan_date", now.strftime("%Y-%m-%d")),
                    scan.get("pass_number", 1),
                    scan.get("ticker"),
                    scan.get("direction"),
                    scan.get("code_score"),
                    scan.get("ai_conviccion"),
                    scan.get("ai_continuacion"),
                    scan.get("tipo_catalizador"),
                    scan.get("resumen_cataliz", "")[:300],
                    scan.get("entry_style"),
                    scan.get("change_pct"),
                    scan.get("rvol"),
                    scan.get("total_vol"),
                    scan.get("stop_pct"),
                    scan.get("target_pct"),
                    scan.get("prioridad"),
                    1 if scan.get("sms_sent") else 0,
                    now.strftime("%Y-%m-%dT%H:%M:%S"),
                ))
                c.commit()
        except Exception as e:
            logger.error(f"[Metrics] Error log_premarket_scan: {e}")

    def get_premarket_scans(self, date: str = None, limit: int = 100) -> list:
        d = date or datetime.now(LIMA).strftime("%Y-%m-%d")
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT * FROM premarket_scans
                WHERE scan_date = ?
                ORDER BY code_score DESC, ai_conviccion DESC
                LIMIT ?
            """, (d, limit)).fetchall()
        return [dict(r) for r in rows]

    # ── Dashboard queries ─────────────────────────────────────────────────────

    def get_alerts_recent(
        self,
        hours: int = 24,
        prioridad: str = None,
        ticker: str = None,
    ) -> list:
        cutoff = (datetime.now(LIMA) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        filters = ["ts >= ?"]
        params: list = [cutoff]
        if prioridad:
            filters.append("prioridad = ?")
            params.append(prioridad)
        if ticker:
            filters.append("ticker = ?")
            params.append(ticker.upper())
        where = " AND ".join(filters)
        with _conn(self.db_path) as c:
            rows = c.execute(f"""
                SELECT id, alert_id, ticker, ts, date_lima,
                       prioridad, tipo_catalizador, direccion, pct_estimado,
                       conviction_score, entry_price, stop_code, target_code,
                       precio_al_alerta, source, ai_engine, sms_enviado,
                       horizonte_tiempo, resumen_cataliz, raw_json
                FROM alerts
                WHERE {where}
                ORDER BY
                    CASE prioridad
                        WHEN 'ALTA'  THEN 1
                        WHEN 'MEDIA' THEN 2
                        WHEN 'BAJA'  THEN 3
                        ELSE 4
                    END,
                    ts DESC
            """, params).fetchall()

        results = []
        for r in rows:
            row = dict(r)
            raw_str = row.pop("raw_json") or "{}"
            try:
                raw = json.loads(raw_str)
                row["score_ia"]          = raw.get("score_ia", 0)
                row["stop"]              = raw.get("stop", "N/A")
                row["target"]            = raw.get("target", "N/A")
                row["timing_entrada"]    = raw.get("timing_entrada", "N/A")
                row["riesgo"]            = raw.get("riesgo", "")
                row["entrada_rango"]     = raw.get("entrada_rango", "N/A")
                row["salida_fecha"]      = raw.get("salida_fecha", "")
                row["confianza"]         = raw.get("confianza", "")
                row["article_url"]       = raw.get("article_url", "")
                # Lenguaje simple + contexto (v2026-06-01)
                row["titular_simple"]    = raw.get("titular_simple", "")
                row["analisis_simple"]   = raw.get("analisis_simple", "")
                row["contexto_sector"]   = raw.get("contexto_sector", "")
                row["ventana"]           = raw.get("ventana", "")
                row["tipo_alerta"]       = raw.get("tipo_alerta", "OPORTUNIDAD")
            except Exception:
                pass
            results.append(row)
        return results

    def get_latest_positions(self) -> list:
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT ticker, direction, open_rate, current_rate,
                       units, invested, net_profit, net_profit_pct, ts
                FROM position_snapshots
                WHERE ts = (SELECT MAX(ts) FROM position_snapshots)
                ORDER BY net_profit_pct DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def get_daily_summary(self, date: str = None) -> dict:
        d = date or datetime.now(LIMA).strftime("%Y-%m-%d")
        with _conn(self.db_path) as c:
            row = c.execute(
                "SELECT * FROM daily_summary WHERE date = ?", (d,)
            ).fetchone()
        return dict(row) if row else {"date": d}

    # ── Watchlist CRUD ────────────────────────────────────────────────────────

    def get_watchlist(self) -> list:
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT ticker, category, added_at, notes
                FROM watchlist WHERE active = 1
                ORDER BY category, ticker
            """).fetchall()
        return [dict(r) for r in rows]

    def add_ticker(self, ticker: str, category: str = "extended", notes: str = "") -> bool:
        now = datetime.now(LIMA).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with _conn(self.db_path) as c:
                existing = c.execute(
                    "SELECT active FROM watchlist WHERE ticker = ?", (ticker,)
                ).fetchone()
                if existing:
                    if existing["active"]:
                        return False
                    c.execute(
                        "UPDATE watchlist SET active=1, category=?, notes=? WHERE ticker=?",
                        (category, notes, ticker),
                    )
                else:
                    c.execute(
                        "INSERT INTO watchlist (ticker, category, added_at, notes, active) VALUES (?,?,?,?,1)",
                        (ticker, category, now, notes),
                    )
                c.commit()
            return True
        except Exception as e:
            logger.error(f"[Metrics] Error add_ticker: {e}")
            return False

    def remove_ticker(self, ticker: str) -> bool:
        try:
            with _conn(self.db_path) as c:
                cur = c.execute(
                    "UPDATE watchlist SET active=0 WHERE ticker=? AND active=1", (ticker,)
                )
                c.commit()
                return cur.rowcount > 0
        except Exception as e:
            logger.error(f"[Metrics] Error remove_ticker: {e}")
            return False

    def init_watchlist_from_list(self, tickers: list) -> int:
        """Seed watchlist from list if table is empty. Returns number added."""
        with _conn(self.db_path) as c:
            count = c.execute("SELECT COUNT(*) FROM watchlist WHERE active=1").fetchone()[0]
        if count > 0:
            return 0
        added = 0
        primary = {"NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "AMD",
                   "INTC", "MU", "PLTR", "APP", "SMCI", "MSTR", "MARA", "RIOT"}
        for t in tickers:
            cat = "primary" if t in primary else "extended"
            if self.add_ticker(t, category=cat):
                added += 1
        logger.info(f"[Metrics] Watchlist inicializada: {added} tickers")
        return added

    # ── Article filter log ───────────────────────────────────────────────────

    def log_article_filter(self, entry: dict):
        now = datetime.now(LIMA)
        try:
            with _conn(self.db_path) as c:
                c.execute("""
                    INSERT INTO article_filter_log
                    (ts, date_lima, ticker, source, title, age_minutes,
                     stage, reason, keyword_score, g1, g2, g3, conv,
                     skip_ai, ai_score, prioridad, sms_sent, event_mode)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    now.strftime("%Y-%m-%dT%H:%M:%S"),
                    now.strftime("%Y-%m-%d"),
                    entry.get("ticker", ""),
                    entry.get("source", ""),
                    (entry.get("title") or "")[:200],
                    entry.get("age_minutes"),
                    entry.get("stage", ""),
                    (entry.get("reason") or "")[:300],
                    entry.get("keyword_score"),
                    entry.get("g1"), entry.get("g2"), entry.get("g3"),
                    entry.get("conv"),
                    1 if entry.get("skip_ai") else 0,
                    entry.get("ai_score"),
                    entry.get("prioridad"),
                    1 if entry.get("sms_sent") else 0,
                    1 if entry.get("event_mode") else 0,
                ))
                c.commit()
        except Exception as e:
            logger.error(f"[Metrics] Error log_article_filter: {e}")

    def get_article_filter_log(self, date: str = None, limit: int = 2000) -> list:
        d = date or datetime.now(LIMA).strftime("%Y-%m-%d")
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT ts, ticker, source, title, age_minutes,
                       stage, reason, keyword_score, g1, g2, g3, conv,
                       skip_ai, ai_score, prioridad, sms_sent, event_mode
                FROM article_filter_log
                WHERE date_lima = ?
                ORDER BY ts DESC
                LIMIT ?
            """, (d, limit)).fetchall()
        return [dict(r) for r in rows]

    # ── Pre-market filter log ─────────────────────────────────────────────────

    def log_premarket_filter(self, entry: dict):
        now = datetime.now(LIMA)
        try:
            with _conn(self.db_path) as c:
                c.execute("""
                    INSERT INTO premarket_filter_log
                    (ts, date_lima, ticker, direction, stage, reason,
                     change_pct, total_vol, rvol, code_score,
                     consistency, max_adverse, ai_score, prioridad,
                     sms_sent, pass_number, data_source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    now.strftime("%Y-%m-%dT%H:%M:%S"),
                    now.strftime("%Y-%m-%d"),
                    entry.get("ticker", ""),
                    entry.get("direction", "N/A"),
                    entry.get("stage", ""),
                    (entry.get("reason") or "")[:300],
                    entry.get("change_pct"),
                    entry.get("total_vol"),
                    entry.get("rvol"),
                    entry.get("code_score"),
                    entry.get("consistency"),
                    entry.get("max_adverse"),
                    entry.get("ai_score"),
                    entry.get("prioridad"),
                    1 if entry.get("sms_sent") else 0,
                    entry.get("pass_number", 1),
                    entry.get("data_source", "alpaca"),
                ))
                c.commit()
        except Exception as e:
            logger.error(f"[Metrics] Error log_premarket_filter: {e}")

    def get_premarket_filter_log(self, date: str = None, limit: int = 500) -> list:
        d = date or datetime.now(LIMA).strftime("%Y-%m-%d")
        with _conn(self.db_path) as c:
            rows = c.execute("""
                SELECT ts, ticker, direction, stage, reason,
                       change_pct, total_vol, rvol, code_score,
                       consistency, max_adverse, ai_score, prioridad,
                       sms_sent, pass_number, data_source
                FROM premarket_filter_log
                WHERE date_lima = ?
                ORDER BY ts DESC
                LIMIT ?
            """, (d, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_summary_stats(self) -> dict:
        """Stats globales de todo el historial."""
        with _conn(self.db_path) as c:
            alerts = c.execute("""
                SELECT COUNT(*) total,
                    SUM(prioridad='ALTA') alta,
                    SUM(prioridad='MEDIA') media,
                    SUM(sms_enviado) sms
                FROM alerts
            """).fetchone()
            gates = c.execute("""
                SELECT COUNT(*) total,
                    SUM(skip_ai) filtered,
                    ROUND(100.0 * SUM(skip_ai) / MAX(COUNT(*), 1), 1) filter_pct
                FROM gate_events
            """).fetchone()
            trades = c.execute("""
                SELECT COUNT(*) total,
                    SUM(outcome='WIN') wins,
                    SUM(net_profit) total_pnl,
                    AVG(net_profit_pct) avg_pct,
                    MAX(net_profit_pct) best_pct,
                    MIN(net_profit_pct) worst_pct
                FROM trades
            """).fetchone()
        return {
            "alerts": dict(alerts),
            "gates":  dict(gates),
            "trades": dict(trades),
        }
