"""
eToro Trader — ejecución de órdenes via eToro OpenAPI oficial.

Endpoints:
  Abrir:    POST {api_base}/api/v1/trading/execution/market-open-orders/by-amount
  Cerrar:   POST {api_base}/api/v1/trading/execution/market-close-orders/positions/{positionId}
  Buscar:   GET  https://public-api.etoro.com/api/v1/market-data/search?internalSymbolFull={ticker}

Configuración en data/etoro_config.json:
  {
    "environment": "demo",        <- "demo" | "real"
    "api_base":    "https://api.etoro.com",
    "public_key":  "...",         <- x-api-key  (demo)
    "user_key":    "...",         <- x-user-key (demo)
    "real_public_key": "...",     <- x-api-key  (real) — solo si environment=real
    "real_user_key":   "..."      <- x-user-key (real) — solo si environment=real
  }

Cuando environment=="demo" usa public_key/user_key (credenciales de demo).
Cuando environment=="real" usa real_public_key/real_user_key.
"""
import json
import logging
import uuid
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

ETORO_CONFIG_PATH = Path(__file__).parent.parent / "data" / "etoro_config.json"
MARKET_DATA_BASE  = "https://public-api.etoro.com"

# Cache de instrument IDs: { "NVDA": 2564, ... }
_instrument_cache: dict[str, int] = {}


# ── Config & headers ──────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open(ETORO_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _get_credentials(config: dict) -> tuple[str, str]:
    """Retorna (public_key, user_key) según el environment activo."""
    env = config.get("environment", "demo").lower()
    if env == "real":
        pk = config.get("real_public_key") or config.get("public_key", "")
        uk = config.get("real_user_key")   or config.get("user_key", "")
    else:
        pk = config.get("public_key", "")
        uk = config.get("user_key", "")
    return pk, uk


