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


# Cache por día UTC del radar de sectores (1 fetch batched de ETFs/día).
_radar_cache: dict = {"day": None, "data": None}


def _sector_radar_block(positions: list) -> dict:
    """
    Radar FORWARD-LOOKING por sector (lo que faltaba el día del crash de semis): el
    momentum 126d de sector_strength es lentísimo (SMH seguía +88% mientras ya caía).
    Aquí miramos la señal CORTA de cada ETF de sector — ret_1d, ret_5d y posición vs
    EMA20 — para detectar sectores que se están DANDO VUELTA *antes* de que el índice
    entero ceda, y cruzarlo con tu exposición de cartera (contagio).

    Devuelve {"sectors": [...], "exposure": {etf: {n, tickers, pnl_avg}}, "alerts": [str]}.
    1 fetch batched de los ETFs/día (cacheado). Best-effort: nunca lanza.
    """
    try:
        from pilot.star_score import SECTOR_ETFS, SECTOR_NAME, SECTOR_ETF
        from pilot.momentum_signals import fetch_daily_batch, _ema
    except Exception as e:
        logger.debug(f"[DailyBrief] radar import: {e}")
        return {}

    # Exposición de cartera por sector (a qué sectores estás expuesto y cuánto P&L hay en juego)
    exposure: dict = {}
    for p in positions:
        tk = (p.get("ticker") or "").upper()
        etf = SECTOR_ETF.get(tk)
        if not etf:
            continue
        e = exposure.setdefault(etf, {"n": 0, "tickers": [], "pnls": []})
        e["n"] += 1
        e["tickers"].append(tk)
        if p.get("pnl_pct") is not None:
            e["pnls"].append(p["pnl_pct"])
    for e in exposure.values():
        e["pnl_avg"] = round(sum(e["pnls"]) / len(e["pnls"]), 1) if e["pnls"] else None
        e.pop("pnls", None)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _radar_cache["day"] == today and _radar_cache["data"] is not None:
        sectors = _radar_cache["data"]
    else:
        sectors = []
        try:
            bars_by_etf = fetch_daily_batch(SECTOR_ETFS, lookback_days=60)
            for etf in SECTOR_ETFS:
                bars = bars_by_etf.get(etf) or []
                closes = [b["c"] for b in bars if b.get("c")]
                if len(closes) < 21:
                    continue
                last = closes[-1]
                ret_1d = round((last / closes[-2] - 1) * 100, 1) if closes[-2] else None
                ret_5d = round((last / closes[-6] - 1) * 100, 1) if len(closes) >= 6 and closes[-6] else None
                ema20 = _ema(closes, 20)
                vs_ema20 = round((last / ema20 - 1) * 100, 1) if ema20 else None
                # "Dándose vuelta": cae fuerte en 5d, o ya perdió la EMA20 con momentum 5d negativo
                rolling = (ret_5d is not None and ret_5d <= -2.5) or \
                          (vs_ema20 is not None and vs_ema20 < 0 and (ret_5d or 0) < 0)
                sectors.append({
                    "etf": etf, "name": SECTOR_NAME.get(etf, etf),
                    "ret_1d": ret_1d, "ret_5d": ret_5d, "vs_ema20": vs_ema20,
                    "rolling_over": bool(rolling),
                })
            sectors.sort(key=lambda s: (s["ret_5d"] if s["ret_5d"] is not None else 0))
            _radar_cache["day"] = today
            _radar_cache["data"] = sectors
        except Exception as e:
            logger.debug(f"[DailyBrief] radar sectores: {e}")
            sectors = []

    # Alertas de contagio: sector dándose vuelta DONDE tienes posiciones = lo más accionable
    alerts = []
    for s in sectors:
        if not s["rolling_over"]:
            continue
        exp = exposure.get(s["etf"])
        r5 = s["ret_5d"]
        r5txt = f"{r5:+.1f}% en 5d" if r5 is not None else "debilitándose"
        ema_txt = " y bajo su EMA20" if (s.get("vs_ema20") is not None and s["vs_ema20"] < 0) else ""
        if exp:
            alerts.append(
                f"{s['name']} ({s['etf']}) {r5txt}{ema_txt} — tienes "
                f"{exp['n']} posición(es) ahí ({', '.join(exp['tickers'])}); riesgo de contagio."
            )
        else:
            alerts.append(f"{s['name']} ({s['etf']}) {r5txt}{ema_txt} — sector girando a la baja (sin exposición tuya).")

    return {"sectors": sectors, "exposure": exposure, "alerts": alerts}


