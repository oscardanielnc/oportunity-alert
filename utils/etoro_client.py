"""
eToro API — wrapper READ-ONLY.
Lee credenciales desde C:/Users/LENOVO/trading/etoro_config.json.
NUNCA ejecuta órdenes.

Resiliencia:
  - Token de sesión opaco (no JWT estándar) — puede expirar sin previo aviso.
  - Circuit breaker: tras CIRCUIT_OPEN_THRESHOLD fallos consecutivos, pausa
    CIRCUIT_PAUSE_MINUTES minutos antes de reintentar.
  - Detecta 401/403 y alerta via SMS (token expirado).
  - Todas las funciones públicas tienen fallback seguro — nunca bloquean el pipeline.
"""
import json
import logging
import time
import uuid
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

ETORO_CONFIG_PATH = Path("C:/Users/LENOVO/trading/etoro_config.json")

# ── Circuit breaker ───────────────────────────────────────────────────────────
CIRCUIT_OPEN_THRESHOLD = 5      # fallos consecutivos antes de abrir el circuito
CIRCUIT_PAUSE_MINUTES  = 30     # minutos de pausa cuando el circuito está abierto

_cb_failures   = 0              # contador de fallos consecutivos
_cb_open_until = 0.0            # timestamp hasta el que el circuito está abierto
_token_alert_sent = False       # evitar spam de SMS por token expirado

SECTOR_MAP = {
    "NVDA": "semiconductores", "AMD": "semiconductores", "ASML": "semiconductores",
    "TSM": "semiconductores", "AVGO": "semiconductores", "MU": "semiconductores",
    "COHR": "semiconductores",
    "QBTS": "cuantica", "IONQ": "cuantica", "RGTI": "cuantica", "QUBT": "cuantica",
    "PLTR": "software_ai", "CRM": "software_ai", "APP": "software_ai",
    "RDDT": "social", "ARM": "semiconductores",
    "XRP": "crypto",
}


