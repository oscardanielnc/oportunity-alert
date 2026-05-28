"""
Pre-market momentum scanner v2 — Fase 1 (LONG) + Fase 2 (SHORT).

Detecta movimientos sostenidos en pre-market (3am-6am Lima / 4am-7am EDT)
antes de la apertura regular (8:30am Lima / 9:30am EDT).

Lógica:
  - 6:00am Lima (pasada 1): escanea todo el watchlist
  - 7:00am Lima (pasada 2): re-evalúa solo tickers con score MEDIA en pasada 1

Criterios LONG:  movimiento >= +2.5%, sin retroceso >1.5% en ventanas de 30min,
                 consistencia direccional >= 60%
Criterios SHORT: movimiento <= -3.5% (umbral más estricto — pre-market thin),
                 sin rally >1.5% en ventanas de 30min, consistencia >= 60%

Mejoras v2 (basadas en investigación académica y backtests documentados):
  1. Filtro de volumen mínimo (50K shares) — thin = skip directo
  2. RVOL vs promedio histórico 10 días — confirma participación real
  3. Detección de spike temprano que ya retrocede (Berkman et al. fade pattern)
  4. Pre-market code conviction score 0-15 — gating antes de llamar a la IA
  5. Día de la semana — lunes descuenta (documentado: Monday effect)
  6. tipo_catalizador en JSON IA — jerarquía de continuación
  7. entry_style en JSON IA — diferencia "apertura" de "pullback_inicial"

Si movimiento ya supera 8% (PRICED_IN_PCT): se advierte overextension
y el umbral de conviccion IA minima se eleva en 1 punto.
"""

import os
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

from utils.ai_client import call_ai, parse_json_response
from alerts.twilio_sms import send_raw_message

logger = logging.getLogger(__name__)

ALPACA_BASE = "https://data.alpaca.markets"
LIMA = timezone(timedelta(hours=-5))

# ── Umbrales de señal técnica ─────────────────────────────────────────────────
LONG_MIN_PCT    = 2.5    # % mínimo de subida para calificar LONG
SHORT_MIN_PCT   = 3.5    # % mínimo de bajada para calificar SHORT (asimétrico: thin=noisy)
MAX_RETRACE_PCT = 1.5    # retroceso/rally máximo en cualquier ventana de 30min
MIN_CONSISTENCY = 0.60   # fracción mínima de velas en la dirección correcta
MIN_BARS        = 6      # mínimo de velas 5min (~30 min de datos)
PRICED_IN_PCT   = 8.0    # si supera esto, convicción mínima sube en 1

# ── Filtros de calidad v2 (académicamente documentados) ──────────────────────
MIN_PREMARKET_VOL   = 50_000   # < esto → skip sin análisis (mercado demasiado thin)
MIN_PRICE           = 2.0      # < $2 → skip (OTC/penny, riesgo manipulación)
RVOL_FLOOR          = 1.5      # RVOL mínimo para no penalizar en code score
RVOL_STRONG         = 3.0      # RVOL que suma +2 pts en code score
RVOL_HISTORY_DAYS   = 10       # días hábiles para calcular promedio de volumen pre-market
EARLY_SPIKE_HOUR    = 5        # antes de las 5am Lima = ventana thin (4-5am EDT)
EARLY_FADE_PCT      = 1.0      # % de retroceso desde extremo early → señal de agotamiento

# ── Pre-market code conviction score ─────────────────────────────────────────
CODE_SCORE_MIN_FOR_AI = 6      # score mínimo (de 15) para llamar a la IA

# ── Convicción IA ─────────────────────────────────────────────────────────────
SCORE_ALTA  = 7
SCORE_MEDIA = 5

# Ventana de noticias (96h cubre fin de semana + festivos como Memorial Day)
NEWS_HOURS_BACK = 96


# ── Alpaca helpers ────────────────────────────────────────────────────────────

def _alpaca_headers() -> dict:
    key    = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise EnvironmentError("ALPACA_API_KEY / ALPACA_SECRET_KEY no configuradas")
    return {
        "APCA-API-KEY-ID":     key,
        "APCA-API-SECRET-KEY": secret,
        "Accept":              "application/json",
    }


