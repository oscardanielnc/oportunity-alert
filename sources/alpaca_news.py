"""
Streaming de noticias en TIEMPO REAL via Alpaca News API.
Fuente del contenido: Benzinga (alianza Alpaca<->Benzinga) — el mismo feed que
Benzinga vende, gratis con la cuenta Alpaca que el proyecto ya usa para precios.

Por que WebSocket y no REST polling: el cuello de botella historico era la latencia
(Finnhub free lagea 30-60 min). El stream hace push instantaneo -> catalizadores frescos.

Cada noticia se normaliza al MISMO dict que sources/finnhub_news para encajar sin
cambios en el pipeline (passes_filter / conviction gates / event gate / dedup).
"""
from __future__ import annotations  # la VM corre Python 3.9: `dict | None` (PEP 604) es 3.10+

import os
import json
import logging
import time
from datetime import datetime, timezone

import websocket  # websocket-client

logger = logging.getLogger(__name__)

ALPACA_NEWS_WS = "wss://stream.data.alpaca.markets/v1beta1/news"
SOURCE_TAG = "ALPACA_BENZINGA"

# Pausa de reconexion con backoff exponencial (segundos)
_RECONNECT_MIN = 1
_RECONNECT_MAX = 60
# Timeout de recv: si no llega nada en este tiempo, se reintenta el recv (keepalive)
_RECV_TIMEOUT = 60


def _credentials() -> tuple[str, str]:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise EnvironmentError("ALPACA_API_KEY y ALPACA_SECRET_KEY deben estar en el entorno")
    return key, secret


def _normalize(msg: dict, watchlist_set: set) -> dict | None:
    """
    Convierte un mensaje de noticia de Alpaca al dict estandar del pipeline.
    Retorna None si la noticia no toca ningun ticker de la watchlist.

    Formato Alpaca (T == "n"):
      {"T":"n","id":123,"headline":"...","summary":"...","content":"...",
       "url":"...","created_at":"2026-...Z","symbols":["NVDA","AMD"],"source":"benzinga"}
    """
    symbols = [str(s).upper() for s in (msg.get("symbols") or [])]
    tickers_found = [s for s in symbols if s in watchlist_set]
    if not tickers_found:
        return None  # noticia irrelevante para nuestro universo

    created = msg.get("created_at", "") or msg.get("updated_at", "")
    now = datetime.now(timezone.utc)
    try:
        pub_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except Exception:
        pub_dt = now
    age_minutes = (now - pub_dt).total_seconds() / 60

    headline = msg.get("headline", "") or ""
    summary = msg.get("summary", "") or msg.get("content", "") or ""

    return {
        "id": f"alpaca_{msg.get('id', '')}",
        "source": SOURCE_TAG,
        "title": headline,
        "summary": summary,
        "url": msg.get("url", ""),
        "published_at": pub_dt.isoformat(),
        "age_minutes": round(age_minutes, 1),
        "tickers_found": tickers_found,
        "raw_text": f"{headline} {summary}".upper(),
    }


def _recv_json(ws) -> list:
    """Recibe un frame y lo parsea a lista de mensajes (Alpaca envia arrays)."""
    raw = ws.recv()
    if not raw:
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]


def _handshake(ws, key: str, secret: str) -> bool:
    """connected -> auth -> subscribe('*'). Retorna True si quedo autenticado y suscrito."""
    # 1. mensaje de conexion
    for m in _recv_json(ws):
        if m.get("T") == "success" and m.get("msg") == "connected":
            break

    # 2. autenticacion
    ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
    authed = False
    for m in _recv_json(ws):
        if m.get("T") == "success" and m.get("msg") == "authenticated":
            authed = True
        elif m.get("T") == "error":
            logger.error(f"[AlpacaNews] auth rechazada: {m}")
            return False
    if not authed:
        return False

    # 3. suscripcion a TODAS las noticias (filtramos por watchlist en codigo,
    #    asi capturamos articulos multi-ticker que mencionan nuestros nombres)
    ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
    _recv_json(ws)  # confirmacion de subscripcion
    return True


def stream_news(watchlist: list[str], on_article, stop_event=None) -> None:
    """
    Bloqueante: abre el WebSocket, autentica, se suscribe y llama
    `on_article(dict)` por cada noticia relevante. Reconecta solo con backoff.
    Pensado para correr en su propio thread (ver alpaca_news_loop en main.py).
    """
    key, secret = _credentials()
    watchlist_set = {t.upper() for t in watchlist}
    backoff = _RECONNECT_MIN

    while not (stop_event and stop_event.is_set()):
        ws = None
        try:
            ws = websocket.create_connection(ALPACA_NEWS_WS, timeout=30)
            if not _handshake(ws, key, secret):
                ws.close()
                time.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX)
                continue

            logger.info("[AlpacaNews] WebSocket conectado y suscrito (fuente: Benzinga)")
            backoff = _RECONNECT_MIN
            ws.settimeout(_RECV_TIMEOUT)

            while not (stop_event and stop_event.is_set()):
                try:
                    messages = _recv_json(ws)
                except websocket.WebSocketTimeoutException:
                    continue  # sin noticias en la ventana; seguimos escuchando
                if not messages:
                    break  # conexion cerrada por el server -> reconectar
                for m in messages:
                    t = m.get("T")
                    if t == "n":
                        article = _normalize(m, watchlist_set)
                        if article:
                            try:
                                on_article(article)
                            except Exception as e:
                                logger.error(f"[AlpacaNews] error procesando noticia: {e}")
                    elif t == "error":
                        logger.error(f"[AlpacaNews] error del stream: {m}")

        except Exception as e:
            logger.error(f"[AlpacaNews] conexion caida: {e}; reconectando en {backoff}s")
        finally:
            try:
                if ws is not None:
                    ws.close()
            except Exception:
                pass

        if not (stop_event and stop_event.is_set()):
            time.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX)