def _market_movers_block(hours: int = 24, limit: int = 4) -> list:
    """Declaraciones recientes de market movers (Trump/Fed/Treasury/SEC) con impacto de mercado,
    para que el brief avise si conviene CERRAR posiciones antes de una caída. Prioriza las
    bajistas/mixtas y las macro (mueven todo el mercado)."""
    from datetime import timedelta
    try:
        from utils.trump_tracker import get_feed
        feed = get_feed(limit=20)
        items = feed.get("items", []) if isinstance(feed, dict) else (feed or [])
        cutoff = datetime.now(LIMA) - timedelta(hours=hours)
        out = []
        for it in items:
            try:
                ts = datetime.fromisoformat(it["ts"]) if it.get("ts") else None
                if ts and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=LIMA)
            except Exception:
                ts = None
            if ts and ts < cutoff:
                continue
            out.append({"source": it.get("source"), "impacto": it.get("impacto"),
                        "alcance": it.get("alcance"), "resumen": it.get("resumen") or it.get("headline"),
                        "tickers": it.get("tickers") or []})
        # prioridad: bajista/mixto primero, luego macro
        out.sort(key=lambda x: ((x["impacto"] or "") not in ("bajista", "mixto"),
                                (x["alcance"] or "") != "macro"))
        return out[:limit]
    except Exception as e:
        logger.debug(f"[DailyBrief] market movers: {e}")
        return []


def _catalysts_block(hours: int = 30, limit: int = 8) -> list:
    """
    Catalizadores recientes (noticias + earnings que la IA puntuó ALTA/MEDIA) mapeados a su
    SECTOR. Es la materia prima para las señales de REVERSIÓN/IMPULSO al alza que pedía Oscar:
    p.ej. "MU reportó muy positivo → todo SMH tiene viento a favor → LONG". El radar de sectores
    da el lado técnico (qué se está dando vuelta); esto da el lado del CATALIZADOR (por qué).
    Dedup por ticker (el de mayor score). Best-effort: nunca lanza.
    """
    try:
        from utils.metrics_store import MetricsStore
        from pilot.star_score import SECTOR_ETF, SECTOR_NAME
        rows = MetricsStore().get_alerts_recent(hours=hours, sort="score")
    except Exception as e:
        logger.debug(f"[DailyBrief] catalizadores: {e}")
        return []
    seen: set = set()
    out = []
    for r in rows:
        tk = (r.get("ticker") or "").upper()
        if not tk or tk in seen:
            continue
        prio = (r.get("prioridad") or "").upper()
        score = r.get("score_ia") or 0
        if prio not in ("ALTA", "MEDIA") and score < 6:
            continue
        seen.add(tk)
        etf = SECTOR_ETF.get(tk)
        out.append({
            "ticker":     tk,
            "direccion":  r.get("direccion"),
            "tipo":       r.get("tipo_catalizador"),
            "prioridad":  prio or None,
            "score":      score,
            "sector_etf": etf,
            "sector":     SECTOR_NAME.get(etf) if etf else None,
            "resumen":    (r.get("resumen_cataliz") or r.get("titular_simple") or "")[:160],
            "contexto_sector": (r.get("contexto_sector") or "")[:160],
        })
        if len(out) >= limit:
            break
    return out


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
        "sector_radar":  _sector_radar_block(positions),
        "catalysts":     _catalysts_block(),
        "positions":     positions,
        "opportunities": _opportunities_block(dash, held),
        "market_movers": _market_movers_block(),
    }


# ── Capa de redacción (Gemini Flash, cacheada por día) ──────────────────────────