def _get_bars_range(
    ticker: str,
    start_utc: datetime,
    end_utc: datetime,
    timeframe: str = "5Min",
) -> list:
    """
    Barras Alpaca para un rango UTC explícito.
    Intenta feed IEX primero (cubre pre-market); cae a SIP si retorna vacío.
    """
    try:
        headers = _alpaca_headers()
    except EnvironmentError as e:
        logger.warning(f"[PreMarket] {e}")
        return []

    params = {
        "timeframe": timeframe,
        "start":     start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end":       end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sort":      "asc",
        "limit":     500,
    }

    for feed in ("sip", "iex"):   # SIP first: consolidated tape, full AH/pre-market volume
        params["feed"] = feed
        try:
            resp = requests.get(
                f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
                params=params, headers=headers, timeout=10,
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            bars = resp.json().get("bars") or []
            if bars:
                return bars
        except Exception as e:
            logger.debug(f"[PreMarket] {ticker} bars error ({feed}): {e}")

    return []


def _get_prev_close(ticker: str) -> float:
    """Cierre del último día completo — usa snapshot prevDailyBar.c con fallback."""
    try:
        headers = _alpaca_headers()
        resp = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/snapshot",
            params={"feed": "iex"}, headers=headers, timeout=8,
        )
        if resp.status_code == 200:
            prev = resp.json().get("prevDailyBar", {}).get("c")
            if prev:
                return float(prev)
        resp2 = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
            params={"timeframe": "1Day", "limit": 3, "feed": "sip", "sort": "desc"},
            headers=headers, timeout=8,
        )
        if resp2.status_code == 200:
            bars = resp2.json().get("bars") or []
            if len(bars) >= 2:
                return float(bars[1]["c"])
            if bars:
                return float(bars[0]["c"])
    except Exception as e:
        logger.debug(f"[PreMarket] prev_close error {ticker}: {e}")
    return 0.0


def _get_avg_premarket_vol(ticker: str, n_days: int = RVOL_HISTORY_DAYS) -> float:
    """
    Volumen promedio en la ventana 3am-6am Lima (8am-11am UTC) de los últimos
    n_days días hábiles. Usado para calcular RVOL.
    Una sola llamada API con ventana de 14 días calendario.
    """
    try:
        headers  = _alpaca_headers()
        now_utc  = datetime.now(timezone.utc)
        start_utc = now_utc - timedelta(days=14)
        # Excluir hoy: hasta ayer a las 11am UTC
        end_utc  = (now_utc - timedelta(days=1)).replace(
            hour=11, minute=0, second=0, microsecond=0
        )

        resp = requests.get(
            f"{ALPACA_BASE}/v2/stocks/{ticker}/bars",
            params={
                "timeframe": "5Min",
                "start":     start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end":       end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "feed":      "sip",   # SIP: volumen consolidado real (IEX da 55x menos)
                "sort":      "asc",
                "limit":     2000,
            },
            headers=headers, timeout=12,
        )
        if resp.status_code != 200:
            return 0.0

        all_bars = resp.json().get("bars") or []
        if not all_bars:
            return 0.0

        # Agrupar por día, solo barras 8am-11am UTC (= 3am-6am Lima)
        daily_vols: dict = {}
        for b in all_bars:
            t = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
            if 8 <= t.hour < 11:
                day_key = t.strftime("%Y-%m-%d")
                daily_vols[day_key] = daily_vols.get(day_key, 0) + float(b.get("v", 0))

        if not daily_vols:
            return 0.0

        recent = sorted(daily_vols.keys())[-n_days:]
        return round(sum(daily_vols[d] for d in recent) / len(recent), 0)

    except Exception as e:
        logger.debug(f"[PreMarket] avg_vol error {ticker}: {e}")
        return 0.0


# ── Análisis de momentum (v2 — incluye spike timing y vol total) ──────────────

