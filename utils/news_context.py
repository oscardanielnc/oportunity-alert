"""
utils/news_context.py — Contexto por CÓDIGO para el scoring de noticias (sin tokens).

Principio (decisión Oscar 2026-06-01): todo lo que se pueda calcular sin IA se calcula
acá y se le pasa a Claude ya masticado. Claude solo razona lo que NO se puede calcular
(contexto macro/sectorial, vientos en contra, si el catalizador se refuerza o se anula).

Fuentes, todas YA existentes (cero llamadas nuevas a APIs de pago):
  - Trayectoria de precio  ← conviction_gates (RSI/EMA20/EMA50/ATR ya calculados) + price_data
  - Fuerza de sector + macro ← pilot/pilot_dashboard.json (sector_strength + macro_bullish)
  - Noticias previas del ticker ← metrics_store.get_recent_news_for_ticker (lo pasa el caller)
  - Estado "ya priceado" / continuación ← conviction (gate1) + price_data.change_pct

build_context() NUNCA lanza: el contexto es best-effort. Si algo falla, esa línea se omite.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DASHBOARD_JSON = Path(__file__).parent.parent / "data" / "pilot_dashboard.json"


def _load_sector_macro(ticker: str) -> dict:
    """
    Lee sector_strength + macro_bullish del dashboard del piloto y los cruza con el
    sector del ticker. Retorna {} si no hay datos.
    """
    try:
        from pilot.star_score import SECTOR_ETF, SECTOR_NAME, DEFAULT_ETF
        d = json.loads(_DASHBOARD_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}

    etf = SECTOR_ETF.get(ticker.upper(), DEFAULT_ETF)
    strength = d.get("sector_strength", []) or []
    out: dict = {
        "macro_bullish": d.get("macro_bullish"),
        "sector_etf": etf,
        "sector_name": SECTOR_NAME.get(etf, etf),
    }
    # Momentum del sector del ticker + su ranking entre todos los sectores
    for i, s in enumerate(strength):
        if s.get("etf") == etf:
            out["sector_mom_pct"] = s.get("mom_pct")
            out["sector_rank"] = i + 1
            out["sector_total"] = len(strength)
            break
    # Sectores líder y rezagado (para que la IA sepa qué está caliente y qué frío)
    if strength:
        out["sector_leader"] = f"{strength[0].get('name')} ({strength[0].get('mom_pct'):+.0f}%)"
        out["sector_laggard"] = f"{strength[-1].get('name')} ({strength[-1].get('mom_pct'):+.0f}%)"
    return out


def marea_leader_status(ticker: str) -> dict:
    """
    ¿El ticker es HOY un líder de Marea? Lee `leaders` del dashboard del piloto (lo
    genera el cron diario). Cruza el brazo de NOTICIAS/GAP con la señal de PRECIO de
    Marea: si un movimiento por catalizador cae sobre un ⭐ TOP en breakout y sector
    favorable, NO es ruido — es la oportunidad que Marea ya rankeaba (caso MRVL 2-jun:
    #7 ⭐ breakout, Semis/IA sector #1). Retorna {} si el ticker no es líder o no hay datos.
    """
    try:
        d = json.loads(_DASHBOARD_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    for l in (d.get("leaders") or []):
        if (l.get("ticker") or "").upper() == ticker.upper():
            return {
                "rank": l.get("rank"),
                "is_top": bool(l.get("is_top")),
                "is_breakout": bool(l.get("is_breakout")),
                "held": bool(l.get("held")),
                "sector_favor": bool(l.get("sector_favor")),
                "sector_name": l.get("sector_name"),
                "sector_rank": l.get("sector_rank"),
                "sector_total": l.get("sector_total"),
                "size_pct": l.get("size_pct"),
            }
    return {}


def marea_leader_tag(ticker: str) -> str:
    """Etiqueta corta para SMS/dashboard si el ticker es líder de Marea hoy. '' si no."""
    s = marea_leader_status(ticker)
    if not s or not (s.get("is_top") or s.get("is_breakout")):
        return ""
    bits = []
    if s.get("is_top"):
        bits.append("⭐ TOP")
    if s.get("is_breakout"):
        bits.append("breakout")
    sect = ""
    if s.get("sector_name") and s.get("sector_rank"):
        sect = f", {s['sector_name']} #{s['sector_rank']}/{s.get('sector_total')}"
    rank = f"#{s['rank']} " if s.get("rank") else ""
    size = f", ~{s['size_pct']:.0f}% sug." if s.get("size_pct") else ""
    held = " — YA EN CARTERA" if s.get("held") else ""
    return f"🌊 LÍDER MAREA {rank}({' + '.join(bits)}{sect}{size}){held}"


def _price_trajectory(price_data: dict, conviction: dict) -> dict:
    """Trayectoria técnica ya calculada por conviction_gates (no recalcula nada caro)."""
    price = price_data.get("current_price") or conviction.get("entry_price") or 0.0
    rsi   = conviction.get("gate2_rsi") or 0.0
    ema20 = conviction.get("gate2_ema20") or 0.0
    ema50 = conviction.get("gate2_ema50") or 0.0
    atr   = conviction.get("atr14") or 0.0
    out: dict = {
        "price": price,
        "change_pct": price_data.get("change_pct"),
        "rsi": rsi,
    }
    if price > 0 and ema20 > 0:
        out["vs_ema20_pct"] = (price - ema20) / ema20 * 100
    if price > 0 and ema50 > 0:
        out["vs_ema50_pct"] = (price - ema50) / ema50 * 100
    if ema20 > 0 and ema50 > 0:
        out["trend"] = "alcista" if ema20 >= ema50 else "bajista"
    if price > 0 and atr > 0:
        out["atr_pct"] = atr / price * 100
    if rsi >= 70:
        out["rsi_label"] = "sobrecomprado"
    elif rsi <= 30:
        out["rsi_label"] = "sobrevendido"
    else:
        out["rsi_label"] = "neutral"
    return out


def build_context(
    ticker: str,
    price_data: dict,
    conviction: dict,
    article: dict,
    *,
    recent_news: list = None,
    earnings_soon: bool = False,
    momentum_continuation: bool = False,
) -> tuple[str, dict]:
    """
    Arma el bloque de contexto (texto para el prompt + dict para logging).
    Best-effort: cualquier sección que falle se omite sin romper.
    """
    ctx: dict = {}
    lines: list[str] = []

    # ── Trayectoria de precio ────────────────────────────────────────────────
    try:
        traj = _price_trajectory(price_data, conviction)
        ctx["trajectory"] = traj
        seg = []
        if traj.get("vs_ema20_pct") is not None:
            seg.append(f"{traj['vs_ema20_pct']:+.1f}% vs EMA20")
        if traj.get("vs_ema50_pct") is not None:
            seg.append(f"{traj['vs_ema50_pct']:+.1f}% vs EMA50")
        if traj.get("trend"):
            seg.append(f"tendencia {traj['trend']}")
        seg.append(f"RSI {traj['rsi']:.0f} ({traj['rsi_label']})")
        if traj.get("atr_pct"):
            seg.append(f"ATR {traj['atr_pct']:.1f}% del precio")
        lines.append("TRAYECTORIA: " + ", ".join(seg))
    except Exception as e:
        logger.debug(f"[NewsContext] trajectory falló {ticker}: {e}")

    # ── Sector + macro ───────────────────────────────────────────────────────
    try:
        sm = _load_sector_macro(ticker)
        if sm:
            ctx["sector"] = sm
            seg = [f"sector {sm.get('sector_name')}"]
            if sm.get("sector_mom_pct") is not None:
                rank = (f", ranking {sm['sector_rank']}/{sm['sector_total']}"
                        if sm.get("sector_rank") else "")
                seg.append(f"momentum {sm['sector_mom_pct']:+.0f}%{rank}")
            if sm.get("macro_bullish") is not None:
                seg.append("macro ALCISTA (QQQ>SMA200)" if sm["macro_bullish"]
                           else "macro BAJISTA (QQQ<SMA200)")
            lines.append("SECTOR/MACRO: " + ", ".join(seg))
            if sm.get("sector_leader"):
                lines.append(
                    f"  (sector líder hoy: {sm['sector_leader']} · "
                    f"más débil: {sm.get('sector_laggard')})"
                )
    except Exception as e:
        logger.debug(f"[NewsContext] sector falló {ticker}: {e}")

    # ── Noticias previas del mismo ticker (clustering de catalizadores) ────────
    try:
        if recent_news:
            ctx["recent_news_count"] = len(recent_news)
            prev = []
            for n in recent_news[:3]:
                rc = (n.get("resumen_cataliz") or "")[:70]
                if rc:
                    prev.append(f"[{n.get('prioridad', '?')}] {rc}")
            if prev:
                lines.append(
                    f"NOTICIAS PREVIAS (90 min, {len(recent_news)}): " + " | ".join(prev)
                )
    except Exception as e:
        logger.debug(f"[NewsContext] recent_news falló {ticker}: {e}")

    # ── Estado de catalizador (priceado / continuación / earnings) ─────────────
    flags = []
    chg = price_data.get("change_pct")
    if chg is not None:
        flags.append(f"ya movió {chg:+.1f}% hoy")
    if momentum_continuation:
        ctx["momentum_continuation"] = True
        flags.append(
            "MOVIMIENTO GRANDE YA EN CURSO — evaluar como CONTINUACIÓN de corto plazo "
            "(ventana típica 1-2 días antes de revertir), no como entrada fresca"
        )
    if earnings_soon:
        ctx["earnings_soon"] = True
        flags.append("earnings hoy/ayer (el gap ya priceó parte del catalizador)")
    if article.get("possible_already_priced"):
        flags.append("titular con lenguaje de movimiento ya consumado (soars/surges)")
    if flags:
        lines.append("ESTADO: " + "; ".join(flags))

    context_text = "\n".join(lines)
    return context_text, ctx