def _load_config() -> dict:
    with open(ETORO_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _get_headers(config: dict) -> dict:
    return {
        "x-api-key": config["public_key"],
        "x-user-key": config["user_key"],
        "x-request-id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def _circuit_ok() -> bool:
    """Retorna True si el circuito permite hacer la llamada."""
    global _cb_open_until
    if _cb_open_until and time.time() < _cb_open_until:
        remaining = int((_cb_open_until - time.time()) / 60)
        logger.debug(f"[eToro] Circuito abierto — reintentando en {remaining} min")
        return False
    return True


def _on_success():
    global _cb_failures, _cb_open_until
    _cb_failures   = 0
    _cb_open_until = 0.0


def _on_failure(status_code: int = 0) -> str:
    """Registra el fallo y abre el circuito si supera el umbral. Retorna motivo."""
    global _cb_failures, _cb_open_until, _token_alert_sent

    _cb_failures += 1
    logger.warning(f"[eToro] Fallo #{_cb_failures} (HTTP {status_code})")

    if status_code in (401, 403):
        msg = "token_expired"
        if not _token_alert_sent:
            _notify_token_expired()
            _token_alert_sent = True
    else:
        msg = f"http_{status_code}" if status_code else "network_error"

    if _cb_failures >= CIRCUIT_OPEN_THRESHOLD:
        _cb_open_until = time.time() + CIRCUIT_PAUSE_MINUTES * 60
        logger.error(
            f"[eToro] Circuito ABIERTO tras {_cb_failures} fallos — "
            f"pausa {CIRCUIT_PAUSE_MINUTES} min"
        )

    return msg


def _notify_token_expired():
    """Envía SMS de alerta cuando el token de eToro expira."""
    try:
        import os
        from twilio.rest import Client as TwilioClient
        sid   = os.environ.get("TWILIO_ACCOUNT_SID", "")
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        to    = os.environ.get("TWILIO_TO", "")
        channel = os.environ.get("NOTIFICATION_CHANNEL", "whatsapp").lower()
        if not (sid and token and to):
            logger.error("[eToro] Token expirado — no hay Twilio configurado para notificar")
            return
        body = (
            "ALERTA OportunityAlert:\n"
            "Token eToro expirado (401/403).\n"
            "Accion requerida:\n"
            "1. Abre etoro_config.json\n"
            "2. Reemplaza user_key con nuevo token de sesion\n"
            "El sistema sigue funcionando sin eToro (gates 1-3 activos)."
        )
        client = TwilioClient(sid, token)
        from_wa = "whatsapp:+14155238886"
        if channel == "whatsapp":
            client.messages.create(body=body, from_=from_wa, to=f"whatsapp:{to}")
        else:
            from_ = os.environ.get("TWILIO_FROM", "")
            client.messages.create(body=body, from_=from_, to=to)
        logger.error("[eToro] SMS de token expirado enviado a Oscar")
    except Exception as e:
        logger.error(f"[eToro] No pudo notificar token expirado: {e}")


def reset_token_alert():
    """Llamar después de actualizar etoro_config.json con nuevo token."""
    global _token_alert_sent, _cb_failures, _cb_open_until
    _token_alert_sent = False
    _cb_failures      = 0
    _cb_open_until    = 0.0
    logger.info("[eToro] Circuit breaker y alerta de token reseteados")


def health_check() -> dict:
    """
    Ping rápido a eToro. Retorna estado para el heartbeat.
    No cuenta como fallo en el circuit breaker.
    """
    try:
        config  = _load_config()
        base    = config["api_base"]
        headers = _get_headers(config)
        r = requests.get(f"{base}/ping", headers=headers, timeout=5)
        if r.status_code == 200:
            return {"ok": True, "status": "online", "failures": _cb_failures}
        return {"ok": False, "status": f"HTTP {r.status_code}", "failures": _cb_failures}
    except Exception as e:
        return {"ok": False, "status": str(e)[:60], "failures": _cb_failures}


def _extract_ticker(pos: dict) -> str:
    for field in ["ticker", "symbol", "instrumentName", "Instrument"]:
        val = pos.get(field, "")
        if val and len(val) <= 6 and str(val).isupper():
            return str(val)
    raw = pos.get("instrumentName", pos.get("Instrument", "UNKNOWN"))
    return str(raw)[:6].upper().replace(" ", "")


def _calc_pnl_pct(pos: dict) -> float:
    try:
        open_r = float(pos.get("openRate") or pos.get("OpenRate") or 0)
        curr_r = float(pos.get("currentRate") or pos.get("Rate") or 0)
        if open_r and curr_r:
            direction = "BUY" if (pos.get("isBuy") or pos.get("Direction") == "BUY") else "SELL"
            pct = (curr_r - open_r) / open_r * 100
            return round(pct if direction == "BUY" else -pct, 2)
    except Exception:
        pass
    profit = float(pos.get("netProfit") or pos.get("Profit") or 0)
    invested = float(pos.get("amount") or pos.get("InvestedAmount") or 1)
    return round(profit / invested * 100, 2) if invested else 0.0


def get_portfolio() -> dict:
    """
    Retorna posiciones abiertas y capital disponible.

    Retorna:
    {
        "positions": [{"ticker", "instrument_id", "units", "open_rate",
                        "current_rate", "net_profit", "net_profit_pct",
                        "direction", "invested_amount"}],
        "available_cash": float,
        "total_value": float,
        "error": str | None,
    }
    """
    if not _circuit_ok():
        return {"positions": [], "available_cash": 0, "total_value": 0,
                "error": "circuit_open"}

    try:
        config = _load_config()
        base = config["api_base"]
        headers = _get_headers(config)

        # Endpoint confirmado: /trading/info/portfolio (sin /{env}/)
        resp = requests.get(
            f"{base}/trading/info/portfolio",
            headers=headers,
            timeout=10,
        )

        if resp.status_code in (401, 403):
            reason = _on_failure(resp.status_code)
            return {"positions": [], "available_cash": 0, "total_value": 0,
                    "error": f"auth_error:{reason}"}

        resp.raise_for_status()
        data = resp.json()

        portfolio = data.get("clientPortfolio", data)
        logger.debug(f"[eToro] Portfolio keys: {list(portfolio.keys())}")

        raw_positions = portfolio.get("positions") or []

        positions = []
        for p in raw_positions:
            ticker = _extract_ticker(p)
            positions.append({
                "ticker": ticker,
                "instrument_id": p.get("instrumentID") or p.get("instrumentId"),
                "units": float(p.get("units") or p.get("amount") or 0),
                "open_rate": float(p.get("openRate") or 0),
                "current_rate": float(p.get("currentRate") or p.get("rate") or 0),
                "net_profit": float(p.get("netProfit") or p.get("profit") or 0),
                "net_profit_pct": _calc_pnl_pct(p),
                "direction": "BUY" if p.get("isBuy", True) else "SELL",
                "invested_amount": float(p.get("amount") or p.get("investedAmount") or 0),
            })

        equity = float(portfolio.get("credit") or 0)
        # total = credit + valor de posiciones abiertas
        invested = sum(p["invested_amount"] for p in positions)
        total = equity + invested

        _on_success()
        return {
            "positions": positions,
            "available_cash": equity,
            "total_value": total,
            "error": None,
        }

    except FileNotFoundError:
        msg = f"etoro_config.json no encontrado en {ETORO_CONFIG_PATH}"
        logger.warning(f"[eToro] {msg}")
        return {"positions": [], "available_cash": 0, "total_value": 0, "error": msg}
    except requests.exceptions.Timeout:
        _on_failure(0)
        return {"positions": [], "available_cash": 0, "total_value": 0, "error": "timeout"}
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        _on_failure(code)
        return {"positions": [], "available_cash": 0, "total_value": 0, "error": str(e)[:80]}
    except Exception as e:
        _on_failure(0)
        logger.warning(f"[eToro] Error obteniendo portfolio: {e}")
        return {"positions": [], "available_cash": 0, "total_value": 0, "error": str(e)[:80]}


def check_portfolio_gate(ticker: str) -> dict:
    """
    Verifica si el portfolio permite entrar en un nuevo ticker.

    Retorna:
    {
        "can_enter": bool,
        "reason": str,
        "available_cash": float,
        "suggested_position_usd": float,
        "already_in_position": bool,
        "sector_pct": float,
    }
    """
    portfolio = get_portfolio()

    if portfolio.get("error"):
        return {
            "can_enter": True,
            "reason": f"eToro sin respuesta — verificar manualmente ({portfolio['error'][:60]})",
            "available_cash": 0,
            "suggested_position_usd": 300,
            "already_in_position": False,
            "sector_pct": 0.0,
        }

    positions = portfolio["positions"]
    available = portfolio["available_cash"]
    total = portfolio["total_value"] or 1.0

    if any(p["ticker"] == ticker for p in positions):
        return {
            "can_enter": False,
            "reason": f"Ya tienes {ticker} abierto",
            "available_cash": available,
            "suggested_position_usd": 0,
            "already_in_position": True,
            "sector_pct": 0.0,
        }

    if available < 200:
        return {
            "can_enter": False,
            "reason": f"Capital insuficiente: ${available:.0f}",
            "available_cash": available,
            "suggested_position_usd": 0,
            "already_in_position": False,
            "sector_pct": 0.0,
        }

    sector = SECTOR_MAP.get(ticker, "otros")
    sector_invested = sum(
        p["invested_amount"] for p in positions
        if SECTOR_MAP.get(p["ticker"], "otros") == sector
    )
    sector_pct = sector_invested / total

    if sector_pct > 0.40:
        return {
            "can_enter": False,
            "reason": f"Sector '{sector}' ya al {sector_pct:.0%} del portafolio",
            "available_cash": available,
            "suggested_position_usd": 0,
            "already_in_position": False,
            "sector_pct": sector_pct,
        }

    suggested = min(available * 0.10, total * 0.20)
    suggested = max(suggested, 200.0)

    return {
        "can_enter": True,
        "reason": "OK",
        "available_cash": available,
        "suggested_position_usd": round(suggested, 0),
        "already_in_position": False,
        "sector_pct": sector_pct,
    }
