"""
Deduplicación de noticias usando SQLite.
Dos niveles:
  1. Por ID de artículo (mismo artículo, misma fuente)
  2. Por fingerprint de evento (mismo evento, distinta fuente — cross-source)
"""
import sqlite3
import hashlib
import os
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


class DedupStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            # WAL mode: permite lecturas concurrentes mientras hay escritura activa
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # Tabla 1: IDs vistos (dedup por artículo individual)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_items (
                    id TEXT PRIMARY KEY,
                    source TEXT,
                    seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Tabla 2: eventos por ticker (dedup cross-source + historial de alertas)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticker_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    event_fingerprint TEXT NOT NULL,
                    source TEXT,
                    direction TEXT,
                    sent_to_claude INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ticker_events_ticker ON ticker_events(ticker)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ticker_events_fp ON ticker_events(event_fingerprint)")

            # Limpiar registros viejos (>7 días)
            conn.execute("DELETE FROM seen_items WHERE seen_at < datetime('now', '-7 days')")
            conn.execute("DELETE FROM ticker_events WHERE created_at < datetime('now', '-7 days')")
            conn.commit()

    # ── Nivel 1: dedup por ID ──────────────────────────────────────────────

    def is_seen(self, item_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_items WHERE id = ?", (item_id,)
            ).fetchone()
        return row is not None

    def mark_seen(self, item_id: str, source: str = ""):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO seen_items (id, source) VALUES (?, ?)",
                    (item_id, source)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error marcando como visto {item_id}: {e}")

    # ── Nivel 2: dedup cross-source por fingerprint ────────────────────────

    def get_event_fingerprint(self, ticker: str, raw_text: str) -> str:
        """
        Genera un fingerprint del evento basado en ticker + keywords críticas presentes.
        Si dos artículos de distintas fuentes tienen el mismo ticker y las mismas
        keywords → mismo evento → fingerprint idéntico.
        """
        from filters.keyword_filter import CRITICAL_KEYWORDS
        found = sorted(kw for kw in CRITICAL_KEYWORDS if kw in raw_text.upper())
        content = f"{ticker}::{':'.join(found)}"
        return hashlib.md5(content.encode()).hexdigest()

    def is_cross_source_duplicate(
        self, ticker: str, fingerprint: str, window_minutes: int = 20
    ) -> bool:
        """
        Retorna True si ya se procesó un evento con el mismo fingerprint
        en los últimos window_minutes minutos (de cualquier fuente).
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT 1 FROM ticker_events
                   WHERE ticker = ? AND event_fingerprint = ?
                   AND created_at > ? AND sent_to_claude = 1""",
                (ticker, fingerprint, cutoff),
            ).fetchone()
        return row is not None

    def mark_ticker_event(
        self,
        ticker: str,
        fingerprint: str,
        source: str = "",
        direction: str = "",
        sent_to_claude: bool = False,
    ):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO ticker_events
                       (ticker, event_fingerprint, source, direction, sent_to_claude)
                       VALUES (?, ?, ?, ?, ?)""",
                    (ticker, fingerprint, source, direction, 1 if sent_to_claude else 0),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error en mark_ticker_event: {e}")

    # ── Historial de alertas por ticker (para pre-check de precio) ─────────

    def get_last_alert_on_ticker(self, ticker: str, hours: int = 4) -> dict | None:
        """
        Retorna el último evento enviado a Claude para este ticker
        en las últimas N horas, o None si no hay ninguno.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT ticker, direction, created_at FROM ticker_events
                   WHERE ticker = ? AND created_at > ? AND sent_to_claude = 1
                   ORDER BY created_at DESC LIMIT 1""",
                (ticker, cutoff),
            ).fetchone()
        if row:
            return {"ticker": row[0], "direction": row[1], "created_at": row[2]}
        return None

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0]
