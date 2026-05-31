"""
Precio real-time via Alpaca Markets API.
Cubre pre-market (4am ET) y after-hours (hasta 8pm ET).
Usa feed=iex (gratuito, incluye AH).
"""
import os
import logging
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser

logger = logging.getLogger(__name__)

ALPACA_BASE = "https://data.alpaca.markets"
# SIP feed for bars (free plan, covers AH/pre-market on all exchanges)
# IEX feed for trades/latest (real-time, but only IEX exchange coverage)
ALPACA_FEED_BARS   = "sip"
ALPACA_FEED_TRADES = "iex"

# Tickers que Alpaca IEX no cubre (crypto, ETFs inversos, etc.)
# Para estos se devuelve precio sin error para no bloquear el pipeline
ALPACA_UNSUPPORTED = {"BTC", "ETH", "XRP", "BNB", "SOL", "DOGE", "ADA"}


def _get_headers() -> dict:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise EnvironmentError("ALPACA_API_KEY y ALPACA_SECRET_KEY deben estar en el entorno")
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def _try_etoro_price(ticker: str) -> dict:
    """
    Intenta armar el dict de get_realtime_price con datos de eToro (precio fiel del
    broker real). Necesita precio Y prev_close para calcular change_pct (clave para
    Gate 1). El prev_close sale del snapshot IEX de Alpaca (cierre diario consolidado).
    Retorna {} si falta cualquier pieza → el caller cae a Alpaca.

    Logging: distingue "eToro no cubre este símbolo" (esperado, debug) de "eToro tiene
    el símbolo pero NO devolvió precio" (anormal → warning, eToro posiblemente caído).
    """
    try:
        from utils.etoro_market import fetch_price as _etoro_fetch, get_instrument_id
    except Exception as e:
        logger.warning(f"[precio] no pude importar etoro_market ({e}) — usando Alpaca")
        return {}

    has_id = get_instrument_id(ticker) is not None

    px = _etoro_fetch(ticker)
    if not px or not px.get("current_price"):
        if has_id:
            # eToro SÍ lista el ticker pero no devolvió precio → algo falló (API caída,
            # token, mercado). Visible para diagnosticar; el caller cae a Alpaca igual.
            logger.warning(f"[precio] eToro cubre {ticker} pero no devolvió precio — fallback Alpaca")
        else:
            logger.debug(f"[precio] {ticker} no está en eToro — usando Alpaca")
        return {}
    current_price = px["current_price"]

    # prev_close: cierre diario ANTERIOR, vía snapshot IEX de Alpaca.
    # OJO IMPORTANTE: el plan free de Alpaca BLOQUEA todo dato SIP reciente (snapshot
    # feed=sip → 403; barras diarias SIP recientes → {"bars":null}). Lo único que el
    # free entrega para esto es el snapshot IEX, que trae prevDailyBar/dailyBar. Y para
    # un CIERRE DIARIO usar IEX es correcto: es un valor consolidado ya cerrado, no un
    # tick intradía (que es donde IEX falla por cobertura ~3%). prevDailyBar = cierre del
    # día previo; si falta, dailyBar (última sesión cerrada) es buen sustituto.
    prev_close = None
    try:
        headers = _get_headers()
        snap_resp = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/snapshot",
            params={"feed": ALPACA_FEED_TRADES},   # IEX (el free no permite SIP reciente)
            headers=headers,
            timeout=8,
        )
        if snap_resp.status_code == 200:
            snap = snap_resp.json()
            prev_close = (snap.get("prevDailyBar") or {}).get("c") \
                or (snap.get("dailyBar") or {}).get("c")
    except Exception:
        pass

    if not prev_close:
        # eToro dio precio pero no conseguimos cierre previo → sin change_pct el Gate 1
        # no opera. Anormal (Alpaca snapshot IEX suele responder) → visible.
        logger.warning(f"[precio] eToro {ticker} OK pero sin prev_close (Alpaca snapshot) — fallback Alpaca")
        return {}   # sin change_pct el Gate 1 no puede operar → mejor usar Alpaca

    change_pct = round((current_price - float(prev_close)) / float(prev_close) * 100, 2)
    logger.debug(f"[precio] {ticker} via eToro ${current_price} ({change_pct:+.2f}%)")
    return {
        "ticker": ticker,
        "current_price": current_price,
        "prev_close": float(prev_close),
        "change_pct": change_pct,
        "last_trade_time": px.get("last_trade_time"),
        "last_trade_age_minutes": px.get("last_trade_age_minutes"),
        "candles_1m": px.get("candles_1m", []),
        "volume_ratio": None,
        "error": None,
        "price_source": "etoro",
    }


