"""
utils/etoro_market.py — Market data (precio + velas 1m) desde la eToro public API.

Por qué: el precio "real-time" de Alpaca usa el feed IEX (~3% del volumen US), que en
pre-market/AH o tickers poco líquidos puede venir stale o no representativo. eToro da el
precio del MISMO broker donde Oscar opera → máxima fidelidad para change_pct y P&L.

READ-ONLY. Reusa credenciales de data/etoro_config.json (igual que etoro_client.py).

Diseño:
  - Mapa símbolo→instrumentId: se construye desde /market-data/instruments (filtrando
    stocks, typeID 5), se cachea en data/etoro_instruments.json (refresco semanal).
    eToro trabaja por instrumentId, NO por símbolo (probado en vivo 2026-05-31).
  - Velas 1m: /market-data/instruments/{id}/history/candles/{dir}/{interval}/{count}
    (probado: devuelve OHLCV real). Esta es la ruta que funciona; las variantes con
    ?symbols= o ?instrumentIds= dan 404.
  - Todas las funciones tienen fallback seguro (None / lista vacía) — nunca lanzan,
    para que el caller pueda caer a Alpaca sin romper el pipeline.
"""
from __future__ import annotations   # py3.9 VM: difiere anotaciones (int | None es PEP 604)

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

# Circuit breaker + detección de token expirado COMPARTIDOS con etoro_client: ambos
# pegan a la misma API con el mismo token, así que comparten el estado del circuito.
# Si el token muere (401/403), lo detecta cualquiera de los dos y se abre para ambos,
# y _on_failure dispara el SMS de "token expirado" una sola vez.
from utils.etoro_client import _circuit_ok, _on_failure, _on_success

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
ETORO_CONFIG_PATH = _DATA_DIR / "etoro_config.json"
INSTRUMENTS_CACHE = _DATA_DIR / "etoro_instruments.json"

STOCK_TYPE_ID = 5            # instrumentTypeID de acciones en eToro (verificado en vivo)
# Exchanges del listing US primario (evita los duplicados .RTH / .EUR / Xetra que eToro
# trae para el mismo símbolo). exchangeID 4 = NASDAQ, 5 = NYSE (verificado en vivo).
US_PRIMARY_EXCHANGES = {4, 5}
US_PRICE_SOURCES = {"NASDAQ", "NYSE"}
CACHE_REFRESH_DAYS = 7
_HTTP_TIMEOUT = 10

# Mapa símbolo→id en memoria (se carga del cache/red una vez por proceso)
_symbol_to_id: dict = {}
_map_loaded_at: float = 0.0