def _headers(config: dict) -> dict:
    pk, uk = _get_credentials(config)
    return {
        "x-api-key":    pk,
        "x-user-key":   uk,
        "x-request-id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def get_environment() -> str:
    try:
        return _load_config().get("environment", "demo").lower()
    except Exception:
        return "demo"


# ── Instrumento ID ────────────────────────────────────────────────────────────

def resolve_instrument_id(ticker: str) -> int | None:
    """
    Convierte ticker a instrumentId de eToro (número entero).
    Resultado cacheado en memoria — inmutable, seguro cachear forever.
    """
    if ticker in _instrument_cache:
        return _instrument_cache[ticker]

    try:
        config = _load_config()
        pk, uk = _get_credentials(config)
        headers = {
            "x-api-key":    pk,
            "x-user-key":   uk,
            "x-request-id": str(uuid.uuid4()),
        }
        resp = requests.get(
            f"{MARKET_DATA_BASE}/api/v1/market-data/search",
            params={"internalSymbolFull": ticker},
            headers=headers,
            timeout=8,
        )
        resp.raise_for_status()
        items = resp.json().get("items") or []
        for item in items:
            if item.get("internalSymbolFull", "").upper() == ticker.upper():
                iid = int(item["instrumentId"])
                _instrument_cache[ticker] = iid
                logger.info("[EToroTrader] %s → instrumentId=%d", ticker, iid)
                return iid
        logger.warning("[EToroTrader] instrumentId no encontrado para %s", ticker)
    except Exception as e:
        logger.error("[EToroTrader] resolve_instrument_id %s: %s", ticker, e)

    return None


def prefetch_instrument_ids(tickers: list[str]) -> None:
    """Precarga los IDs en cache. Llamar al inicio con el watchlist."""
    for t in tickers:
        if t not in _instrument_cache:
            resolve_instrument_id(t)


# ── Abrir posición ────────────────────────────────────────────────────────────

def open_position(
    ticker: str,
    direction: str,       # "LONG" | "SHORT"
    amount_usd: float,    # capital total a usar (100% disponible)
    leverage: int = 1,
    stop_loss_pct: float | None = None,   # % desde precio de entrada, opcional
    take_profit_pct: float | None = None, # % desde precio de entrada, opcional
) -> dict:
    """
    Abre una posición de mercado.

    Retorna:
    {
        "ok": bool,
        "position_id": str | None,
        "entry_price": float | None,
        "units": float | None,
        "amount": float | None,
        "direction": str,
        "environment": str,
        "error": str | None,
        "raw": dict,        <- respuesta completa de eToro
    }
    """
    instrument_id = resolve_instrument_id(ticker)
    if instrument_id is None:
        return {
            "ok": False, "position_id": None, "entry_price": None,
            "units": None, "amount": None, "direction": direction,
            "environment": get_environment(),
            "error": f"instrumentId no encontrado para {ticker}",
            "raw": {},
        }

    is_buy = direction.upper() in ("LONG", "BUY")

    body: dict = {
        "InstrumentId": instrument_id,
        "Amount":       round(amount_usd, 2),
        "Leverage":     leverage,
        "IsBuy":        is_buy,
    }

    # Stop-loss y take-profit opcionales (en puntos de precio o %)
    # eToro acepta StopLoss y TakeProfit como precios absolutos si los incluyes.
    # Los dejamos fuera por ahora y dejamos que el watcher maneje salidas por señal.

    try:
        config  = _load_config()
        base    = config["api_base"]
        headers = _headers(config)
        env     = config.get("environment", "demo").lower()

        resp = requests.post(
            f"{base}/api/v1/trading/execution/market-open-orders/by-amount",
            headers=headers,
            json=body,
            timeout=15,
        )

        raw = {}
        try:
            raw = resp.json()
        except Exception:
            raw = {"raw_text": resp.text[:300]}

        if not resp.ok:
            logger.error(
                "[EToroTrader] open_position %s FALLO HTTP %d: %s",
                ticker, resp.status_code, json.dumps(raw)[:200],
            )
            return {
                "ok": False, "position_id": None, "entry_price": None,
                "units": None, "amount": amount_usd, "direction": direction,
                "environment": env, "error": f"HTTP {resp.status_code}: {raw}",
                "raw": raw,
            }

        # eToro puede devolver el positionId con diferentes nombres
        position_id = (
            str(raw.get("positionId") or raw.get("PositionId") or
                raw.get("position_id") or raw.get("orderId") or "")
        )
        entry_price = float(
            raw.get("openRate") or raw.get("OpenRate") or
            raw.get("executionPrice") or raw.get("rate") or 0
        )
        units = float(raw.get("units") or raw.get("Units") or raw.get("amount") or 0)

        logger.info(
            "[EToroTrader] Posición abierta: %s %s $%.2f | positionId=%s entry=%.4f env=%s",
            ticker, direction, amount_usd, position_id, entry_price, env,
        )

        return {
            "ok":          True,
            "position_id": position_id,
            "entry_price": entry_price or None,
            "units":       units or None,
            "amount":      amount_usd,
            "direction":   direction,
            "environment": env,
            "error":       None,
            "raw":         raw,
        }

    except requests.exceptions.Timeout:
        logger.error("[EToroTrader] open_position %s TIMEOUT", ticker)
        return {
            "ok": False, "position_id": None, "entry_price": None,
            "units": None, "amount": amount_usd, "direction": direction,
            "environment": get_environment(), "error": "timeout",
            "raw": {},
        }
    except Exception as e:
        logger.error("[EToroTrader] open_position %s ERROR: %s", ticker, e)
        return {
            "ok": False, "position_id": None, "entry_price": None,
            "units": None, "amount": amount_usd, "direction": direction,
            "environment": get_environment(), "error": str(e)[:200],
            "raw": {},
        }


# ── Cerrar posición ───────────────────────────────────────────────────────────

def close_position(position_id: str, units_to_close: float | None = None) -> dict:
    """
    Cierra una posición abierta.

    position_id     : ID retornado por open_position o por get_portfolio().
    units_to_close  : None = cierre completo; número = cierre parcial.

    Retorna:
    {
        "ok": bool,
        "position_id": str,
        "close_price": float | None,
        "environment": str,
        "error": str | None,
        "raw": dict,
    }
    """
    if not position_id:
        return {
            "ok": False, "position_id": position_id, "close_price": None,
            "environment": get_environment(), "error": "position_id vacío", "raw": {},
        }

    body = {"UnitsToDeduct": units_to_close}  # None = cierre completo

    try:
        config  = _load_config()
        base    = config["api_base"]
        headers = _headers(config)
        env     = config.get("environment", "demo").lower()

        resp = requests.post(
            f"{base}/api/v1/trading/execution/market-close-orders/positions/{position_id}",
            headers=headers,
            json=body,
            timeout=15,
        )

        raw = {}
        try:
            raw = resp.json()
        except Exception:
            raw = {"raw_text": resp.text[:300]}

        if not resp.ok:
            logger.error(
                "[EToroTrader] close_position %s FALLO HTTP %d: %s",
                position_id, resp.status_code, json.dumps(raw)[:200],
            )
            return {
                "ok": False, "position_id": position_id, "close_price": None,
                "environment": env, "error": f"HTTP {resp.status_code}: {raw}",
                "raw": raw,
            }

        close_price = float(
            raw.get("closeRate") or raw.get("CloseRate") or
            raw.get("executionPrice") or raw.get("rate") or 0
        ) or None

        logger.info(
            "[EToroTrader] Posición cerrada: positionId=%s close=%.4f env=%s",
            position_id, close_price or 0, env,
        )

        return {
            "ok":          True,
            "position_id": position_id,
            "close_price": close_price,
            "environment": env,
            "error":       None,
            "raw":         raw,
        }

    except requests.exceptions.Timeout:
        logger.error("[EToroTrader] close_position %s TIMEOUT", position_id)
        return {
            "ok": False, "position_id": position_id, "close_price": None,
            "environment": get_environment(), "error": "timeout", "raw": {},
        }
    except Exception as e:
        logger.error("[EToroTrader] close_position %s ERROR: %s", position_id, e)
        return {
            "ok": False, "position_id": position_id, "close_price": None,
            "environment": get_environment(), "error": str(e)[:200], "raw": {},
        }


# ── Capital disponible ────────────────────────────────────────────────────────

def get_available_capital() -> float:
    """Retorna el capital disponible (cash) desde eToro. 0.0 si hay error."""
    try:
        from utils.etoro_client import get_portfolio
        p = get_portfolio()
        return float(p.get("available_cash") or 0.0)
    except Exception as e:
        logger.warning("[EToroTrader] get_available_capital error: %s", e)
        return 0.0
