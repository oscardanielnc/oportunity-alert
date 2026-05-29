"""
Ticker-specific calibrated parameter store.
Stores optimal params per (ticker, interval) validated by backtest_complete.
Used by the watcher to apply per-ticker settings instead of generic segment defaults.

The calibration flow:
  1. Run backtest_perticker_v2.py TICKER  -> generates data/TICKER_config.json
  2. Upload JSON via POST /api/ticker-config/{ticker}  (dashboard frontend)
  3. Watcher reads DB params -> applies them for that ticker
  4. Only calibrated tickers can be used in Watcher
"""
import sqlite3, json, os, logging
from datetime import datetime, timezone, timedelta

logger  = logging.getLogger(__name__)
LIMA    = timezone(timedelta(hours=-5))
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ticker_params.db")
VALID_INTERVALS = ("1m", "5m", "15m")


def _conn(path: str = DB_PATH):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


class TickerParamsStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with _conn(self.db_path) as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS ticker_params (
                    ticker          TEXT NOT NULL,
                    interval        TEXT NOT NULL,
                    threshold_adj   INTEGER DEFAULT 0,
                    n_binary_min    INTEGER DEFAULT 2,
                    pattern_cap     INTEGER DEFAULT 2,
                    confirm_req     INTEGER DEFAULT 2,
                    max_scans       INTEGER,
                    adverse_pct     REAL,
                    block_new       INTEGER DEFAULT 0,
                    block_short     INTEGER DEFAULT 0,
                    criteria_mask   TEXT,
                    pattern_scheme  TEXT,
                    c3_vol_min      REAL    DEFAULT 1.2,
                    use_vwap        INTEGER DEFAULT 0,
                    vwap_tolerance  REAL    DEFAULT 0.0,
                    pf              REAL,
                    wr_pct          REAL,
                    avg_pnl         REAL,
                    n_trades        INTEGER,
                    calibrated_at   TEXT,
                    PRIMARY KEY (ticker, interval)
                );
                CREATE TABLE IF NOT EXISTS calibrated_tickers (
                    ticker           TEXT PRIMARY KEY,
                    calibrated_at    TEXT NOT NULL,
                    tfs_available    TEXT,
                    hourly_analysis  TEXT,
                    fee_analysis     TEXT
                );
            """)
            # Migration: add columns if DB was created before this version
            for col, typedef in [
                ("criteria_mask","TEXT"), ("pattern_scheme","TEXT"),
                ("c3_vol_min","REAL DEFAULT 1.2"),
                ("use_vwap","INTEGER DEFAULT 0"),
                ("vwap_tolerance","REAL DEFAULT 0.0"),
                ("hourly_analysis","TEXT"), ("fee_analysis","TEXT"),
            ]:
                try:
                    c.execute(f"ALTER TABLE ticker_params ADD COLUMN {col} {typedef}")
                except Exception:
                    pass
            c.commit()

    # ── Write ─────────────────────────────────────────────────────────────────

    def save_config(self, ticker: str, config: dict) -> tuple[bool, str]:
        """
        Save calibration from JSON produced by backtest_perticker_v2.py.
        Expected format:
          { "ticker":"NVDA", "tfs": { "1m": {params…, "stats":{pf,wr_pct,avg_pnl,n_trades}}, … } }
        Returns (success, message).
        """
        ticker = ticker.upper()
        now    = datetime.now(LIMA).isoformat()
        tfs    = config.get("tfs", {})
        if not tfs:
            return False, "JSON no contiene 'tfs'"
        saved  = []
        try:
            with _conn(self.db_path) as c:
                for interval, params in tfs.items():
                    if interval not in VALID_INTERVALS:
                        continue
                    st = params.get("stats", {})
                    # criteria_mask stored as JSON string, None = all criteria on
                    cm = params.get("criteria_mask")
                    if isinstance(cm, dict):
                        cm_json = json.dumps(cm)
                    elif cm and cm != "all":
                        cm_json = cm
                    else:
                        cm_json = None  # all on — no storage needed

                    ps = params.get("pattern_scheme")
                    ps_val = ps if (ps and ps != "default") else None

                    c.execute("""
                        INSERT OR REPLACE INTO ticker_params
                        (ticker, interval, threshold_adj, n_binary_min, pattern_cap,
                         confirm_req, max_scans, adverse_pct, block_new, block_short,
                         criteria_mask, pattern_scheme,
                         c3_vol_min, use_vwap, vwap_tolerance,
                         pf, wr_pct, avg_pnl, n_trades, calibrated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        ticker, interval,
                        params.get("threshold_adj", 0),
                        params.get("n_binary_min",  2),
                        params.get("pattern_cap",   2),
                        params.get("confirm_req",   2),
                        params.get("max_scans"),
                        params.get("adverse_pct"),
                        1 if params.get("block_new")   else 0,
                        1 if params.get("block_short") else 0,
                        cm_json, ps_val,
                        params.get("c3_vol_min",     1.2),
                        1 if params.get("use_vwap") else 0,
                        params.get("vwap_tolerance", 0.0),
                        st.get("pf"),  st.get("wr_pct"),
                        st.get("avg_pnl"), st.get("n_trades"),
                        now,
                    ))
                    saved.append(interval)
                if saved:
                    ha = config.get("hourly_analysis")
                    ha_json = json.dumps(ha) if ha else None
                    fa = config.get("fee_analysis")
                    fa_json = json.dumps(fa) if fa else None
                    c.execute("""
                        INSERT OR REPLACE INTO calibrated_tickers
                        (ticker, calibrated_at, tfs_available, hourly_analysis, fee_analysis)
                        VALUES (?,?,?,?,?)
                    """, (ticker, now, json.dumps(sorted(saved)), ha_json, fa_json))
                c.commit()
            return True, f"Calibrado: {ticker} en {', '.join(saved)}"
        except Exception as e:
            logger.error("[TickerParams] save_config %s: %s", ticker, e)
            return False, str(e)

    def delete_config(self, ticker: str) -> bool:
        ticker = ticker.upper()
        try:
            with _conn(self.db_path) as c:
                c.execute("DELETE FROM ticker_params WHERE ticker=?", (ticker,))
                c.execute("DELETE FROM calibrated_tickers WHERE ticker=?", (ticker,))
                c.commit()
            return True
        except Exception as e:
            logger.error("[TickerParams] delete_config %s: %s", ticker, e)
            return False

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_params(self, ticker: str, interval: str) -> dict | None:
        """Returns signal params dict for use by the watcher. None if not calibrated."""
        try:
            with _conn(self.db_path) as c:
                row = c.execute(
                    "SELECT * FROM ticker_params WHERE ticker=? AND interval=?",
                    (ticker.upper(), interval)
                ).fetchone()
            if not row:
                return None
            # criteria_mask: stored as JSON string, None means "all on"
            cm_raw = row["criteria_mask"]
            if cm_raw:
                try:
                    criteria_mask = json.loads(cm_raw)
                except Exception:
                    criteria_mask = None
            else:
                criteria_mask = None

            return {
                "threshold_adj":  row["threshold_adj"],
                "n_binary_min":   row["n_binary_min"],
                "pattern_cap":    row["pattern_cap"],
                "confirm_req":    row["confirm_req"],
                "max_scans":      row["max_scans"],
                "adverse_pct":    row["adverse_pct"],
                "block_new":      bool(row["block_new"]),
                "block_short":    bool(row["block_short"]),
                "criteria_mask":  criteria_mask,
                "pattern_scheme": row["pattern_scheme"] or "default",
                "c3_vol_min":     row["c3_vol_min"]     if row["c3_vol_min"]    is not None else 1.2,
                "use_vwap":       bool(row["use_vwap"])  if row["use_vwap"]     is not None else False,
                "vwap_tolerance": row["vwap_tolerance"]  if row["vwap_tolerance"] is not None else 0.0,
            }
        except Exception as e:
            logger.error("[TickerParams] get_params %s %s: %s", ticker, interval, e)
            return None

    def get_tf_stats(self, ticker: str) -> dict:
        """Returns {interval: {pf, wr_pct, avg_pnl, n_trades, block_new}} for TF selector."""
        out = {}
        try:
            with _conn(self.db_path) as c:
                rows = c.execute(
                    "SELECT interval,pf,wr_pct,avg_pnl,n_trades,block_new FROM ticker_params WHERE ticker=?",
                    (ticker.upper(),)
                ).fetchall()
            for r in rows:
                out[r["interval"]] = {
                    "pf":        r["pf"],
                    "wr_pct":    r["wr_pct"],
                    "avg_pnl":   r["avg_pnl"],
                    "n_trades":  r["n_trades"],
                    "block_new": bool(r["block_new"]),
                }
        except Exception as e:
            logger.error("[TickerParams] get_tf_stats %s: %s", ticker, e)
        return out

    def is_calibrated(self, ticker: str) -> bool:
        try:
            with _conn(self.db_path) as c:
                return c.execute(
                    "SELECT 1 FROM calibrated_tickers WHERE ticker=?",
                    (ticker.upper(),)
                ).fetchone() is not None
        except Exception:
            return False

    def get_fee_analysis(self, ticker: str) -> dict:
        """Returns fee_analysis dict for a single ticker. O(1) targeted query."""
        try:
            with _conn(self.db_path) as c:
                row = c.execute(
                    "SELECT fee_analysis FROM calibrated_tickers WHERE ticker=?",
                    (ticker.upper(),)
                ).fetchone()
            if row and row["fee_analysis"]:
                return json.loads(row["fee_analysis"])
        except Exception as e:
            logger.error("[TickerParams] get_fee_analysis %s: %s", ticker, e)
        return {}

    def get_all_calibrated(self) -> list[dict]:
        """Returns all calibrated tickers with per-TF stats, hourly and fee analysis."""
        try:
            with _conn(self.db_path) as c:
                tickers = c.execute(
                    "SELECT ticker, calibrated_at, tfs_available, "
                    "hourly_analysis, fee_analysis "
                    "FROM calibrated_tickers ORDER BY ticker"
                ).fetchall()
                result = []
                for t in tickers:
                    params_rows = c.execute(
                        "SELECT interval, pf, wr_pct, avg_pnl, n_trades, block_new "
                        "FROM ticker_params WHERE ticker=?",
                        (t["ticker"],)
                    ).fetchall()
                    tfs = {}
                    for r in params_rows:
                        tfs[r["interval"]] = {
                            "pf":        r["pf"],
                            "wr_pct":    r["wr_pct"],
                            "avg_pnl":   r["avg_pnl"],
                            "n_trades":  r["n_trades"],
                            "block_new": bool(r["block_new"]),
                        }
                    ha_raw = t["hourly_analysis"]
                    fa_raw = t["fee_analysis"] if "fee_analysis" in t.keys() else None
                    hourly = json.loads(ha_raw) if ha_raw else {}
                    fees   = json.loads(fa_raw) if fa_raw else {}
                    result.append({
                        "ticker":          t["ticker"],
                        "calibrated_at":   t["calibrated_at"],
                        "tfs":             tfs,
                        "hourly_analysis": hourly,
                        "fee_analysis":    fees,
                    })
            return result
        except Exception as e:
            logger.error("[TickerParams] get_all_calibrated: %s", e)
            return []