_SYSTEM = (
    "Eres el estratega macro de Oscar, un inversor que opera swing trading (estrategia Marea, "
    "tendencia de semanas) y eventos de corto plazo. Oscar YA SABE lo que pasó hoy (que el QQQ "
    "cayó, que tal acción está roja) — NO le repitas lo obvio ni hagas un resumen descriptivo. "
    "Tu ÚNICO valor es la JUGADA SIGUIENTE: leer la situación y darle señales ACCIONABLES y "
    "ANTICIPATORIAS sobre lo que probablemente pase a continuación, con el llamado a la acción "
    "más probable. Piensa como un trader macro que conecta puntos:\n"
    "  • CONTAGIO A LA BAJA: si las grandes de un sector ya cayeron (p.ej. NVDA/AVGO en semis), "
    "el resto del grupo y los índices suelen seguir en 1-3 días → 'el sector va a seguir cayendo, "
    "REDUCE/SAL de tus posiciones en X'.\n"
    "  • ROTACIÓN / FLUJO: si el dinero está saliendo de un sector (ret 5d negativo, pierde su "
    "EMA20, breadth cayendo) → 'están rotando fuera de X, sal rápido' o 'entrando a Y, mira ahí'.\n"
    "  • REVERSIÓN / IMPULSO AL ALZA: si salió un catalizador fuerte (p.ej. earnings de MU muy "
    "positivos) que beneficia a todo un sector → 'la caída de semis probablemente se revierte, "
    "considera ABRIR LONG en líderes del sector'.\n"
    "Cada señal lleva: (1) la lógica en cadena (por qué pasará), (2) el llamado a la acción más "
    "probable (SALIR/REDUCIR, ABRIR LONG, VIGILAR), (3) qué dato confirma o invalida la tesis. "
    "NO inventes datos que no te dieron; razona solo con lo que está en el contexto. Si de verdad "
    "no hay ninguna jugada clara, dilo en una frase y no rellenes. Respondes SIEMPRE en español. "
    "Tu salida es JSON."
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
        parts.append(f"- Sector líder (momentum 126d): {sec['leader']['name']} ({sec['leader']['mom_pct']:+.0f}%); "
                     f"más débil: {sec['laggard']['name']} ({sec['laggard']['mom_pct']:+.0f}%).")
    # Radar CORTO de sectores (forward-looking) — la pieza clave para anticipar contagios
    radar = brief.get("sector_radar", {})
    rsec = radar.get("sectors") or []
    if rsec:
        parts.append("- Radar corto de sectores (ret 1d / 5d / vs EMA20) — DETECTA giros antes que el momentum largo:")
        for s in rsec:
            flags = " ⚠ DÁNDOSE VUELTA" if s.get("rolling_over") else ""
            exp = (radar.get("exposure") or {}).get(s["etf"])
            etxt = f" [expuesto: {', '.join(exp['tickers'])}]" if exp else ""
            parts.append(
                f"  · {s['name']} ({s['etf']}): "
                f"1d {('%+.1f%%' % s['ret_1d']) if s.get('ret_1d') is not None else '—'}, "
                f"5d {('%+.1f%%' % s['ret_5d']) if s.get('ret_5d') is not None else '—'}, "
                f"vs EMA20 {('%+.1f%%' % s['vs_ema20']) if s.get('vs_ema20') is not None else '—'}{flags}{etxt}"
            )
        if radar.get("alerts"):
            parts.append("- ⚠ RIESGO DE CONTAGIO (anticipa, avisa A TIEMPO):")
            for a in radar["alerts"]:
                parts.append(f"  · {a}")
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
    # Catalizadores recientes (la materia prima de las señales de reversión/impulso al alza)
    cats = brief.get("catalysts") or []
    if cats:
        parts.append("- Catalizadores recientes (noticias/earnings que la IA puntuó fuerte) — úsalos para "
                     "detectar viento a favor/en contra de TODO un sector y posibles reversiones:")
        for c in cats:
            sec = f" [{c['sector']}/{c['sector_etf']}]" if c.get("sector") else ""
            dir_ = f" {c['direccion']}" if c.get("direccion") else ""
            sc = f" score {c['score']}" if c.get("score") else ""
            ctx = f" — contexto: {c['contexto_sector']}" if c.get("contexto_sector") else ""
            parts.append(f"  · {c['ticker']}{sec}{dir_} ({c.get('tipo') or '?'}{sc}): {c.get('resumen')}{ctx}")
    if brief.get("market_movers"):
        parts.append("- Market movers recientes (Trump/Fed/Treasury/SEC) — vigila si presionan tus posiciones:")
        for m in brief["market_movers"]:
            tk = f" [{', '.join(m['tickers'])}]" if m.get("tickers") else ""
            parts.append(f"  · ({m.get('source')}/{m.get('impacto')}/{m.get('alcance')}){tk} {m.get('resumen')}")
    parts.append(
        "\nTAREA — dame la JUGADA SIGUIENTE, no un resumen. Oscar ya sabe que hoy hubo rojo/verde; NO se "
        "lo repitas. Conecta los puntos del contexto y razona hacia adelante (24-72h) para producir "
        "SEÑALES ACCIONABLES, tanto de CAÍDA como de SUBIDA:\n"
        "  · Contagio: ¿qué sector con grandes ya cayendo arrastrará al resto / a tus posiciones? → SALIR/REDUCIR.\n"
        "  · Rotación de flujo: ¿de qué sector está saliendo el dinero (ret 5d-, bajo EMA20, breadth-)? ¿hacia cuál entra?\n"
        "  · Reversión por catalizador: ¿algún catalizador fuerte (earnings, etc.) da viento a favor a TODO un sector "
        "y revierte una caída? → considerar ABRIR LONG en sus líderes.\n"
        "Cada punto: la lógica en cadena + el llamado a la acción más probable + el dato que lo confirmaría/invalidaría.\n"
        "Devuelve JSON con dos campos: "
        '{"resumen": "2-4 frases con la TESIS macro del momento y la jugada principal (no descriptivo)", '
        '"atencion": ["señal accionable 1 (con acción y lógica)", "señal 2", ...]}. '
        "Máximo 4 señales, la más urgente/probable primero. Si de verdad no hay ninguna jugada clara, "
        "devuelve atencion vacío y dilo en una frase — no rellenes con obviedades."
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
    # Motor FUERTE (cambio 2026-06-23: Oscar quiere estrategia macro real, no resumen barato).
    # El brief es ~1-2 llamadas/día → el costo del razonador es despreciable y la calidad
    # del análisis (anticipación, contagio, reversiones por catalizador) es lo que importa.
    #   - tier "strong" del resolutor central → razonador (default deepseek-v4-pro).
    #   - overrides: DAILY_BRIEF_ENGINE (motor) y DAILY_BRIEF_MODEL (modelo) si se quiere
    #     desviar el brief del resto del sistema; si no, hereda AI_ENGINE / AI_MODEL_STRONG.
    # NO forzamos un motor sin key: si la IA no responde, cae a resumen por código.
    from utils.ai_client import call_ai, parse_json_response, resolve_engine_model
    eng, model = resolve_engine_model(
        "strong",
        engine=_os.environ.get("DAILY_BRIEF_ENGINE"),
        model=_os.environ.get("DAILY_BRIEF_MODEL"),
    )
    used_engine = f"{eng}:{model}" if model else eng

    text = None
    try:
        # max_tokens holgado: deepseek-v4-pro es razonador y el "pensamiento" consume tokens
        # ANTES del JSON final (con 1100 salía vacío → fallback). 3000 deja espacio al
        # razonamiento + la narrativa completa (resumen + hasta 4 señales con su lógica).
        text = call_ai(_build_prompt(brief), system=_SYSTEM, engine=eng, model=model,
                       max_tokens=3000, temperature=0.3)
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
    prev = _read_cached_narrative()   # ANTES de sobrescribir el cache
    _save_cache(payload)
    _notify_if_changed(payload, prev)
    return {k: payload[k] for k in ("resumen", "atencion", "generated_at", "engine", "date")}


def _content_signature(narr: dict | None) -> str:
    """Firma del contenido (resumen + puntos de atención) para detectar cambios reales,
    ignorando timestamp/engine. Así no avisamos por un brief idéntico regenerado."""
    if not narr:
        return ""
    aten = narr.get("atencion") or []
    if not isinstance(aten, list):
        aten = [str(aten)]
    return ((narr.get("resumen") or "").strip()
            + "||" + "|".join(str(a).strip() for a in aten))


def _notify_if_changed(payload: dict, prev: dict | None) -> None:
    """
    Manda un WhatsApp con el Brief SOLO si el contenido cambió respecto al anterior
    (decisión de Oscar 2026-06-19: evitar spam de briefs idénticos). Best-effort: nunca
    rompe la generación. Usa el canal configurado (CallMeBot→Twilio) vía send_raw_message.
    """
    try:
        if _content_signature(payload) == _content_signature(prev):
            logger.info("[DailyBrief] contenido sin cambios — no se envía WhatsApp")
            return
        import os
        to = os.environ.get("TWILIO_TO", "")
        if not to:
            logger.warning("[DailyBrief] TWILIO_TO no configurado — no se envía aviso del brief")
            return
        from alerts.twilio_sms import send_raw_message
        body = _format_brief_whatsapp(payload)
        if send_raw_message(body, to):
            logger.info("[DailyBrief] WhatsApp del brief enviado (contenido nuevo)")
        else:
            logger.warning("[DailyBrief] no se pudo enviar WhatsApp del brief")
    except Exception as e:
        logger.warning(f"[DailyBrief] aviso WhatsApp falló: {e}")


def _format_brief_whatsapp(payload: dict) -> str:
    """Texto del WhatsApp del Brief: resumen + puntos de atención. Conciso."""
    lines = [f"📋 Brief de mercado — {payload.get('generated_at', '')}", ""]
    resumen = (payload.get("resumen") or "").strip()
    if resumen:
        lines.append(resumen)
    aten = payload.get("atencion") or []
    if aten:
        lines.append("")
        lines.append("👀 Atención hoy:")
        for a in aten:
            lines.append(f"• {a}")
    return "\n".join(lines)


def _fallback_narrative(brief: dict) -> tuple:
    """Resumen mínimo por código si Gemini no responde — sin texto bonito, pero útil."""
    reg = brief.get("regime", {})
    sen = brief.get("sentinel", {})
    resumen = ("Régimen " + ("risk-on (alcista)." if reg.get("risk_on") else "risk-off (defensivo)."))
    aten = []
    # Contagio sectorial primero (es lo anticipatorio): sectores girando donde hay exposición
    for a in (brief.get("sector_radar", {}).get("alerts") or []):
        aten.append(a)
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
