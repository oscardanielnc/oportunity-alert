"""
utils/intraday_paper.py — Paper-forward del edge OVERNIGHT de semis (Binance perps tradfi).

Valida en TIEMPO REAL (out-of-sample) la estrategia de las Fases 1-6 del estudio
(ver INTRADAY_MOMENTUM_STUDY.md): long en cierta ventana horaria de semis. Por cada ticker,
a su HORA DE ENTRADA crea un registro en pantalla (estado ABIERTO), sigue el %max/%min por el
que atraviesa, y lo CIERRA a su hora de salida con el % final. Contexto: dirección del QQQ y de
la propia acción el día previo (y del día, cuando se conoce).

NO ejecuta órdenes — es registro/observación (paper). 100% lectura de Binance (ccxt) + QQQ (Alpaca).
Sin stop automático (el test mostró que el stop -3/-4% resta; el riesgo se controla por sizing).

Ventanas (hora LIMA, LONG) de binance_best_window.py:
  SNDK 00->12 · MU 00->12 · MRVL 21->06 · INTC 07->14 · AMD 23->09 · TSM 16->04
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)
LIMA = timezone(timedelta(hours=-5))
_DATA = Path(__file__).parent.parent / "data"
_STATE = _DATA / "intraday_paper.json"

# ticker -> (hora_entrada_lima, duracion_horas)
WINDOWS = {
    "SNDK": (0, 12), "MU": (0, 12), "MRVL": (21, 9),
    "INTC": (7, 7), "AMD": (23, 10), "TSM": (16, 12),
}

_ex = None
_klines_cache: dict = {}     # tk -> (fetch_epoch, {hour_epoch:(o,h,l,c)})
_daily_cache: dict = {}      # key -> (utc_date, value)


def _exchange():
    global _ex
    if _ex is None:
        import ccxt
        _ex = ccxt.binanceusdm({"enableRateLimit": True})
    return _ex


def _cached_day(key):
    it = _daily_cache.get(key)
    if it and it[0] == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        return it[1]
    return None


def _store_day(key, val):
    _daily_cache[key] = (datetime.now(timezone.utc).strftime("%Y-%m-%d"), val)
    return val


def _klines_1h(tk):
    """{hour_epoch_sec: (o,h,l,c)} de las últimas ~60h. Cache 5 min."""
    import time as _t
    now = _t.time()
    c = _klines_cache.get(tk)
    if c and now - c[0] < 300:
        return c[1]
    try:
        ex = _exchange()
        since = ex.milliseconds() - 60 * 3600 * 1000
        o = ex.fetch_ohlcv(f"{tk}/USDT:USDT", "1h", since=since, limit=200)
        d = {b[0] // 1000: (b[1], b[2], b[3], b[4]) for b in o}
    except Exception as e:
        logger.debug(f"[IntradayPaper] klines {tk}: {e}")
        d = c[1] if c else {}
    _klines_cache[tk] = (now, d)
    return d


def _stock_prev_dir(tk):
    """'up'/'down' del último día diario CERRADO de la acción (Binance 1d). Cache/día."""
    k = _cached_day(f"sd_{tk}")
    if k is not None:
        return k
    try:
        ex = _exchange()
        o = ex.fetch_ohlcv(f"{tk}/USDT:USDT", "1d", limit=4)
        # último cerrado = penúltima vela (la última está en curso)
        if len(o) >= 3:
            prev, prev2 = o[-2], o[-3]
            d = "up" if prev[4] >= prev2[4] else "down"
        else:
            d = None
    except Exception as e:
        logger.debug(f"[IntradayPaper] stock_prev {tk}: {e}")
        d = None
    return _store_day(f"sd_{tk}", d)


def _qqq_daily():
    """Lista (date_str, ret, close) de QQQ diario (Alpaca). Cache/día."""
    k = _cached_day("qqq")
    if k is not None:
        return k
    import requests
    key = os.environ.get("ALPACA_API_KEY", ""); sec = os.environ.get("ALPACA_SECRET_KEY", "")
    out = []
    try:
        start = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = requests.get("https://data.alpaca.markets/v2/stocks/QQQ/bars",
                         params={"timeframe": "1Day", "start": start, "feed": "sip", "limit": 50, "sort": "asc"},
                         headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}, timeout=20)
        bars = r.json().get("bars", [])
        for i in range(1, len(bars)):
            out.append((bars[i]["t"][:10], bars[i]["c"] / bars[i - 1]["c"] - 1, bars[i]["c"]))
    except Exception as e:
        logger.debug(f"[IntradayPaper] qqq_daily: {e}")
    return _store_day("qqq", out)


def _qqq_dirs(entry_date_str):
    """(qqq_prev_dir, qqq_today_dir) relativo a la fecha de entrada (today=null si aún no cerró)."""
    daily = _qqq_daily()
    prev = today = None
    for d, ret, _c in daily:
        if d < entry_date_str:
            prev = "up" if ret >= 0 else "down"
        elif d == entry_date_str:
            today = "up" if ret >= 0 else "down"
    return prev, today


def _load() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict):
    try:
        _DATA.mkdir(exist_ok=True)
        tmp = _STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STATE)
    except Exception as e:
        logger.warning(f"[IntradayPaper] no pude guardar: {e}")


def _excursions(kl, e_ep, end_ep, entry_price):
    """(max_pct, min_pct, cur_pct) sobre barras 1h en [e_ep, end_ep]."""
    highs, lows, last = [], [], None
    t = e_ep
    while t <= end_ep:
        if t in kl:
            highs.append(kl[t][1]); lows.append(kl[t][2]); last = kl[t][3]
        t += 3600
    if not highs or not entry_price:
        return None, None, None
    return (round((max(highs) / entry_price - 1) * 100, 2),
            round((min(lows) / entry_price - 1) * 100, 2),
            round((last / entry_price - 1) * 100, 2))


def tick():
    """Llamar cada ciclo: abre/actualiza/cierra los paper-trades de la canasta. Best-effort."""
    state = _load()
    now = datetime.now(LIMA)
    now_ep = int(now.timestamp()) // 3600 * 3600
    for tk, (eh, dur) in WINDOWS.items():
        # ventana activa: entrada hoy o ayer (para las que cruzan medianoche)
        entry_dt = None
        for back in (0, 1):
            cand = (now - timedelta(days=back)).replace(hour=eh, minute=0, second=0, microsecond=0)
            if cand <= now < cand + timedelta(hours=dur):
                entry_dt = cand
                break
        if not entry_dt:
            continue
        tid = f"{tk}_{entry_dt.strftime('%Y-%m-%d')}"
        kl = _klines_1h(tk)
        e_ep = int(entry_dt.timestamp())
        exit_ep = e_ep + dur * 3600
        rec = state.get(tid)
        if rec is None:
            entry_price = kl.get(e_ep, (None, None, None, None))[3]
            if not entry_price:   # barra de entrada aún no disponible -> usa último cierre
                last_eps = [t for t in kl if t <= now_ep]
                entry_price = kl[max(last_eps)][3] if last_eps else None
            if not entry_price:
                continue
            qp, qt = _qqq_dirs(entry_dt.strftime("%Y-%m-%d"))
            rec = {
                "id": tid, "ticker": tk, "state": "open",
                "entry_hour": f"{eh:02d}:00", "exit_hour": f"{(eh + dur) % 24:02d}:00", "dur_h": dur,
                "entry_ts": e_ep, "entry_lima": entry_dt.strftime("%Y-%m-%d %H:%M"),
                "exit_lima": (entry_dt + timedelta(hours=dur)).strftime("%Y-%m-%d %H:%M"),
                "entry_price": round(entry_price, 4),
                "qqq_prev": qp, "qqq_today": qt, "stock_prev": _stock_prev_dir(tk),
                "max_pct": 0.0, "min_pct": 0.0, "cur_pct": 0.0,
                "exit_price": None, "final_pct": None,
            }
            state[tid] = rec
            logger.info(f"[IntradayPaper] ABRE {tk} @ {rec['entry_price']} ({rec['entry_lima']} Lima)")
        if rec["state"] == "open":
            mx, mn, cur = _excursions(kl, e_ep, min(now_ep, exit_ep), rec["entry_price"])
            if mx is not None:
                rec["max_pct"], rec["min_pct"], rec["cur_pct"] = mx, mn, cur
            # rellenar contexto que faltó al abrir (p.ej. si la API no respondió en ese momento)
            if rec.get("qqq_prev") is None or rec.get("qqq_today") is None:
                qp, qt = _qqq_dirs(entry_dt.strftime("%Y-%m-%d"))
                if rec.get("qqq_prev") is None:
                    rec["qqq_prev"] = qp
                if rec.get("qqq_today") is None:
                    rec["qqq_today"] = qt
            if rec.get("stock_prev") is None:
                rec["stock_prev"] = _stock_prev_dir(tk)
    # cerrar trades cuya ventana ya terminó
    for tid, rec in state.items():
        if rec["state"] != "open":
            continue
        exit_ep = rec["entry_ts"] + rec["dur_h"] * 3600
        if int(now.timestamp()) < exit_ep:
            continue
        kl = _klines_1h(rec["ticker"])
        exit_price = kl.get(exit_ep, (None, None, None, None))[3]
        if not exit_price:
            below = [t for t in kl if t <= exit_ep]
            exit_price = kl[max(below)][3] if below else rec["entry_price"]
        mx, mn, _ = _excursions(kl, rec["entry_ts"], exit_ep, rec["entry_price"])
        rec["exit_price"] = round(exit_price, 4)
        rec["final_pct"] = round((exit_price / rec["entry_price"] - 1) * 100, 2)
        rec["cur_pct"] = rec["final_pct"]
        if mx is not None:
            rec["max_pct"], rec["min_pct"] = mx, mn
        rec["state"] = "closed"
        # rellenar qqq_today si ya cerró el mercado US
        if rec.get("qqq_today") is None:
            _, qt = _qqq_dirs(rec["entry_lima"][:10])
            rec["qqq_today"] = qt
        logger.info(f"[IntradayPaper] CIERRA {rec['ticker']} final {rec['final_pct']:+.2f}%")
    _save(state)
    return state


def get_trades(limit: int = 60) -> list:
    """Registros para el dashboard: abiertos primero, luego cerrados recientes."""
    state = _load()
    recs = list(state.values())
    recs.sort(key=lambda r: (r["state"] != "open", -(r.get("entry_ts") or 0)))
    return recs[:limit]