def get_realtime_price(ticker: str) -> dict:
    """
    Retorna precio actual del ticker incluyendo pre-market y AH.

    Retorna:
        {
            "ticker": str,
            "current_price": float,
            "prev_close": float,
            "change_pct": float,
            "last_trade_time": str (ISO),
            "last_trade_age_minutes": float,
            "candles_1m": list[dict],   # últimas 10 velas de 1 min
            "volume_ratio": float,       # volumen actual vs promedio 20d (estimado)
            "error": str | None
        }
    """
    result = {
        "ticker": ticker,
        "current_price": None,
        "prev_close": None,
        "change_pct": None,
        "last_trade_time": None,
        "last_trade_age_minutes": None,
        "candles_1m": [],
        "volume_ratio": None,
        "error": None,
    }

    if ticker in ALPACA_UNSUPPORTED:
        result["error"] = f"{ticker} es crypto — precio via Binance, no Alpaca"
        logger.debug(f"Alpaca no soporta {ticker} (crypto) — continuando sin precio")
        return result

    # ── Fuente primaria: eToro (mismo broker donde opera Oscar → precio fiel) ──
    # Si eToro responde con precio + change_pct, lo usamos. Si falla o le falta el
    # change_pct (necesita prev_close), caemos al flujo Alpaca de abajo (IEX→SIP).
    try:
        etoro_px = _try_etoro_price(ticker)
        if etoro_px:
            return etoro_px
    except Exception as e:
        logger.debug(f"[alpaca_price] eToro primaria falló {ticker}: {e} — fallback Alpaca")

    try:
        headers = _get_headers()

        # Último trade (IEX — tiempo real durante mercado regular)
        trade_resp = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/trades/latest",
            params={"feed": ALPACA_FEED_TRADES},
            headers=headers,
            timeout=10,
        )
        trade_resp.raise_for_status()
        trade_data = trade_resp.json().get("trade") or {}
        current_price = float(trade_data.get("p", 0))
        trade_time_str = trade_data.get("t", "")

        result["current_price"] = current_price
        result["last_trade_time"] = trade_time_str

        # Calcular antigüedad del último trade IEX
        trade_age_minutes = None
        if trade_time_str:
            try:
                trade_dt = dateparser.parse(trade_time_str)
                now_utc = datetime.now(timezone.utc)
                if trade_dt.tzinfo is None:
                    trade_dt = trade_dt.replace(tzinfo=timezone.utc)
                trade_age_minutes = (now_utc - trade_dt).total_seconds() / 60
                result["last_trade_age_minutes"] = round(trade_age_minutes, 1)
            except Exception:
                pass

        # Si el trade IEX tiene >5 min de antigüedad → usar último bar SIP (cubre AH/pre-market)
        if trade_age_minutes is None or trade_age_minutes > 5:
            try:
                sip_start = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                sip_resp = requests.get(
                    f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                    params={"timeframe": "1Min", "start": sip_start,
                            "feed": "sip", "limit": 1, "sort": "desc"},
                    headers=headers,
                    timeout=8,
                )
                if sip_resp.status_code == 200:
                    sip_bars = sip_resp.json().get("bars") or []
                    if sip_bars and sip_bars[0].get("c"):
                        current_price = float(sip_bars[0]["c"])
                        result["current_price"] = current_price
                        logger.debug(
                            f"[alpaca_price] {ticker} IEX stale "
                            f"({trade_age_minutes:.0f if trade_age_minutes else '?'} min) "
                            f"→ SIP bar ${current_price:.2f}"
                        )
            except Exception as sip_e:
                logger.debug(f"[alpaca_price] SIP bar fallback {ticker}: {sip_e}")

        # Snapshot para prev_close y change_pct (IEX, solo necesitamos prevDailyBar)
        snap_resp = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/snapshot",
            params={"feed": ALPACA_FEED_TRADES},
            headers=headers,
            timeout=10,
        )
        if snap_resp.status_code == 200:
            snap = snap_resp.json()
            prev_close = snap.get("prevDailyBar", {}).get("c")
            if prev_close and current_price:
                result["prev_close"] = float(prev_close)
                result["change_pct"] = round(
                    (current_price - float(prev_close)) / float(prev_close) * 100, 2
                )

        # Velas de 1 minuto — IEX primero (real-time mercado regular), SIP si IEX está obsoleto
        now_utc = datetime.now(timezone.utc)
        start = (now_utc - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bars = []
        for _feed in ("iex", ALPACA_FEED_BARS):
            bars_resp = requests.get(
                f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                params={
                    "timeframe": "1Min",
                    "start": start,
                    "feed": _feed,
                    "limit": 10,
                    "sort": "desc",
                },
                headers=headers,
                timeout=10,
            )
            if bars_resp.status_code == 200:
                _bars = bars_resp.json().get("bars") or []
                if _bars:
                    try:
                        last_t = dateparser.parse(_bars[0].get("t", ""))
                        if last_t:
                            if last_t.tzinfo is None:
                                last_t = last_t.replace(tzinfo=timezone.utc)
                            age = (now_utc - last_t).total_seconds() / 60
                            if age <= 30:   # IEX fresco → usarlo
                                bars = _bars
                                break
                    except Exception:
                        pass
                    if not bars:
                        bars = _bars   # IEX obsoleto pero SIP tiene algo
        if bars_resp.status_code == 200 and not bars:
            bars = bars_resp.json().get("bars") or []
            result["candles_1m"] = [
                {
                    "t": b.get("t"),
                    "o": b.get("o"),
                    "h": b.get("h"),
                    "l": b.get("l"),
                    "c": b.get("c"),
                    "v": b.get("v"),
                }
                for b in bars
            ]

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            result["error"] = f"Ticker {ticker} no encontrado en Alpaca"
        else:
            result["error"] = f"HTTP error Alpaca: {e}"
        logger.warning(result["error"])
    except Exception as e:
        result["error"] = f"Error obteniendo precio Alpaca: {e}"
        logger.error(result["error"])

    return result


def summarize_candles(candles: list) -> str:
    """Genera resumen legible de velas para el prompt de Claude."""
    if not candles:
        return "Sin datos de velas"
    try:
        # Las velas vienen en orden desc (más reciente primero)
        recent = candles[:5]
        greens = sum(1 for c in recent if c.get("c", 0) >= c.get("o", 0))
        reds = len(recent) - greens
        vols = [c.get("v", 0) for c in recent if c.get("v")]
        vol_trend = "vol creciente" if len(vols) >= 2 and vols[0] > vols[-1] else "vol decreciente"
        return f"{greens} velas verdes / {reds} rojas en últimas 5 velas 1m, {vol_trend}"
    except Exception:
        return "Error resumiendo velas"
