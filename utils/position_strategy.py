"""
utils/position_strategy.py — Atribución de estrategia + salida por estrategia
para posiciones REALES de eToro. READ-ONLY respecto a eToro (solo aconseja).

Modelo HÍBRIDO (decisión Oscar 2026-05-30):
  1. Override manual desde el dashboard      -> data/position_tags.json   (gana)
  2. Auto-match contra el piloto por ticker  -> pilot_state.json["positions"][tk]["source"]
  3. Sin override ni match                    -> "manual" (solo contexto de noticias)

Salida por estrategia (MISMA matemática que el backtest validado; reusa pilot/):
  - marea  -> chandelier: stop = hh_desde_entrada - 4*ATR22; salir si el ÚLTIMO CIERRE < stop
              (evalúa sobre cierre diario, igual que run_pilot; no whipsaw intradía).
  - ped    -> tiempo: salir cuando days_held >= 6 (≈ Day+7 close).
  - manual -> nunca dispara salida (exit=False); el tracker solo explica movimientos.

Eficiencia: las barras diarias se cachean por (ticker, fecha) — a lo sumo ~1 fetch
Alpaca por ticker por día, no por cada ciclo de 10 min.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR     = Path(__file__).parent.parent / "data"
TAGS_PATH     = _DATA_DIR / "position_tags.json"
PILOT_STATE   = _DATA_DIR / "pilot_state.json"
ACCOUNT_CACHE = _DATA_DIR / "account_cache.json"

VALID_STRATEGIES = {"marea", "ped", "manual"}

# Cache de barras diarias: { ticker: (fecha_iso, [bars]) }
_bars_cache: dict = {}


# ── Tags (override manual) ──────────────────────────────────────────────────────

def load_tags() -> dict:
    """{ ticker: {"strategy": str, "note": str, "updated_at": iso} }"""
    if not TAGS_PATH.exists():
        return {}
    try:
        return json.loads(TAGS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[PosStrategy] position_tags.json ilegible: {e}")
        return {}


def _atomic_write(path: Path, payload) -> None:
    """Escritura atómica (tmp + replace) — lectores concurrentes nunca ven JSON parcial."""
    _DATA_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def save_tags(tags: dict) -> None:
    # El dashboard escribe y el tracker lee concurrentemente.
    _atomic_write(TAGS_PATH, tags)


# ── Cache de cuenta (lo escribe el PositionTracker, lo lee el dashboard/API) ─────
# Evita que /api/health y /api/positions llamen a eToro en cada request.

def write_account_cache(available_cash: float, total_value: float, evals: dict,
                        positions: list = None) -> None:
    """
    Persiste cash + valor total + evaluaciones de salida del último ciclo del tracker.
    `positions`: subset mínimo (ticker + invested_amount) para que el Gate 5 de noticias
    lo lea sin llamar a eToro en vivo. Si es None, no se escribe la clave (el lector
    cae a live). Pasar [] explícitamente cuando el portafolio está flat.
    """
    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "available_cash": available_cash,
        "total_value": total_value,
        "evals": evals,
    }
    if positions is not None:
        payload["positions"] = [
            {"ticker": p.get("ticker"), "invested_amount": p.get("invested_amount", 0)}
            for p in positions
        ]
    _atomic_write(ACCOUNT_CACHE, payload)


def read_account_cache() -> dict:
    if not ACCOUNT_CACHE.exists():
        return {}
    try:
        return json.loads(ACCOUNT_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def account_cache_age_minutes() -> "float | None":
    """Antigüedad del cache en minutos, o None si no existe/ilegible."""
    cache = read_account_cache()
    ts = cache.get("ts")
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return None


def set_tag(ticker: str, strategy: str, note: str = "") -> bool:
    """Override manual de estrategia para un ticker. strategy in {marea,ped,manual}."""
    ticker = ticker.upper().strip()
    strategy = strategy.lower().strip()
    if strategy not in VALID_STRATEGIES:
        return False
    tags = load_tags()
    tags[ticker] = {
        "strategy": strategy,
        "note": note,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_tags(tags)
    logger.info(f"[PosStrategy] Override {ticker} -> {strategy}")
    return True


def clear_tag(ticker: str) -> None:
    tags = load_tags()
    if tags.pop(ticker.upper().strip(), None) is not None:
        save_tags(tags)


# ── Piloto (auto-match) ─────────────────────────────────────────────────────────

def _pilot_positions() -> dict:
    """{ ticker: {source, entry_date, hh, entry_price} } del portafolio papel."""
    if not PILOT_STATE.exists():
        return {}
    try:
        return json.loads(PILOT_STATE.read_text(encoding="utf-8")).get("positions", {})
    except Exception:
        return {}


# ── Resolución híbrida ──────────────────────────────────────────────────────────

_pilot_scope: set = None


def _in_pilot_scope(ticker: str) -> bool:
    """
    ¿El ticker está en el alcance operativo del Piloto? = universo Marea (top-80 liquidez)
    ∪ MEGA_CAP_PED. Sirve para auto-atribuir a 'marea' las posiciones reales que vienen de
    recomendaciones del Piloto pero NO están en el paper portfolio (caso típico: el paper
    simula otros nombres, o el ticker salió del top-80 como ARM que vive en MEGA_CAP_PED).
    """
    global _pilot_scope
    if _pilot_scope is None:
        scope = set()
        try:
            from pilot.universe import load_universe
            scope |= set(load_universe())
        except Exception:
            pass
        try:
            from pilot.ped_signals import MEGA_CAP_PED
            scope |= set(MEGA_CAP_PED)
        except Exception:
            pass
        _pilot_scope = scope
    return ticker in _pilot_scope


def resolve_strategy(ticker: str) -> dict:
    """
    Determina la estrategia de una posición real.
    Retorna {"strategy": "marea"|"ped"|"manual", "origin": "override"|"pilot"|"universe"|"default"}.
    """
    ticker = ticker.upper().strip()

    tag = load_tags().get(ticker)
    if tag and tag.get("strategy") in VALID_STRATEGIES:
        return {"strategy": tag["strategy"], "origin": "override"}

    pilot = _pilot_positions().get(ticker)
    if pilot:
        # Presencia en el piloto = posición del sistema. PED se marca explícito;
        # todo lo demás (incl. estado viejo sin 'source') es Marea por defecto.
        src = pilot.get("source", "marea")
        return {"strategy": src if src in ("marea", "ped") else "marea", "origin": "pilot"}

    # No está en el paper, pero SÍ en el alcance del Piloto → Marea por defecto (estrategia
    # dominante). Si fue una entrada PED, Oscar lo corrige con el override (ahora persiste).
    if _in_pilot_scope(ticker):
        return {"strategy": "marea", "origin": "universe"}

    return {"strategy": "manual", "origin": "default"}


def _entry_date(ticker: str, open_datetime: str = "") -> str:
    """
    Fecha de entrada (YYYY-MM-DD). Prioriza el openDateTime REAL de eToro;
    cae al entry_date del piloto si falta.
    """
    if open_datetime:
        return open_datetime[:10]
    pilot = _pilot_positions().get(ticker.upper().strip())
    if pilot and pilot.get("entry_date"):
        return str(pilot["entry_date"])[:10]
    return ""


# ── Barras diarias (cacheadas por día) ──────────────────────────────────────────

def _get_bars(ticker: str) -> list:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cached = _bars_cache.get(ticker)
    if cached and cached[0] == today:
        return cached[1]
    try:
        from pilot.momentum_signals import fetch_daily_batch
        bars = fetch_daily_batch([ticker]).get(ticker, [])
    except Exception as e:
        logger.warning(f"[PosStrategy] No pude traer barras de {ticker}: {e}")
        bars = []
    _bars_cache[ticker] = (today, bars)
    return bars


def _days_held(bars: list, entry_date: str) -> int:
    """Días hábiles transcurridos desde la entrada (robusto si entry_date no es día hábil)."""
    if not entry_date:
        return 0
    return sum(1 for b in bars if b["t"] > entry_date)


# ── Evaluación de salida ────────────────────────────────────────────────────────

def evaluate_exit(ticker: str, strategy: str, entry_date: str,
                  current_price: float = 0.0) -> dict:
    """
    Decide si la posición debe cerrarse según su estrategia.

    Retorna:
      {
        "exit": bool,            # True = recomendar VENDER
        "reason": str,           # etiqueta corta de la regla
        "detail": str,           # texto para el SMS / dashboard
        "stop_price": float|None,# chandelier (solo marea)
        "days_held": int|None,   # solo ped
      }
    """
    from pilot.momentum_signals import _atr, chandelier_stop, MULT, ATR_N

    base = {"exit": False, "reason": "", "detail": "", "stop_price": None,
            "days_held": None, "intraday_breach": False}

    if strategy == "manual":
        base["reason"] = "manual"
        base["detail"] = "Posición manual — sin salida automática (solo contexto de noticias)."
        return base

    bars = _get_bars(ticker)
    if not bars:
        base["reason"] = "sin_datos"
        base["detail"] = "Sin barras Alpaca — no se pudo evaluar la salida."
        return base

    if strategy == "ped":
        from pilot.ped_signals import PED_HOLD_DAYS
        held = _days_held(bars, entry_date)
        base["days_held"] = held
        if held >= PED_HOLD_DAYS:
            base["exit"] = True
            base["reason"] = "ped_time"
            base["detail"] = (
                f"PED cumplió el hold ({held} días háb. ~ Day+{held+1}). "
                "Salida por tiempo — cerrar al próximo open."
            )
        else:
            base["reason"] = "ped_hold"
            base["detail"] = f"PED en curso — día {held}/{PED_HOLD_DAYS}. Mantener."
        return base

    # strategy == "marea" -> chandelier sobre cierre diario
    atr = _atr(bars)
    if atr is None:
        base["reason"] = "sin_atr"
        base["detail"] = "ATR insuficiente — no se pudo calcular el chandelier."
        return base

    hh = max((b["h"] for b in bars if not entry_date or b["t"] >= entry_date), default=None)
    if hh is None:
        # Posición abierta HOY (o entrada 24/5 cuya fecha UTC es posterior a la última
        # barra): todavía no existe barra diaria >= entry_date. Usar el precio actual /
        # último cierre como hh provisional. NUNCA caer al máximo de todo el histórico
        # (520d): si el ticker venía lejos de su máximo de 52 semanas, eso disparaba un
        # "Chandelier roto" falso el día 0 de la posición.
        hh = max(current_price or 0, bars[-1]["c"])
    stop = chandelier_stop(hh, atr)
    last_close = bars[-1]["c"]
    base["stop_price"] = round(stop, 2)

    if last_close < stop:
        base["exit"] = True
        base["reason"] = "chandelier"
        base["detail"] = (
            f"Chandelier roto: cierre ${last_close:.2f} < stop ${stop:.2f} "
            f"(pico ${hh:.2f} − {MULT:g}×ATR{ATR_N}). Cerrar al próximo open."
        )
    else:
        margin = (last_close - stop) / last_close * 100 if last_close else 0
        base["reason"] = "chandelier_ok"
        base["detail"] = (
            f"Marea OK — cierre ${last_close:.2f}, stop ${stop:.2f} "
            f"(margen {margin:.1f}%). Dejar correr."
        )
        # AVISO INTRADÍA (informativo, 2026-06-10): la regla validada decide al CIERRE,
        # pero el tracker tiene el precio eToro en vivo cada 10 min. Si el precio ACTUAL
        # ya perforó el chandelier, marcarlo — main.py manda un SMS informativo ("la regla
        # confirma al cierre; eToro 24/5 te deja salir ya si decidís"). NO cambia exit:
        # la vigilancia intradía como REGLA se midió y rechazó; esto solo informa.
        if current_price and current_price < stop:
            base["intraday_breach"] = True
            base["detail"] = (
                f"⚠️ Precio ACTUAL ${current_price:.2f} bajo el stop ${stop:.2f} "
                f"(cierre previo ${last_close:.2f} aún arriba). La regla confirma al "
                f"CIERRE — vigilar."
            )
    return base


def evaluate_position(ticker: str, open_datetime: str = "",
                      current_price: float = 0.0) -> dict:
    """
    Conveniencia: resuelve estrategia + entry_date + salida en un solo paso.
    Retorna {strategy, origin, entry_date, exit, reason, detail, stop_price, days_held}.
    """
    res = resolve_strategy(ticker)
    entry_date = _entry_date(ticker, open_datetime)
    ev = evaluate_exit(ticker, res["strategy"], entry_date, current_price)
    return {**res, "entry_date": entry_date, **ev}
