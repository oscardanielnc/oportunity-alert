"""
eToro API — wrapper READ-ONLY.
Lee credenciales desde data/etoro_config.json (dentro del proyecto).
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

ETORO_CONFIG_PATH = Path(__file__).parent.parent / "data" / "etoro_config.json"

# ── Circuit breaker ───────────────────────────────────────────────────────────
CIRCUIT_OPEN_THRESHOLD = 5      # fallos consecutivos antes de abrir el circuito
CIRCUIT_PAUSE_MINUTES  = 30     # minutos de pausa cuando el circuito está abierto

_cb_failures   = 0              # contador de fallos consecutivos
_cb_open_until = 0.0            # timestamp hasta el que el circuito está abierto
# 2026-06-10: era un bool one-shot — si Oscar dormía el SMS, el sistema quedaba sin
# monitoreo de posiciones por DÍAS sin re-aviso. Ahora: re-aviso cada 24h mientras
# el 401/403 persista.
_token_alert_last_ts = 0.0      # último SMS de token expirado (epoch)
_TOKEN_REALERT_HOURS = 24

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
    global _cb_failures, _cb_open_until, _token_alert_last_ts

    _cb_failures += 1
    logger.warning(f"[eToro] Fallo #{_cb_failures} (HTTP {status_code})")

    if status_code in (401, 403):
        msg = "token_expired"
        if time.time() - _token_alert_last_ts > _TOKEN_REALERT_HOURS * 3600:
            _notify_token_expired()
            _token_alert_last_ts = time.time()
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
    """Avisa cuando el token de eToro expira. Usa el sender compartido (respeta
    NOTIFICATION_CHANNEL: callmebot/whatsapp/sms, con fallback)."""
    try:
        import os
        to = os.environ.get("TWILIO_TO", "")
        if not to:
            logger.error("[eToro] Token expirado — no hay destinatario (TWILIO_TO) para notificar")
            return
        body = (
            "ALERTA OportunityAlert:\n"
            "Token eToro expirado (401/403).\n"
            "Accion requerida:\n"
            "1. Abre etoro_config.json\n"
            "2. Reemplaza user_key con nuevo token de sesion\n"
            "El sistema sigue funcionando sin eToro (gates 1-3 activos)."
        )
        from alerts.twilio_sms import send_raw_message
        if send_raw_message(body, to):
            logger.error("[eToro] Aviso de token expirado enviado a Oscar")
        else:
            logger.error("[eToro] No pudo enviarse el aviso de token expirado")
    except Exception as e:
        logger.error(f"[eToro] No pudo notificar token expirado: {e}")


def reset_token_alert():
    """Llamar después de actualizar etoro_config.json con nuevo token."""
    global _token_alert_last_ts, _cb_failures, _cb_open_until
    _token_alert_last_ts = 0.0
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
        r = requests.get(f"{base}/api/v1/trading/info/portfolio", headers=headers, timeout=5)
        if r.status_code == 200:
            return {"ok": True, "status": "online", "failures": _cb_failures}
        return {"ok": False, "status": f"HTTP {r.status_code}", "failures": _cb_failures}
    except Exception as e:
        return {"ok": False, "status": str(e)[:60], "failures": _cb_failures}


_INVALID_TICKERS = {"UNKNOWN", "UNKNOW", "N/A", "NULL", "NONE", ""}

def _extract_ticker(pos: dict) -> str:
    for field in ["ticker", "symbol", "instrumentName", "Instrument"]:
        val = pos.get(field, "")
        if val and len(val) <= 6 and str(val).isupper() and str(val) not in _INVALID_TICKERS:
            return str(val)
    raw = pos.get("instrumentName", pos.get("Instrument", "UNKNOWN"))
    candidate = str(raw)[:6].upper().replace(" ", "")
    # Si el nombre completo es algo como "Unknown" o empieza con caracteres no-alpha,
    # retornar "UNKNOWN" explícitamente para que el position tracker lo filtre.
    if not candidate.isalpha() or candidate in _INVALID_TICKERS:
        return "UNKNOWN"
    return candidate


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

        # Endpoint confirmado (doc oficial): /api/v1/trading/info/portfolio
        # base = https://public-api.etoro.com  (NO api.etoro.com, NO /{env}/)
        resp = requests.get(
            f"{base}/api/v1/trading/info/portfolio",
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

        # El endpoint de portfolio de eToro trae cada posición con instrumentID NUMÉRICO
        # y SIN símbolo ni currentRate (verificado en vivo 2026-06-01). Por eso:
        #   1. el ticker se resuelve por instrumentID (mapa inverso de etoro_market);
        #   2. el precio actual se trae por id (para valor + P&L reales);
        #   3. NUNCA se descarta una posición — su `amount` debe contar en el equity total
        #      (descartar posiciones colapsaba el equity al cash → falso -90%).
        try:
            from utils.etoro_market import get_symbol_for_id, fetch_price_by_id
        except Exception:
            get_symbol_for_id = lambda _iid: None
            fetch_price_by_id = lambda _iid: {}

        positions = []
        positions_value = 0.0
        for p in raw_positions:
            iid = p.get("instrumentID") or p.get("instrumentId")
            ticker = _extract_ticker(p)
            if ticker in _INVALID_TICKERS:
                resolved = get_symbol_for_id(iid) if iid else None
                ticker = resolved or (f"ID:{iid}" if iid else "UNKNOWN")

            amount    = float(p.get("amount") or p.get("initialAmountInDollars") or p.get("investedAmount") or 0)
            units     = float(p.get("units") or 0)
            open_rate = float(p.get("openRate") or 0)
            is_buy    = bool(p.get("isBuy", True))

            # Precio actual por id → valor + P&L reales (el portfolio no los trae)
            current_rate = open_rate
            try:
                pr = fetch_price_by_id(iid) if iid else {}
                if pr.get("current_price"):
                    current_rate = float(pr["current_price"])
            except Exception:
                pass

            current_value = units * current_rate if (units and current_rate) else amount
            net_profit    = current_value - amount
            pnl_pct = ((current_rate / open_rate - 1) * 100) if open_rate else 0.0
            if not is_buy:
                pnl_pct = -pnl_pct

            positions_value += current_value
            positions.append({
                "ticker": ticker,
                "instrument_id": iid,
                "units": units,
                "open_rate": open_rate,
                "current_rate": current_rate,
                "net_profit": round(net_profit, 2),
                "net_profit_pct": round(pnl_pct, 2),
                "direction": "BUY" if is_buy else "SELL",
                "invested_amount": amount,
                "current_value": round(current_value, 2),
                # openDateTime (ISO) -> fecha de entrada real, base para Day+7 (PED) y hh (Marea)
                "open_datetime": str(p.get("openDateTime") or p.get("OpenDateTime") or ""),
            })

        cash = float(portfolio.get("credit") or 0)
        total = cash + positions_value   # equity real = efectivo + valor actual de posiciones

        _on_success()
        return {
            "positions": positions,
            "available_cash": cash,
            "total_value": round(total, 2),
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


def check_portfolio_gate(ticker: str, portfolio: dict = None) -> dict:
    """
    Verifica si el portfolio permite entrar en un nuevo ticker.

    `portfolio`: si se pasa (p.ej. desde account_cache.json que mantiene el
    PositionTracker), evita una llamada sincrónica a eToro. Si es None, consulta
    eToro en vivo (comportamiento original). El dict debe traer al menos
    `positions` (con ticker + invested_amount), `available_cash` y `total_value`.

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
    if portfolio is None:
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