def _analyze_momentum(bars: list, prev_close: float, direction: str) -> dict:
    """
    Evalúa si las barras 5min de pre-market muestran momentum sostenido.
    Versión v2: añade total_premarket_vol, early_spike_fading, extreme timing.
    """
    result = {
        "passes":               False,
        "total_change_pct":     0.0,
        "max_adverse_pct":      0.0,
        "consistency":          0.0,
        "current_price":        0.0,
        "vwap":                 0.0,
        "bars_analyzed":        len(bars),
        "is_priced_in":         False,
        "rejection_reason":     None,
        # ── v2 ───────────────────────────────────────────────────────────────
        "total_premarket_vol":  0,
        "early_spike_fading":   False,
        "early_spike_retrace":  0.0,
        "extreme_time_lima":    "N/D",
        "high_bar_age_minutes": 999.0,
        "rvol":                 0.0,   # inyectado externamente tras _get_avg_premarket_vol
    }

    if len(bars) < MIN_BARS:
        result["rejection_reason"] = f"datos insuficientes ({len(bars)} bars)"
        return result

    if prev_close <= 0:
        result["rejection_reason"] = "prev_close no disponible"
        return result

    current_price = float(bars[-1]["c"])
    result["current_price"] = round(current_price, 4)

    # Volumen total del pre-market
    total_vol = int(sum(float(b.get("v", 0)) for b in bars))
    result["total_premarket_vol"] = total_vol

    total_change = (current_price - prev_close) / prev_close * 100
    result["total_change_pct"] = round(total_change, 2)

    if direction == "LONG" and total_change < LONG_MIN_PCT:
        result["rejection_reason"] = f"movimiento insuficiente ({total_change:+.1f}% < +{LONG_MIN_PCT}%)"
        return result
    if direction == "SHORT" and total_change > -SHORT_MIN_PCT:
        result["rejection_reason"] = f"movimiento insuficiente ({total_change:+.1f}% > -{SHORT_MIN_PCT}%)"
        return result

    if abs(total_change) >= PRICED_IN_PCT:
        result["is_priced_in"] = True

    # ── Detección del extremo y análisis de spike temprano ──────────────────
    # Berkman et al.: spikes formados en la ventana thin (4-5am EDT = 3-4am Lima)
    # tienen alta probabilidad de reversión intraday si ya retrocedieron.
    now_lima = datetime.now(LIMA)
    try:
        if direction == "LONG":
            extreme_idx   = max(range(len(bars)), key=lambda i: float(bars[i]["h"]))
            extreme_price = float(bars[extreme_idx]["h"])
        else:
            extreme_idx   = min(range(len(bars)), key=lambda i: float(bars[i]["l"]))
            extreme_price = float(bars[extreme_idx]["l"])

        t_str       = bars[extreme_idx]["t"]
        extreme_utc = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
        extreme_lima = extreme_utc.astimezone(LIMA)

        result["extreme_time_lima"]    = extreme_lima.strftime("%H:%M")
        result["high_bar_age_minutes"] = round(
            (now_lima - extreme_lima).total_seconds() / 60, 1
        )

        # ¿El extremo se formó en la ventana thin (antes de EARLY_SPIKE_HOUR Lima)?
        if extreme_lima.hour < EARLY_SPIKE_HOUR:
            if direction == "LONG":
                retrace = max(0.0, (extreme_price - current_price) / extreme_price * 100)
            else:
                retrace = max(0.0, (current_price - extreme_price) / abs(extreme_price) * 100)
            result["early_spike_retrace"] = round(retrace, 2)
            result["early_spike_fading"]  = retrace >= EARLY_FADE_PCT

    except Exception:
        pass

    # ── Retroceso máximo en ventanas de 30min ────────────────────────────────
    window_size = 6
    max_adverse = 0.0
    for i in range(max(0, len(bars) - window_size * 3), len(bars) - window_size + 1):
        window = bars[i: i + window_size]
        if len(window) < window_size:
            continue
        w_open = float(window[0]["o"])
        if w_open <= 0:
            continue
        if direction == "LONG":
            w_low   = min(float(b["l"]) for b in window)
            adverse = max(0.0, (w_open - w_low) / w_open * 100)
        else:
            w_high  = max(float(b["h"]) for b in window)
            adverse = max(0.0, (w_high - w_open) / w_open * 100)
        max_adverse = max(max_adverse, adverse)

    result["max_adverse_pct"] = round(max_adverse, 2)

    if max_adverse >= MAX_RETRACE_PCT:
        result["rejection_reason"] = (
            f"{'retroceso' if direction == 'LONG' else 'rally'} "
            f"{max_adverse:.1f}% supera umbral {MAX_RETRACE_PCT}%"
        )
        return result

    # ── Consistencia direccional ─────────────────────────────────────────────
    directional = sum(
        1 for b in bars
        if (direction == "LONG"  and float(b["c"]) >= float(b["o"]))
        or (direction == "SHORT" and float(b["c"]) <= float(b["o"]))
    )
    consistency = directional / len(bars)
    result["consistency"] = round(consistency, 2)

    if consistency < MIN_CONSISTENCY:
        result["rejection_reason"] = (
            f"consistencia {consistency:.0%} < {MIN_CONSISTENCY:.0%} — errático"
        )
        return result

    # ── VWAP del pre-market ──────────────────────────────────────────────────
    total_vol_f = sum(float(b.get("v", 0)) for b in bars)
    vwap_num    = sum(
        float(b.get("v", 0)) * (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3
        for b in bars
    )
    result["vwap"] = round(vwap_num / total_vol_f, 4) if total_vol_f > 0 else current_price

    result["passes"] = True
    return result


# ── Patrón de consolidación (aproximación ligera sin importar event_gate) ─────

def _estimate_stabilization_score(bars: list, direction: str) -> int:
    """
    Detecta consolidación post-spike en las últimas ~60 min.
    Score 0-2: volumen sostenido (1pt) + rango de velas achicando (1pt).
    Documentado como el patrón de mayor probabilidad de continuación.
    """
    if len(bars) < 8:
        return 0

    recent = bars[-6:]
    older  = bars[-12:-6] if len(bars) >= 12 else bars[:-6]
    if not older:
        return 0

    # Volumen sostenido: reciente >= 60% del anterior (no se está secando)
    vol_recent = sum(float(b.get("v", 0)) for b in recent)
    vol_older  = sum(float(b.get("v", 0)) for b in older)
    vol_ok = vol_older > 0 and (vol_recent / vol_older) >= 0.60

    # Rango achicando: velas más pequeñas = menor volatilidad = consolidación
    range_recent = sum(float(b["h"]) - float(b["l"]) for b in recent) / len(recent)
    range_older  = sum(float(b["h"]) - float(b["l"]) for b in older)  / len(older)
    range_ok = range_older > 0 and range_recent < range_older * 0.90

    return int(vol_ok) + int(range_ok)


# ── Pre-market code conviction score (0-15) ───────────────────────────────────

def _premarket_code_score(
    ticker: str,
    momentum: dict,
    bars: list,
    direction: str,
) -> tuple:
    """
    Score de convicción calculado en código puro antes de llamar a la IA.
    Si score < CODE_SCORE_MIN_FOR_AI → skip IA (ahorra tokens).

    Componentes:
      vol_score      0-4  (volumen absoluto pre-market)
      rvol_score     0-2  (RVOL vs promedio histórico 10 días)
      momentum_score 0-3  (consistencia direccional)
      time_score     0-2  (cuándo se alcanzó el extremo)
      day_score      0-1  (día de la semana — lunes descuenta)
      pattern_score  0-2  (consolidación post-spike)
      penalties      0/-3 (priced_in, early_spike_fading, precio bajo)
    """
    breakdown = {
        "vol_score":      0,
        "rvol_score":     0,
        "momentum_score": 0,
        "time_score":     0,
        "day_score":      0,
        "pattern_score":  0,
        "penalties":      0,
        "total":          0,
    }

    premarket_vol = momentum.get("total_premarket_vol", 0)
    rvol          = momentum.get("rvol", 0.0)
    consistency   = momentum.get("consistency", 0.0)

    # 1. Volumen absoluto (0-4) — documentado como el predictor más importante
    if premarket_vol >= 500_000:
        breakdown["vol_score"] = 4
    elif premarket_vol >= 200_000:
        breakdown["vol_score"] = 3
    elif premarket_vol >= 100_000:
        breakdown["vol_score"] = 2
    elif premarket_vol >= 50_000:
        breakdown["vol_score"] = 1

    # 2. RVOL vs promedio histórico (0-2)
    if rvol >= RVOL_STRONG:
        breakdown["rvol_score"] = 2
    elif rvol >= RVOL_FLOOR:
        breakdown["rvol_score"] = 1

    # 3. Consistencia direccional (0-3)
    if consistency >= 0.80:
        breakdown["momentum_score"] = 3
    elif consistency >= 0.70:
        breakdown["momentum_score"] = 2
    elif consistency >= 0.60:
        breakdown["momentum_score"] = 1

    # 4. Timing del extremo (0-2) — high reciente = momentum vivo
    if not momentum.get("early_spike_fading", False):
        age = momentum.get("high_bar_age_minutes", 999)
        if age <= 30:
            breakdown["time_score"] = 2
        elif age <= 60:
            breakdown["time_score"] = 1

    # 5. Día de la semana (0-1) — Monday effect documentado (Cross, French)
    breakdown["day_score"] = 0 if datetime.now(LIMA).weekday() == 0 else 1

    # 6. Patrón de consolidación (0-2)
    breakdown["pattern_score"] = _estimate_stabilization_score(bars, direction)

    # 7. Penalizaciones
    penalties = 0
    if momentum.get("is_priced_in"):
        penalties -= 1   # movimiento ya >8%
    if momentum.get("early_spike_fading"):
        penalties -= 2   # spike early que retrocede (Berkman fade pattern)
    if momentum.get("current_price", 10) < 5.0:
        penalties -= 1   # precio bajo → mayor riesgo de manipulación

    breakdown["penalties"] = penalties

    total = (
        breakdown["vol_score"]      +
        breakdown["rvol_score"]     +
        breakdown["momentum_score"] +
        breakdown["time_score"]     +
        breakdown["day_score"]      +
        breakdown["pattern_score"]  +
        penalties
    )
    breakdown["total"] = max(0, total)
    return breakdown["total"], breakdown


# ── Noticias recientes ────────────────────────────────────────────────────────

def _fetch_recent_news(ticker: str) -> list:
    """Últimas 4 noticias del ticker en las 96h previas (cubre festivos)."""
    try:
        from sources.finnhub_news import fetch_company_news
        articles = fetch_company_news(ticker, hours_back=NEWS_HOURS_BACK)
        articles.sort(key=lambda a: a.get("age_minutes", 9999))
        result = []
        for a in articles[:4]:
            title   = a.get("title", "").strip()
            summary = (a.get("summary") or "")[:200].strip()
            age_h   = round(a.get("age_minutes", 0) / 60, 1)
            if title:
                result.append({"title": title, "summary": summary, "age_h": age_h})
        return result
    except Exception as e:
        logger.debug(f"[PreMarket] news fetch error {ticker}: {e}")
        return []


# ── Prompt IA (v2 — incluye score técnico, RVOL, entry_style, tipo_catalizador) ─

def _build_ai_prompt(
    ticker: str,
    direction: str,
    momentum: dict,
    news_items: list,
    prev_close: float,
    code_score: int,
    score_breakdown: dict,
) -> str:
    news_block = ""
    for n in news_items:
        age_str = f"{n['age_h']:.0f}h atras"
        news_block += f"  - [{age_str}] {n['title']}\n"
        if n.get("summary"):
            news_block += f"    {n['summary'][:150]}\n"
    if not news_block:
        news_block = "  (Sin noticias relevantes en las ultimas 96h)\n"

    rvol     = momentum.get("rvol", 0)
    vol      = momentum.get("total_premarket_vol", 0)
    rvol_str = f"{rvol:.1f}x" if rvol > 0 else "N/D"

    try:
        from utils.conviction_gates import SECTOR_FOR_ATR
        sector = SECTOR_FOR_ATR.get(ticker, "DEFAULT")
    except Exception:
        sector = "DEFAULT"

    # Warnings contextuales para guiar la IA
    warnings = []
    if momentum.get("is_priced_in"):
        warnings.append(
            f"ADVERTENCIA: movimiento >{PRICED_IN_PCT}% — posible overextension. "
            "Requiere catalizador muy solido para conviccion >= 8."
        )
    if momentum.get("early_spike_fading"):
        retrace = momentum.get("early_spike_retrace", 0)
        warnings.append(
            f"ADVERTENCIA: Spike formado antes de las {EARLY_SPIKE_HOUR}am Lima "
            f"ya retrocedio {retrace:.1f}% — patron de agotamiento (Berkman fade)."
        )
    if 0 < rvol < RVOL_FLOOR:
        warnings.append(
            f"ADVERTENCIA: RVOL bajo ({rvol:.1f}x) — liquidez thin, "
            "mayor riesgo de reversion brusca al abrir."
        )

    warning_block = ("\n".join(warnings) + "\n\n") if warnings else ""
    adverse_label = "Retroceso max" if direction == "LONG" else "Rally-en-contra max"

    return f"""Analiza senial pre-market. Se preciso y conservador.

TICKER: {ticker} | SECTOR: {sector}
DIRECCION: {'ALCISTA (LONG)' if direction == 'LONG' else 'BAJISTA (SHORT)'}

PRECIO:
  Cierre anterior: ${prev_close:.2f}
  Precio actual: ${momentum['current_price']:.2f}
  Cambio total: {momentum['total_change_pct']:+.1f}%
  {adverse_label} en 30min: {momentum['max_adverse_pct']:.1f}% (umbral={MAX_RETRACE_PCT}%)
  Consistencia direccional: {momentum['consistency']:.0%}
  VWAP pre-market: ${momentum['vwap']:.2f}
  Extremo en: {momentum.get('extreme_time_lima','N/D')} Lima (hace {momentum.get('high_bar_age_minutes',999):.0f}min)

VOLUMEN:
  Pre-market total: {vol:,} shares
  RVOL vs promedio historico 10d: {rvol_str}

SCORE TECNICO PRE-IA: {code_score}/15
  Vol={score_breakdown.get('vol_score',0)}/4  RVOL={score_breakdown.get('rvol_score',0)}/2  \
Consistencia={score_breakdown.get('momentum_score',0)}/3
  Timing={score_breakdown.get('time_score',0)}/2  DiaSem={score_breakdown.get('day_score',0)}/1  \
Patron={score_breakdown.get('pattern_score',0)}/2  Penaliz={score_breakdown.get('penalties',0)}

{warning_block}NOTICIAS RECIENTES (ultimas 96h):
{news_block}
Responde SOLO con JSON valido:
{{
  "conviccion": <1-10>,
  "continuacion": "<ALTA|MEDIA|BAJA>",
  "tipo_catalizador": "<earnings_beat_raise|earnings_beat|fda_approval|merger_acq|contract_gov|upgrade_target|sin_catalizador|otro>",
  "resumen_cataliz": "<max 200 chars: catalizador y por que continua o no>",
  "entry_style": "<apertura|pullback_inicial|confirmar_5min|esperar_patron>",
  "stop_pct": <float — % desde precio actual>,
  "target_pct": <float — minimo 2x stop_pct>,
  "riesgo": "<max 150 chars: riesgo principal>"
}}

REGLAS DE EVALUACION:
  Subir conviccion: earnings beat+raise, RVOL>=3x, consolidacion post-spike, mid-cap, catalizador claro
  Bajar conviccion: sin catalizador, RVOL<1.5x, spike temprano retrocediendo, lunes, sector QUANTUM/SMALL_AI thin
  entry_style="pullback_inicial" para QUANTUM/SMALL_AI/SPACE — riesgo retail-gap (Berkman et al.)
  entry_style="apertura" solo si mid/large-cap + catalizador fundamental solido (earnings+raise, M&A)
  entry_style="confirmar_5min" si hay catalizador pero incertidumbre tecnica
  entry_style="esperar_patron" si el codigo_score es bajo o hay advertencias activas
  stop_pct minimo: 2.5% HIGHVOL, 3.5% QUANTUM/SMALL_AI/SPACE
  target_pct >= 2x stop_pct (R:R minimo documentado 2:1)
  contrato gobierno (contract_gov): bajar conviccion 1pt — sell-the-news comun
  Escala: 8-10=catalizador+tecnico confirmado | 5-7=señal presente catalizador incierto | 1-4=ruido"""


# ── Scoring IA ────────────────────────────────────────────────────────────────

def _score_premarket_with_ai(
    ticker: str,
    direction: str,
    momentum: dict,
    news_items: list,
    prev_close: float,
    code_score: int,
    score_breakdown: dict,
) -> dict:
    """Llama a Gemini o Claude con el prompt v2. Retorna dict con tipo_catalizador y entry_style."""
    fallback = {
        "conviccion":       0,
        "continuacion":     "BAJA",
        "tipo_catalizador": "sin_catalizador",
        "resumen_cataliz":  "Error en analisis IA",
        "entry_style":      "esperar_patron",
        "stop_pct":         3.5,
        "target_pct":       7.0,
        "riesgo":           "No se pudo evaluar",
    }

    prompt = _build_ai_prompt(
        ticker, direction, momentum, news_items, prev_close, code_score, score_breakdown
    )

    raw  = call_ai(prompt, max_tokens=400, force_json=True)
    data = parse_json_response(raw)

    if data is None:
        logger.warning(f"[PreMarket] IA no devolvio JSON para {ticker}")
        return fallback

    try:
        return {
            "conviccion":       int(data.get("conviccion", 0)),
            "continuacion":     str(data.get("continuacion", "BAJA")),
            "tipo_catalizador": str(data.get("tipo_catalizador", "sin_catalizador")),
            "resumen_cataliz":  str(data.get("resumen_cataliz", ""))[:300],
            "entry_style":      str(data.get("entry_style", "esperar_patron")),
            "stop_pct":         float(data.get("stop_pct", 3.5)),
            "target_pct":       float(data.get("target_pct", 7.0)),
            "riesgo":           str(data.get("riesgo", ""))[:200],
        }
    except Exception as e:
        logger.error(f"[PreMarket] Error procesando respuesta IA {ticker}: {e}")
        return fallback


# ── Formato SMS (v2 — RVOL, code score, tipo_catalizador, entry_style) ────────

def _format_premarket_sms(
    ticker: str,
    direction: str,
    momentum: dict,
    ai_result: dict,
    prev_close: float,
    code_score: int,
    score_breakdown: dict,
    is_second_pass: bool = False,
) -> str:
    conviccion  = ai_result["conviccion"]
    prioridad   = "ALTA" if conviccion >= SCORE_ALTA else "MEDIA"
    current     = momentum["current_price"]
    change_pct  = momentum["total_change_pct"]
    consistency = momentum["consistency"]
    adverse     = momentum["max_adverse_pct"]
    rvol        = momentum.get("rvol", 0)
    vol         = momentum.get("total_premarket_vol", 0)

    if direction == "LONG":
        stop_price   = round(current * (1 - ai_result["stop_pct"] / 100), 2)
        target_price = round(current * (1 + ai_result["target_pct"] / 100), 2)
    else:
        stop_price   = round(current * (1 + ai_result["stop_pct"] / 100), 2)
        target_price = round(current * (1 - ai_result["target_pct"] / 100), 2)

    pass_tag = " [2a EVAL]" if is_second_pass else ""

    entry_map = {
        "apertura":        "en apertura regular",
        "pullback_inicial": "en pullback inicial (no chase)",
        "confirmar_5min":  "confirmar dir. primeros 5min",
        "esperar_patron":  "esperar consolidacion",
    }
    entry_str = entry_map.get(ai_result["entry_style"], ai_result["entry_style"])

    # Iconos de catalizador (solo en SMS, no en logs)
    cat_icons = {
        "earnings_beat_raise": "💰",
        "earnings_beat":       "📊",
        "fda_approval":        "💊",
        "merger_acq":          "🤝",
        "contract_gov":        "🏛",
        "upgrade_target":      "📈",
        "sin_catalizador":     "⚠️",
        "otro":                "🔔",
    }
    cat_icon = cat_icons.get(ai_result.get("tipo_catalizador", "otro"), "🔔")

    rvol_str = f" | RVOL {rvol:.1f}x" if rvol > 0 else ""
    vol_line = f"{vol:,} shares{rvol_str}"

    lines = [
        f"PRE-MARKET {direction} — {ticker}{pass_tag} [{prioridad}]",
        f"Movimiento: {change_pct:+.1f}% sostenido (3am-6am Lima)",
        f"Consistencia: {consistency:.0%} | Sin {'retroceso' if direction == 'LONG' else 'rally'} >{adverse:.1f}%",
        f"Precio: ${current:.2f} (prev close ${prev_close:.2f})",
        f"Volumen: {vol_line}",
        "",
        f"{cat_icon} Catalizador: {ai_result['resumen_cataliz']}",
        "",
        f"Entrada: {entry_str} ~${current:.2f}",
        f"Stop: ${stop_price:.2f} (-{ai_result['stop_pct']:.1f}%)",
        f"Target: ${target_price:.2f} (+{ai_result['target_pct']:.1f}%)",
        f"Conviccion IA: {conviccion}/10 | Score tecnico: {code_score}/15",
        "",
        f"Riesgo: {ai_result['riesgo']}",
    ]

    if momentum.get("is_priced_in"):
        lines.append("ADV: >8% de movimiento — posible overextension")
    if momentum.get("early_spike_fading"):
        retrace = momentum.get("early_spike_retrace", 0)
        lines.append(f"ADV: Spike temprano retrocediendo {retrace:.1f}% — menor conviction")

    return "\n".join(lines)


# ── Scanner principal ──────────────────────────────────────────────────────────

def run_premarket_scan(
    watchlist: list,
    twilio_to: str,
    dedup,
    pass_number: int = 1,
    media_tickers: Optional[set] = None,
    metrics=None,
) -> list:
    """
    Escanea el watchlist buscando momentum pre-market sostenido (LONG y SHORT).

    pass_number=1: 6am Lima — todo el watchlist
    pass_number=2: 7am Lima — solo tickers con score MEDIA de la primera pasada

    Retorna lista de tickers MEDIA de esta pasada para re-evaluar en pasada 2.
    """
    now_lima  = datetime.now(LIMA)
    today_str = now_lima.strftime("%Y-%m-%d")

    pm_start  = now_lima.replace(hour=3, minute=0, second=0, microsecond=0)
    pm_end    = min(now_lima, now_lima.replace(hour=9, minute=0, second=0, microsecond=0))
    start_utc = pm_start.astimezone(timezone.utc)
    end_utc   = pm_end.astimezone(timezone.utc)

    tickers_to_scan = (
        [t for t in watchlist if t in media_tickers]
        if (pass_number == 2 and media_tickers)
        else watchlist
    )

    n_scanned    = len(tickers_to_scan)
    n_vol_filter = 0
    n_pass_tech  = 0
    n_code_skip  = 0
    n_alerts     = 0
    media_found  = []

    logger.info(
        f"[PreMarket] Pasada {pass_number} | "
        f"{pm_start.strftime('%H:%M')}-{pm_end.strftime('%H:%M')} Lima | "
        f"{n_scanned} tickers"
    )

    for ticker in tickers_to_scan:
        try:
            if dedup.has_flag(f"premarket_alta_{ticker}_{today_str}"):
                continue

            bars = _get_bars_range(ticker, start_utc, end_utc, timeframe="5Min")
            if not bars:
                continue

            # ── Filtro de volumen mínimo (pre-análisis, sin IA) ────────────────
            total_vol = sum(float(b.get("v", 0)) for b in bars)
            if total_vol < MIN_PREMARKET_VOL:
                logger.debug(
                    f"[PreMarket] {ticker} skip — vol thin "
                    f"({total_vol:,.0f} < {MIN_PREMARKET_VOL:,})"
                )
                n_vol_filter += 1
                continue

            prev_close = _get_prev_close(ticker)
            if prev_close <= 0:
                continue

            current_price = float(bars[-1]["c"])

            # Filtro precio mínimo
            if current_price < MIN_PRICE:
                logger.debug(
                    f"[PreMarket] {ticker} skip — precio ${current_price:.2f} < ${MIN_PRICE}"
                )
                continue

            raw_change = (current_price - prev_close) / prev_close * 100

            candidate_dirs = []
            if raw_change >= LONG_MIN_PCT:
                candidate_dirs.append("LONG")
            if raw_change <= -SHORT_MIN_PCT:
                candidate_dirs.append("SHORT")
            if not candidate_dirs:
                continue

            # RVOL — una sola llamada por ticker candidato (solo los que pasan vol+precio)
            avg_vol = _get_avg_premarket_vol(ticker)

            for direction in candidate_dirs:
                momentum = _analyze_momentum(bars, prev_close, direction)

                if not momentum["passes"]:
                    logger.debug(
                        f"[PreMarket] {ticker} {direction} rechazado: "
                        f"{momentum['rejection_reason']}"
                    )
                    continue

                # Inyectar RVOL en momentum
                momentum["rvol"] = round(total_vol / avg_vol, 2) if avg_vol > 0 else 0.0

                n_pass_tech += 1

                # ── Pre-market code conviction score (0-15) ──────────────────
                code_score, score_breakdown = _premarket_code_score(
                    ticker, momentum, bars, direction
                )

                logger.info(
                    f"[PreMarket] {ticker} {direction} | "
                    f"{momentum['total_change_pct']:+.1f}% | "
                    f"vol {total_vol:,.0f} | rvol {momentum['rvol']:.1f}x | "
                    f"code={code_score}/15 "
                    f"(v{score_breakdown['vol_score']} r{score_breakdown['rvol_score']} "
                    f"m{score_breakdown['momentum_score']} t{score_breakdown['time_score']} "
                    f"d{score_breakdown['day_score']} p{score_breakdown['pattern_score']} "
                    f"pen{score_breakdown['penalties']})"
                )

                if code_score < CODE_SCORE_MIN_FOR_AI:
                    logger.info(
                        f"[PreMarket] {ticker} {direction} skip IA — "
                        f"code_score={code_score}/15 < {CODE_SCORE_MIN_FOR_AI}"
                    )
                    n_code_skip += 1
                    continue

                # Elevar umbral ALTA si movimiento ya está priceado
                score_alta_effective = SCORE_ALTA + 1 if momentum["is_priced_in"] else SCORE_ALTA

                news_items = _fetch_recent_news(ticker)
                ai_result  = _score_premarket_with_ai(
                    ticker, direction, momentum, news_items, prev_close,
                    code_score, score_breakdown,
                )
                conviccion = ai_result["conviccion"]

                logger.info(
                    f"[PreMarket] {ticker} {direction} IA={conviccion}/10 "
                    f"cat={ai_result['tipo_catalizador']} "
                    f"entry={ai_result['entry_style']} | "
                    f"{ai_result['resumen_cataliz'][:65]}"
                )

                sms_sent      = False
                final_prioridad = "BAJA"

                if conviccion >= score_alta_effective:
                    final_prioridad = "ALTA"
                    body = _format_premarket_sms(
                        ticker, direction, momentum, ai_result, prev_close,
                        code_score, score_breakdown,
                        is_second_pass=(pass_number == 2),
                    )
                    sms_sent = send_raw_message(body, twilio_to)
                    if sms_sent:
                        n_alerts += 1
                        dedup.set_flag(f"premarket_alta_{ticker}_{today_str}", ttl_hours=20)
                        dedup.set_flag(f"premarket_media_{ticker}_{today_str}", ttl_hours=20)
                        logger.info(
                            f"[PreMarket] SMS ALTA: {ticker} {direction} "
                            f"IA={conviccion} code={code_score} | "
                            f"{momentum['total_change_pct']:+.1f}%"
                        )

                elif conviccion >= SCORE_MEDIA and pass_number == 1:
                    final_prioridad = "MEDIA"
                    media_key = f"premarket_media_{ticker}_{today_str}"
                    if not dedup.has_flag(media_key):
                        body = _format_premarket_sms(
                            ticker, direction, momentum, ai_result, prev_close,
                            code_score, score_breakdown,
                        )
                        sms_sent = send_raw_message(body, twilio_to)
                        if sms_sent:
                            n_alerts += 1
                            media_found.append(ticker)
                            dedup.set_flag(media_key, ttl_hours=20)
                            logger.info(
                                f"[PreMarket] SMS MEDIA: {ticker} {direction} "
                                f"IA={conviccion} code={code_score} — re-eval 7am"
                            )

                else:
                    logger.info(
                        f"[PreMarket] {ticker} {direction} silencio — "
                        f"IA={conviccion}/10 insuficiente"
                    )

                # Registrar en métricas para el dashboard (todos los candidatos que pasaron IA)
                if metrics:
                    metrics.log_premarket_scan({
                        "scan_date":       today_str,
                        "pass_number":     pass_number,
                        "ticker":          ticker,
                        "direction":       direction,
                        "code_score":      code_score,
                        "ai_conviccion":   conviccion,
                        "ai_continuacion": ai_result["continuacion"],
                        "tipo_catalizador": ai_result["tipo_catalizador"],
                        "resumen_cataliz": ai_result["resumen_cataliz"],
                        "entry_style":     ai_result["entry_style"],
                        "change_pct":      momentum["total_change_pct"],
                        "rvol":            momentum.get("rvol", 0),
                        "total_vol":       momentum.get("total_premarket_vol", 0),
                        "stop_pct":        ai_result["stop_pct"],
                        "target_pct":      ai_result["target_pct"],
                        "prioridad":       final_prioridad,
                        "sms_sent":        sms_sent,
                    })

        except Exception as e:
            logger.error(f"[PreMarket] Error en {ticker}: {e}")

    logger.info(
        f"[PreMarket] Pasada {pass_number} completada: "
        f"{n_scanned} escaneados | {n_vol_filter} vol-thin | "
        f"{n_pass_tech} pasan tecnico | {n_code_skip} skip-IA | "
        f"{n_alerts} alertas"
        + (f" | {len(media_found)} re-eval 7am" if media_found else "")
    )
    return media_found
