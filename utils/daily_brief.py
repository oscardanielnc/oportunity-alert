"""
utils/daily_brief.py — Brief Diario de mercado (decision support, NO autopiloto).

Consolida en UN solo lugar lo que hoy está disperso (régimen, centinela macro,
fuerza de sectores, posiciones cerca de su chandelier, earnings, líderes Marea) y
deja que un modelo barato (Gemini Flash) lo REDACTE en lenguaje natural + priorice
qué merece atención hoy. La IA NO dice comprar/vender — resume y prioriza; Oscar decide.

Decisión de diseño (Oscar, 2026-06-17): cadencia DIARIA (pre-market), no horaria.
Lo horario es costo + ruido (validado: el kill-switch reactivo whipsapea, ver
research/backtest_marea_killswitch.py). Todo el dato duro es código ya existente;
la IA es solo capa de redacción y se CACHEA 1 vez por día (Lima) para no re-llamar.

build_brief()        -> dict estructurado (cero IA, best-effort, nunca lanza)
generate_narrative() -> redacta con IA y CACHEA (camino caro; solo background/manual)
regenerate()         -> build + generate_narrative, refresca cache (background AM + ALTA)
get_brief()          -> build + ÚLTIMA narrativa cacheada (visita: NO llama IA)
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

LIMA = timezone(timedelta(hours=-5))
_DATA_DIR   = Path(__file__).parent.parent / "data"
_DASH_JSON  = _DATA_DIR / "pilot_dashboard.json"
_CACHE_PATH = _DATA_DIR / "daily_brief_cache.json"


def _today_lima() -> str:
    return datetime.now(LIMA).strftime("%Y-%m-%d")


# ── Bloques de datos (cero IA) ──────────────────────────────────────────────────

def _load_dashboard() -> dict:
    try:
        return json.loads(_DASH_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _regime_block(dash: dict) -> dict:
    """Régimen macro (QQQ vs SMA200 ±3%) + sub-régimen del piloto, si existe."""
    r = dash.get("regime") or {}
    out = {
        "macro_bullish": dash.get("macro_bullish"),
        "label": r.get("label"),
        "use":   r.get("use"),
        "color": r.get("color"),
        "legend": r.get("legend"),
    }
    # Estado de flip persistido por el centinela (risk_on real recalculado)
    try:
        from utils.market_sentinel import _load_state
        st = _load_state()
        if "risk_on" in st:
            out["risk_on"] = st["risk_on"]
            out["checked_at"] = st.get("checked_at")
    except Exception:
        pass
    if out.get("risk_on") is None:
        out["risk_on"] = dash.get("macro_bullish")
    return out


def _sectors_block(dash: dict) -> dict:
    strength = dash.get("sector_strength") or []
    if not strength:
        return {}
    def _fmt(s):
        return {"name": s.get("name"), "mom_pct": s.get("mom_pct")}
    return {
        "leader":  _fmt(strength[0]),
        "laggard": _fmt(strength[-1]),
        "ranking": [_fmt(s) for s in strength],
    }


def _positions_block() -> list:
    """
    Posiciones reales con su estado de salida. Usa SOLO datos cacheados (snapshot
    de eToro en metrics + evals que precalcula el PositionTracker) → cero fetches.
    Añade distancia al chandelier y flag de earnings (Finnhub, cacheado/día).
    """
    try:
        from utils.metrics_store import MetricsStore
        from utils.position_strategy import resolve_strategy, read_account_cache
        rows = MetricsStore().get_latest_positions()
    except Exception as e:
        logger.debug(f"[DailyBrief] posiciones: {e}")
        return []

    evals = (read_account_cache() or {}).get("evals", {})
    out = []
    for r in rows:
        tk = r.get("ticker")
        if not tk:
            continue
        ev = evals.get(tk, {})
        cur = r.get("current_rate") or 0.0
        stop = ev.get("stop_price")
        stop_dist = None
        if cur and stop:
            stop_dist = round((cur - stop) / cur * 100, 1)
        try:
            strat = resolve_strategy(tk)["strategy"]
        except Exception:
            strat = "manual"
        item = {
            "ticker":         tk,
            "pnl_pct":        r.get("net_profit_pct"),
            "current":        cur,
            "strategy":       strat,
            "exit_signal":    ev.get("exit", False),
            "exit_reason":    ev.get("reason", ""),
            "detail":         ev.get("detail", ""),
            "stop_price":     stop,
            "stop_dist_pct":  stop_dist,
            "days_held":      ev.get("days_held"),
            "intraday_breach": ev.get("intraday_breach", False),
        }
        try:
            from utils.earnings_calendar import has_earnings_today_or_yesterday
            item["earnings"] = has_earnings_today_or_yesterday(tk)
        except Exception:
            item["earnings"] = False
        out.append(item)
    return out


def _opportunities_block(dash: dict, held: set, limit: int = 5) -> list:
    """Líderes Marea de hoy que NO están en cartera (lo que Marea rankea y aún no tienes)."""
    out = []
    for l in (dash.get("leaders") or []):
        tk = (l.get("ticker") or "").upper()
        if not tk or tk in held or l.get("held"):
            continue
        if not (l.get("is_top") or l.get("is_breakout")):
            continue
        out.append({
            "ticker":       tk,
            "rank":         l.get("rank"),
            "is_top":       bool(l.get("is_top")),
            "is_breakout":  bool(l.get("is_breakout")),
            "sector_name":  l.get("sector_name"),
            "sector_rank":  l.get("sector_rank"),
            "sector_total": l.get("sector_total"),
            "sector_favor": bool(l.get("sector_favor")),
            "size_pct":     l.get("size_pct"),
        })
        if len(out) >= limit:
            break
    return out


def _sentinel_block(positions: list) -> dict:
    """Triggers macro EN VIVO (QQQ/SPY hoy, ret5d, breadth). Best-effort."""
    try:
        from utils.market_sentinel import check_market_risk
        held = [
            {"ticker": p["ticker"], "current": p.get("current"), "stop": p.get("stop_price")}
            for p in positions if p.get("current") and p.get("stop_price")
        ]
        res = check_market_risk(held=held)
        return res or {"triggers": [], "at_risk": [], "severity": 0}
    except Exception as e:
        logger.debug(f"[DailyBrief] sentinel: {e}")
        return {"triggers": [], "at_risk": [], "severity": 0}


def build_brief() -> dict:
    """Ensambla el brief estructurado (sin IA). Best-effort: nunca lanza."""
    dash = _load_dashboard()
    positions = _positions_block()
    held = {p["ticker"].upper() for p in positions}
    return {
        "as_of":         datetime.now(LIMA).strftime("%Y-%m-%d %H:%M"),
        "dashboard_age": dash.get("as_of") or dash.get("updated"),
        "regime":        _regime_block(dash),
        "sentinel":      _sentinel_block(positions),
        "sectors":       _sectors_block(dash),
        "positions":     positions,
        "opportunities": _opportunities_block(dash, held),
    }


# ── Capa de redacción (Gemini Flash, cacheada por día) ──────────────────────────

_SYSTEM = (
    "Eres un asistente de briefing de mercado para Oscar, un inversor que opera swing "
    "trading (estrategia Marea, tendencia de semanas) y eventos de corto plazo. NO das "
    "órdenes de comprar ni vender — resumes la situación y señalas qué merece atención "
    "hoy para que Oscar decida. Eres conciso, directo y honesto: si no hay nada urgente, "
    "lo dices. Respondes SIEMPRE en español. Tu salida es JSON."
)


def _build_prompt(brief: dict) -> str:
    reg = brief.get("regime", {})
    sec = brief.get("sectors", {})
    sen = brief.get("sentinel", {})
    parts = ["Datos de hoy:"]
    parts.append(f"- Régimen macro: {'risk-on (alcista)' if reg.get('risk_on') else 'risk-off (defensivo)'}"
                 + (f" — {reg.get('label')}" if reg.get('label') else ""))
    if sen.get("triggers"):
        parts.append(f"- Centinela macro DISPARADO: {'; '.join(sen['triggers'])}")
    else:
        parts.append("- Centinela macro: sin alertas de riesgo hoy.")
    if sec.get("leader"):
        parts.append(f"- Sector líder: {sec['leader']['name']} ({sec['leader']['mom_pct']:+.0f}%); "
                     f"más débil: {sec['laggard']['name']} ({sec['laggard']['mom_pct']:+.0f}%).")
    if brief.get("positions"):
        parts.append("- Tus posiciones:")
        for p in brief["positions"]:
            bits = [f"  · {p['ticker']} ({p['strategy']})"]
            if p.get("pnl_pct") is not None:
                bits.append(f"P&L {p['pnl_pct']:+.1f}%")
            if p.get("stop_dist_pct") is not None:
                bits.append(f"a {p['stop_dist_pct']:.1f}% del stop")
            if p.get("exit_signal"):
                bits.append(f"⚠ SEÑAL DE SALIDA: {p.get('exit_reason')}")
            if p.get("intraday_breach"):
                bits.append("precio bajo el stop intradía (regla confirma al cierre)")
            if p.get("earnings"):
                bits.append("earnings reciente")
            parts.append(", ".join(bits))
    else:
        parts.append("- Sin posiciones abiertas.")
    if brief.get("opportunities"):
        opps = ", ".join(f"{o['ticker']} (#{o.get('rank')}{'/breakout' if o.get('is_breakout') else ''})"
                         for o in brief["opportunities"])
        parts.append(f"- Líderes Marea fuera de cartera: {opps}")
    parts.append(
        "\nDevuelve JSON con dos campos: "
        '{"resumen": "2-4 frases sobre el panorama y tus posiciones", '
        '"atencion": ["punto accionable 1", "punto 2", ...]}. '
        "Máximo 4 puntos de atención, los más importantes primero. Si no hay nada que "
        "vigilar, devuelve atencion vacío y dilo en el resumen."
    )
    return "\n".join(parts)


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(payload: dict) -> None:
    try:
        _DATA_DIR.mkdir(exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception as e:
        logger.warning(f"[DailyBrief] no pude cachear narrativa: {e}")


def _read_cached_narrative() -> dict | None:
    """Narrativa cacheada (la última generada, sea de hoy o de días previos). None si nunca se generó."""
    cache = _load_cache()
    if cache.get("resumen"):
        return {k: cache[k] for k in ("resumen", "atencion", "generated_at", "engine", "date")
                if k in cache}
    return None


def generate_narrative(brief: dict) -> dict:
    """
    Redacta el brief con la IA y CACHEA el resultado. Esta función SIEMPRE llama a la
    IA (es el camino caro) — solo debe invocarse desde el background (loop AM + evento
    ALTA) o desde el botón manual "regenerar". Las visitas a la página NO la llaman:
    sirven _read_cached_narrative(). Cae a un resumen por código si la IA no responde.
    """
    today = _today_lima()
    import os as _os
    # Motor BARATO (el brief es 1 llamada/día — Oscar pidió modelo barato, 2026-06-17):
    #   - engine: DAILY_BRIEF_ENGINE > AI_ENGINE del sistema > gemini.
    #   - si el motor es claude, forzamos Haiku (no el Sonnet del scorer de noticias).
    #   - gemini-2.0-flash ya es gratis. Override fino con DAILY_BRIEF_MODEL.
    # NO forzamos un motor sin key: si la IA no responde, cae a resumen por código.
    eng = (_os.environ.get("DAILY_BRIEF_ENGINE")
           or _os.environ.get("AI_ENGINE", "gemini")).lower()
    model = _os.environ.get("DAILY_BRIEF_MODEL")
    if eng == "claude" and not model:
        model = "claude-haiku-4-5-20251001"
    used_engine = f"{eng}:{model}" if model else eng

    text = None
    try:
        from utils.ai_client import call_ai, parse_json_response
        text = call_ai(_build_prompt(brief), system=_SYSTEM, engine=eng, model=model,
                       max_tokens=600, temperature=0.2)
        data = parse_json_response(text) or {}
    except Exception as e:
        logger.warning(f"[DailyBrief] IA no disponible: {e}")
        data = {}

    resumen = (data.get("resumen") or "").strip()
    atencion = data.get("atencion") or []
    if not isinstance(atencion, list):
        atencion = [str(atencion)]

    if not resumen:
        resumen, atencion = _fallback_narrative(brief)
        engine = "código (IA no disponible)"
    else:
        engine = used_engine

    payload = {
        "date": today,
        "resumen": resumen,
        "atencion": atencion[:4],
        "generated_at": datetime.now(LIMA).strftime("%Y-%m-%d %H:%M"),
        "engine": engine,
    }
    _save_cache(payload)
    return {k: payload[k] for k in ("resumen", "atencion", "generated_at", "engine", "date")}


def _fallback_narrative(brief: dict) -> tuple:
    """Resumen mínimo por código si Gemini no responde — sin texto bonito, pero útil."""
    reg = brief.get("regime", {})
    sen = brief.get("sentinel", {})
    resumen = ("Régimen " + ("risk-on (alcista)." if reg.get("risk_on") else "risk-off (defensivo)."))
    aten = []
    if sen.get("triggers"):
        aten.append("Centinela macro disparado: " + "; ".join(sen["triggers"]))
    for p in brief.get("positions", []):
        if p.get("exit_signal"):
            aten.append(f"{p['ticker']}: señal de salida ({p.get('exit_reason')}).")
        elif p.get("stop_dist_pct") is not None and p["stop_dist_pct"] < 5:
            aten.append(f"{p['ticker']} a {p['stop_dist_pct']:.1f}% de su stop.")
    return resumen, aten[:4]


# Cache en proceso del brief completo. El dashboard auto-refresca cada 60s; sin esto,
# tener la tab abierta dispararía los fetches en vivo del centinela (QQQ/SPY) cada minuto.
# Un brief es pre-market, no un ticker → 5 min de frescura sobran. force lo bypassa.
_STRUCT_TTL = 300
_struct_cache: dict = {"ts": 0.0, "payload": None}


def get_brief(force_narrative: bool = False) -> dict:
    """
    Brief para el endpoint (cada visita). Por defecto NO llama a la IA: ensambla los
    datos duros (cero IA) y adjunta la ÚLTIMA narrativa cacheada — la que generó el
    background (loop AM o evento ALTA). Así abrir la página es instantáneo y gratis;
    se lee la generación previa durante todo el día hasta que el background la renueve.

    force_narrative=True (botón manual "regenerar") SÍ dispara una generación on-demand.
    """
    if force_narrative:
        return regenerate(reason="manual (botón regenerar)")

    # Camino normal (visita): datos frescos + narrativa cacheada. Struct-cache 5 min
    # para no re-disparar los fetches en vivo del centinela (QQQ/SPY) en cada refresh.
    if (_struct_cache["payload"] is not None
            and time.time() - _struct_cache["ts"] < _STRUCT_TTL):
        return _struct_cache["payload"]
    brief = build_brief()
    cached = _read_cached_narrative()
    brief["narrative"] = cached or {
        "resumen": "", "atencion": [], "generated_at": None, "engine": None, "pending": True,
    }
    _struct_cache["ts"] = time.time()
    _struct_cache["payload"] = brief
    return brief


def regenerate(reason: str = "") -> dict:
    """
    Regenera la narrativa con IA y refresca el struct-cache. Punto de entrada del
    background (loop AM + evento ALTA) y del botón manual. Best-effort: nunca lanza.
    """
    brief = build_brief()
    try:
        brief["narrative"] = generate_narrative(brief)
        logger.info(f"[DailyBrief] narrativa regenerada ({reason}) → "
                    f"{brief['narrative'].get('generated_at')} [{brief['narrative'].get('engine')}]")
    except Exception as e:
        logger.warning(f"[DailyBrief] regenerate falló ({reason}): {e}")
        brief["narrative"] = _read_cached_narrative() or {
            "resumen": "", "atencion": [], "generated_at": None, "engine": None, "pending": True,
        }
    _struct_cache["ts"] = time.time()
    _struct_cache["payload"] = brief
    return brief


def has_narrative_today() -> bool:
    """True si ya existe narrativa cacheada generada HOY (Lima). Lo usa el loop AM."""
    cache = _load_cache()
    return bool(cache.get("resumen") and cache.get("date") == _today_lima())
