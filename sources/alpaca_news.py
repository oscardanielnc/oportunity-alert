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
# 2026-06-10: tras cada timeout se manda un ping (si la conexion esta half-open —
# NAT/firewall mato el TCP sin RST — el ping falla y fuerza la reconexion; antes el
# thread quedaba vivo pero SORDO indefinidamente). Tope de timeouts consecutivos como
# segunda red: 30 min sin NADA de un feed mundial "*" en dias habiles es anomalo.
_MAX_SILENT_TIMEOUTS = 30
# Cada cuanto refrescar la watchlist viva (la suscripcion WS es a "*", asi que el
# filtro por watchlist es local; no hay que re-suscribir cuando cambia).
_WATCHLIST_REFRESH_S = 60


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


def stream_news(watchlist, on_article, stop_event=None) -> None:
    """
    Bloqueante: abre el WebSocket, autentica, se suscribe y llama
    `on_article(dict)` por cada noticia relevante. Reconecta solo con backoff.
    Pensado para correr en su propio thread (ver alpaca_news_loop en main.py).

    `watchlist` puede ser una lista fija o un callable que devuelve la lista viva.
    Con un callable, el filtro se refresca cada _WATCHLIST_REFRESH_S → los tickers
    que agregas por el dashboard se reflejan sin reiniciar el servicio (la suscripción
    WS es a "*", el filtro por watchlist es 100% local).
    """
    key, secret = _credentials()
    get_wl = watchlist if callable(watchlist) else (lambda: watchlist)

    def _build_set():
        # Si el getter falla o devuelve vacío, mantenemos el set previo (no filtramos
        # de más ni de menos). Solo aplicamos un set nuevo si trae tickers.
        try:
            wl = get_wl() or []
        except Exception:
            wl = []
        return {t.upper() for t in wl}

    watchlist_set = _build_set()
    last_wl_refresh = time.time()
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
            silent_timeouts = 0

            while not (stop_event and stop_event.is_set()):
                # Refrescar la watchlist viva cada _WATCHLIST_REFRESH_S (antes y después
                # del recv, que puede bloquear hasta _RECV_TIMEOUT). Mismo thread que el
                # consumo de mensajes → reasignación sin race condition.
                if time.time() - last_wl_refresh > _WATCHLIST_REFRESH_S:
                    new_set = _build_set()
                    if new_set:
                        watchlist_set = new_set
                    last_wl_refresh = time.time()

                try:
                    messages = _recv_json(ws)
                    silent_timeouts = 0
                except websocket.WebSocketTimeoutException:
                    silent_timeouts += 1
                    if silent_timeouts >= _MAX_SILENT_TIMEOUTS:
                        logger.warning(
                            f"[AlpacaNews] {silent_timeouts} min sin mensajes — "
                            "reconexion preventiva"
                        )
                        break
                    try:
                        ws.ping()   # half-open detection: si el TCP murio, esto lanza
                    except Exception as ping_e:
                        logger.warning(f"[AlpacaNews] ping fallo ({ping_e}) — reconectando")
                        break
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