def _config() -> dict:
    with open(ETORO_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _headers(cfg: dict) -> dict:
    return {
        "x-api-key": cfg["public_key"],
        "x-user-key": cfg["user_key"],
        "x-request-id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def _base(cfg: dict) -> str:
    return cfg.get("api_base", "https://public-api.etoro.com")


# ── Mapa símbolo → instrumentId ────────────────────────────────────────────────

def _fetch_instruments_map() -> dict:
    """
    Construye {símbolo_upper: instrumentId} desde /market-data/instruments,
    filtrando acciones (typeID 5). Retorna {} si falla (el caller cae a Alpaca).
    """
    if not _circuit_ok():
        return {}   # circuito abierto (token expirado / fallos) → no insistir, usar Alpaca
    try:
        cfg = _config()
        r = requests.get(
            f"{_base(cfg)}/api/v1/market-data/instruments",
            headers=_headers(cfg),
            timeout=30,
        )
        if r.status_code in (401, 403):
            _on_failure(r.status_code)   # dispara alerta de token + abre circuito
            return {}
        r.raise_for_status()
        _on_success()
        data = r.json().get("instrumentDisplayDatas", [])
        mapping = {}
        for d in data:
            if d.get("instrumentTypeID") != STOCK_TYPE_ID:
                continue
            # Clave = symbolFull (p.ej. "NVDA"), NO instrumentDisplayName ("NVIDIA Corporation").
            # eToro trae duplicados por exchange: NVDA (NASDAQ US), NVDA.RTH, NVDA.EUR (Xetra).
            # Nos quedamos solo con el listing US primario para que el precio sea el de US.
            sym = (d.get("symbolFull") or "").upper().strip()
            iid = d.get("instrumentID")
            if not sym or not iid or "." in sym:   # ".RTH"/".EUR" → descartar
                continue
            if d.get("exchangeID") not in US_PRIMARY_EXCHANGES:
                continue
            # Primera ocurrencia gana (el listing primario aparece antes); no sobrescribir.
            mapping.setdefault(sym, iid)
        return mapping
    except Exception as e:
        logger.warning(f"[eToroMkt] no pude traer instrumentos: {e}")
        return {}


def _save_cache(mapping: dict) -> None:
    try:
        _DATA_DIR.mkdir(exist_ok=True)
        tmp = INSTRUMENTS_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "count": len(mapping), "map": mapping},
            ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(INSTRUMENTS_CACHE)
    except Exception as e:
        logger.warning(f"[eToroMkt] no pude guardar cache: {e}")


def _load_cache() -> tuple[dict, float]:
    """Retorna (map, age_days). ({}, inf) si no existe/ilegible."""
    if not INSTRUMENTS_CACHE.exists():
        return {}, float("inf")
    try:
        p = json.loads(INSTRUMENTS_CACHE.read_text(encoding="utf-8"))
        gen = datetime.strptime(p["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - gen).total_seconds() / 86400
        return p.get("map", {}), age_days
    except Exception:
        return {}, float("inf")


def get_instrument_id(symbol: str) -> int | None:
    """
    Resuelve el instrumentId de un símbolo (case-insensitive). Estrategia:
      1. memoria; 2. cache en disco si está fresco; 3. red (y refresca cache).
    Si tras refrescar el símbolo no aparece (eToro a veces devuelve subsets),
    NO sobrescribe el mapa previo con uno más chico — fusiona.
    """
    global _symbol_to_id, _map_loaded_at
    sym = (symbol or "").upper().strip()
    if not sym:
        return None

    if sym in _symbol_to_id:
        return _symbol_to_id[sym]

    # Cargar de cache si memoria vacía
    if not _symbol_to_id:
        cached, age = _load_cache()
        if cached:
            _symbol_to_id = cached
            _map_loaded_at = time.time()
            if sym in _symbol_to_id:
                return _symbol_to_id[sym]
            # cache viejo y símbolo ausente → intentar refrescar abajo
            if age < CACHE_REFRESH_DAYS:
                return None  # cache fresco pero el símbolo no existe en eToro

    # Refrescar desde red (símbolo ausente o sin cache)
    fresh = _fetch_instruments_map()
    if fresh:
        # Fusionar (no reemplazar): protege contra respuestas parciales del endpoint
        _symbol_to_id = {**_symbol_to_id, **fresh}
        _map_loaded_at = time.time()
        _save_cache(_symbol_to_id)
    return _symbol_to_id.get(sym)


# ── Velas 1m + precio ───────────────────────────────────────────────────────────

def fetch_candles_1m(symbol: str, count: int = 10) -> list:
    """
    Velas de 1 minuto más recientes (orden cronológico ascendente para encajar con
    el resto del pipeline). Cada vela: {t (ISO), o, h, l, c, v}. [] si falla.
    """
    iid = get_instrument_id(symbol)
    if not iid:
        return []
    if not _circuit_ok():
        return []   # circuito abierto → caller cae a Alpaca
    try:
        cfg = _config()
        count = max(1, min(int(count), 1000))
        url = (f"{_base(cfg)}/api/v1/market-data/instruments/{iid}"
               f"/history/candles/Desc/OneMinute/{count}")
        r = requests.get(url, headers=_headers(cfg), timeout=_HTTP_TIMEOUT)
        if r.status_code in (401, 403):
            _on_failure(r.status_code)   # token expirado → alerta + abre circuito
            return []
        r.raise_for_status()
        _on_success()
        payload = r.json()
        # Estructura: {"interval":..,"candles":[{"instrumentId":..,"candles":[{...}]}]}
        groups = payload.get("candles", [])
        raw = groups[0].get("candles", []) if groups else []
        out = []
        for c in raw:
            out.append({
                "t": c.get("fromDate"),
                "o": c.get("open"),
                "h": c.get("high"),
                "l": c.get("low"),
                "c": c.get("close"),
                "v": c.get("volume"),
            })
        out.reverse()   # Desc → Asc (más viejo primero), como las velas de Alpaca
        return out
    except Exception as e:
        logger.warning(f"[eToroMkt] velas {symbol} (id {iid}): {e}")
        return []


def fetch_price(symbol: str) -> dict:
    """
    Precio actual + change_pct desde las velas 1m de eToro (cierre de la última vela
    vs cierre de la primera del día disponible). Estructura compatible con lo que
    espera get_realtime_price. Retorna {} si falla → el caller cae a Alpaca.
    """
    candles = fetch_candles_1m(symbol, count=10)
    if not candles:
        return {}
    last = candles[-1]
    price = last.get("c")
    if not price:
        return {}
    # antigüedad de la última vela
    age_min = None
    try:
        t = datetime.fromisoformat(str(last["t"]).replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - t).total_seconds() / 60
    except Exception:
        pass
    return {
        "current_price": float(price),
        "last_trade_time": last.get("t"),
        "last_trade_age_minutes": round(age_min, 1) if age_min is not None else None,
        "candles_1m": list(reversed(candles)),   # pipeline espera desc (reciente primero)
        "source": "etoro",
    }
