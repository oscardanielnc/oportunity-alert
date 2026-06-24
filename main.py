"""
OportunityAlert — Loop principal 24/7
Uso:
  python main.py          → arranca el monitor
  python main.py status   → muestra resumen sin arrancar el monitor
"""
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
import threading
from datetime import datetime, timezone, timedelta

# Cargar .env automáticamente (localmente; en VM lo hace systemd EnvironmentFile)
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)  # override=False: variables ya en entorno tienen prioridad
except ImportError:
    pass

from sources.edgar import fetch_8k_filings
from sources.finnhub_news import fetch_company_news, fetch_market_news
from sources.reddit_monitor import fetch_reddit_mentions
from sources.alpaca_news import stream_news as alpaca_stream_news
from filters.keyword_filter import passes_filter, TIER_1_KEYWORDS
from filters.claude_scorer import score_with_claude
from utils.news_context import build_context, marea_leader_tag
from alerts.twilio_sms import send_sms, format_sms, send_raw_message as _send_twilio_raw
from alerts.alert_logger import log_alert, log_claude_analysis, update_24h_prices, get_recent_alerts
from utils.alpaca_price import get_realtime_price
from utils.dedup_store import DedupStore
from utils.etoro_client import get_portfolio, check_portfolio_gate
from utils.conviction_gates import evaluate_conviction
from utils.playbook_matcher import find_matching_strategies
from utils.metrics_store import MetricsStore, DB_PATH as _METRICS_DB
from utils import scoreboard
from utils.event_gate import detect_event_type, is_event_mode, event_gate2_score, format_event_watch_sms
from utils.delayed_alerts import queue_followup, start_worker
from utils.earnings_calendar import has_earnings_today_or_yesterday
from utils.signal_score import classify_category, compute_signal_score, should_send_sms
# premarket_scanner ARCHIVADO (2026-05-30) → _deprecated/. Sin edge validado (backtest).
from api.app import run_dashboard

LIMA = timezone(timedelta(hours=-5))

# Tickers crypto-correlacionados: alertar inmediatamente aunque mercado esté cerrado
# (informan trades en Binance o preparación para apertura del lunes)
CRYPTO_ALWAYS_ALERT = {"MSTR", "MARA", "RIOT", "IREN"}

# ── Contadores globales para heartbeat ───────────────────────────────────────
_stats = {"analyzed": 0, "alta": 0, "media": 0, "baja": 0, "sms_sent": 0, "dedup": 0, "filtered": 0}
_stats_lock = threading.Lock()

# Singleton de métricas — inicializado en main()
_metrics: MetricsStore = None

# ── Cola de acumulación durante mercado cerrado ───────────────────────────────
# Persistida a disco: si el servicio se reinicia un fin de semana (deploy, crash),
# las oportunidades acumuladas NO se pierden — se recargan al arrancar.
_weekend_queue: list[dict] = []
_weekend_lock = threading.Lock()
_WEEKEND_QUEUE_PATH = "data/weekend_queue.json"


def _save_weekend_queue():
    """Persiste la cola a disco de forma atómica (tmp+replace). Llamar con el lock tomado."""
    try:
        import os as _os
        _os.makedirs("data", exist_ok=True)
        tmp = _WEEKEND_QUEUE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_weekend_queue, f, ensure_ascii=False)
        _os.replace(tmp, _WEEKEND_QUEUE_PATH)
    except Exception as e:
        logger.warning(f"[WeekendQueue] no pude persistir: {e}")


def _load_weekend_queue():
    """Recarga la cola desde disco al arrancar. Silencioso si no existe."""
    global _weekend_queue
    try:
        import os as _os
        if _os.path.exists(_WEEKEND_QUEUE_PATH):
            with open(_WEEKEND_QUEUE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                _weekend_queue = data
                logger.info(f"[WeekendQueue] {len(data)} oportunidades recargadas de disco")
    except Exception as e:
        logger.warning(f"[WeekendQueue] no pude recargar: {e}")


# ── Logging: Lima timezone + RotatingFileHandler ─────────────────────────────

class _LimaFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        lima_dt = datetime.fromtimestamp(record.created, tz=LIMA)
        return lima_dt.strftime("%Y-%m-%d %H:%M:%S Lima")


def _setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = _LimaFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    os.makedirs("data", exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        "opportunity_alert.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Silenciar librerías verbosas
    for lib in ("twilio", "twilio.http_client", "httpx", "httpcore"):
        logging.getLogger(lib).setLevel(logging.WARNING)


logger = logging.getLogger("main")


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str = "config.json") -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_watchlist(config: dict) -> list[str]:
    """
    Fuente ÚNICA canónica = tabla `watchlist` en metrics.db (la gestiona el frontend).
    config.json se usa solo como SEED inicial / fallback si la DB está vacía.
    (watchlist.txt / watchlist.py DEPRECADOS 2026-05-30 → _deprecated/.)
    """
    tickers = []

    # 1. DB watchlist — fuente canónica (gestionada por el frontend / metrics.db)
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).parent / "data" / "metrics.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute(
                "SELECT ticker FROM watchlist WHERE active=1 ORDER BY category, ticker"
            ).fetchall()
            conn.close()
            tickers.extend(row[0] for row in rows)
    except Exception:
        pass

    # 2. config.json — solo seed/fallback (merge de los que no estén ya en DB)
    wl = config.get("watchlist", {})
    for cat in ["primary", "extended", "crypto"]:
        tickers.extend(wl.get(cat, []))

    return list(dict.fromkeys(tickers))  # dedup preservando orden


def get_live_watchlist() -> list[str]:
    """
    Versión dinámica: consulta la DB en tiempo real.
    Usada por los loops para detectar tickers añadidos sin reiniciar el servicio.
    """
    if _metrics is None:
        return []
    try:
        return [row["ticker"] for row in _metrics.get_watchlist()]
    except Exception:
        return []


def get_env_or_die(env_var: str) -> str:
    val = os.environ.get(env_var, "")
    if not val:
        logger.error(f"Variable requerida no configurada: {env_var}")
        sys.exit(1)
    return val


# ── Watchdog de threads (2026-06-10) ─────────────────────────────────────────
# El supervisor reinicia threads MUERTOS, pero uno COLGADO (ej. socket half-open)
# quedaba vivo y nadie lo detectaba. Cada loop reporta su último ciclo OK con
# _beat(); el heartbeat diario denuncia a los que llevan >3× su intervalo sin latir.
_thread_health: dict = {}     # {name: (last_ok_epoch, expected_interval_s)}
_thread_health_lock = threading.Lock()


def _beat(name: str, interval_s: float):
    """Marca 'ciclo OK' de un loop. Llamar al final de cada iteración exitosa."""
    with _thread_health_lock:
        _thread_health[name] = (time.time(), interval_s)


def _stale_threads() -> list:
    """Threads cuyo último ciclo OK fue hace > max(3×intervalo, 30 min)."""
    now = time.time()
    stale = []
    with _thread_health_lock:
        for name, (last_ok, interval) in _thread_health.items():
            limit = max(3 * interval, 1800)
            if now - last_ok > limit:
                stale.append(f"{name} ({(now - last_ok) / 3600:.1f}h)")
    return stale


# ── Supervisor de threads ─────────────────────────────────────────────────────

def is_market_open() -> bool:
    """
    Retorna True si eToro permite operar acciones US (sesión regular + after-hours).

    Sesiones cubiertas (hora Lima, UTC-5):
      - Lun-Jue: 00:00 → 23:59  (sin corte — pre-market inicia 3am Lima / 4am EDT)
      - Viernes: 00:00 → 20:00  (after-hours eToro cierra ~8pm EDT = 7pm Lima;
                                  antes cortaba a las 15:00 Lima y mandaba earnings
                                  post-market al digest del fin de semana — bug corregido)
      - Sábado:  CERRADO (digest acumula para domingo)
      - Domingo: CERRADO hasta 19:00 Lima (eToro reabre ~7pm Lima con mercados asiáticos)
    """
    now = datetime.now(LIMA)
    weekday = now.weekday()   # 0=lun … 4=vie … 5=sab … 6=dom
    total_minutes = now.hour * 60 + now.minute

    if weekday == 5:                                  # sábado completo
        return False
    if weekday == 6 and total_minutes < 19 * 60:      # domingo antes de 19:00 Lima
        return False
    if weekday == 4 and total_minutes >= 20 * 60:     # viernes ≥ 20:00 Lima
        return False
    return True


def _supervised_thread(name: str, target_fn, *args, **kwargs):
    """
    Lanza target_fn en un loop supervisado.
    Si falla, espera con backoff exponencial y reinicia automáticamente.
    Backoff: 15s → 30s → 60s (tope).
    """
    delay = 15
    while True:
        started = time.time()
        try:
            logger.info(f"[{name}] Thread iniciado")
            target_fn(*args, **kwargs)
            # Si retorna sin error (no debería en loops normales)
            logger.warning(f"[{name}] Loop terminó inesperadamente. Reiniciando en {delay}s")
        except Exception as exc:
            logger.error(f"[{name}] Caida inesperada: {exc}. Reiniciando en {delay}s")
        # Si corrió estable >10 min antes de caer, resetear el backoff (antes quedaba
        # en 60s para siempre tras 3 fallos históricos).
        if time.time() - started > 600:
            delay = 15
        time.sleep(delay)
        delay = min(delay * 2, 60)


# ── Lógica central ────────────────────────────────────────────────────────────


# 2026-06-10: el vocabulario bajista vive ahora en keyword_filter (donde el FILTRO
# también lo usa — antes solo se reconocía lo bajista DESPUÉS de que el filtro alcista
# ya lo había matado). Acá solo se reusa para la heurística de dirección.
from filters.keyword_filter import ALL_BEARISH_KEYWORDS as _BEARISH_KEYWORDS


def _guess_direction(article: dict) -> str:
    """
    Heurística rápida: si el artículo contiene señales bajistas claras → SHORT.
    En caso de duda → LONG (más frecuente en catalizadores positivos).
    """
    text = article.get("raw_text", "").upper()
    if any(kw in text for kw in _BEARISH_KEYWORDS):
        return "SHORT"
    return "LONG"


_summary_last_refresh = 0.0
_summary_lock = threading.Lock()


def _refresh_summary_throttled(min_interval_s: float = 300):
    """refresh_daily_summary corría por CADA artículo IA (varios SQL agregados en el
    camino caliente del pipeline). Throttle a 1 vez/5 min — el resumen es informativo,
    no necesita estar al segundo (2026-06-10)."""
    global _summary_last_refresh
    with _summary_lock:
        if time.time() - _summary_last_refresh < min_interval_s:
            return
        _summary_last_refresh = time.time()
    try:
        if _metrics:
            _metrics.refresh_daily_summary()
    except Exception as e:
        logger.debug(f"[Metrics] refresh throttled: {e}")


def _portfolio_gate(ticker: str) -> dict:
    """
    Gate 5 (portfolio eToro) leído del account_cache que mantiene el PositionTracker
    cada 10 min, para NO bloquear el pipeline de noticias con una llamada sincrónica
    a eToro (hasta 5s, peor con el circuit breaker medio-abierto). Cae a consulta en
    vivo solo si el cache no existe o está viejo (>20 min ≈ tracker caído).
    """
    try:
        from utils.position_strategy import read_account_cache, account_cache_age_minutes
        cache = read_account_cache()
        age = account_cache_age_minutes()
        if cache and age is not None and age < 20 and "positions" in cache:
            cached_pf = {
                "positions": cache.get("positions", []),
                "available_cash": cache.get("available_cash", 0),
                "total_value": cache.get("total_value", 0),
                "error": None,
            }
            return check_portfolio_gate(ticker, portfolio=cached_pf)
    except Exception as e:
        logger.debug(f"[Gate5] cache no usable ({e}); fallback a eToro live")
    return check_portfolio_gate(ticker)


def process_article(
    article: dict,
    watchlist: list[str],
    dedup: DedupStore,
    config: dict,
    twilio_from: str,
    twilio_to: str,
    send_priorities: list[str],
    log_priorities: list[str],
) -> None:
    article_id = article.get("id", "")

    # Radar Trump (2026-06-18): chequeo barato sobre TODO artículo (con dedup propio).
    # Va ANTES del filtro de watchlist para no perder declaraciones macro de Trump que
    # no nombran un ticker del universo (guerra/tariffs/Fed). El gate por keywords
    # descarta en <1ms lo que no menciona a Trump.
    try:
        from utils import trump_tracker
        if trump_tracker.mentions_trump(article):
            trump_tracker.capture_headline(article, watchlist=watchlist, held=_held_tickers())
    except Exception:
        pass

    if dedup.is_seen(article_id):
        with _stats_lock:
            _stats["dedup"] += 1
        return

    passes, reason = passes_filter(
        article=article,
        watchlist=watchlist,
        seen_ids=set(),
        # None → usa la lógica source-aware en passes_filter (SEC_EDGAR=4h, Finnhub=90min).
        # Si config.json define "max_article_age_minutes", ese valor overridea todo.
        max_age_minutes=config.get("max_article_age_minutes"),
        # Bypass de posición: noticias bajistas sobre tickers EN CARTERA pasan aunque
        # el keyword score sea bajo (hueco confirmado en el crash 06-04/06-10).
        held_tickers=_held_tickers(),
    )
    if not passes:
        with _stats_lock:
            _stats["filtered"] += 1
        logger.debug(f"Filtro [{reason}]: {article.get('title', '')[:70]}")
        if _metrics:
            _metrics.log_article_filter({
                "ticker": (article.get("tickers_found") or ["UNKNOWN"])[0],
                "source": article.get("source", ""),
                "title":  article.get("title", ""),
                "age_minutes": article.get("age_minutes", 0),
                "stage":  "keyword_filtered",
                "reason": reason,
                "keyword_score": article.get("keyword_score", 0),
            })
        dedup.mark_seen(article_id, article.get("source", ""))
        return

    ticker = (article.get("tickers_found") or ["UNKNOWN"])[0]
    source = article.get("source", "")
    age = article.get("age_minutes", 0)
    score = article.get("keyword_score", 0)
    raw_text = article.get("raw_text", "")

    # Dedup cross-source (mismo evento, distinta fuente). Ventana = 120 min para CUBRIR la
    # edad source-aware de Finnhub/Yahoo (hasta 90 min) + jitter de poll: si Alpaca/Benzinga
    # (push en segundos) ya trajo y PROCESÓ el evento, la copia tardía de Finnhub se descarta;
    # si nunca se procesó (sent_to_claude=0), la 2da copia SÍ pasa (estar al tanto no daña).
    cross_dedup_min = config.get("cross_source_dedup_minutes", 120)
    fingerprint = dedup.get_event_fingerprint(ticker, raw_text)
    if dedup.is_cross_source_duplicate(ticker, fingerprint, window_minutes=cross_dedup_min):
        logger.info(f"[CROSS-DUP] {ticker} mismo evento <{cross_dedup_min}min — skip | {article.get('title', '')[:60]}")
        dedup.mark_seen(article_id, source)
        return

    logger.info(
        f"[CANDIDATO] {ticker} | {source} | {age:.0f}min | "
        f"score={score} | {article.get('title', '')[:70]}"
    )

    # ── Precio real-time ──────────────────────────────────────────────────────
    price_data = get_realtime_price(ticker)
    last_alert = dedup.get_last_alert_on_ticker(ticker, hours=4)

    # ── Detección de tipo de evento ───────────────────────────────────────────
    event_type      = detect_event_type(article, price_data)
    event_mode      = is_event_mode(event_type)
    initial_dir     = _guess_direction(article)

    # ── Catalizador fresco fuerte? (decide si un movimiento ya priceado pasa como
    #    CONTINUACIÓN en vez de descartarse). Solo Tier-1 o earnings/FDA — no drift. ──
    _t1 = set(TIER_1_KEYWORDS)
    fresh_catalyst = (
        any(kw in _t1 for kw in (article.get("keywords_found") or []))
        or event_type in ("earnings", "fda", "government_contract")
    )

    # ── Conviction Gates v2.0 (código puro, < 1 segundo, sin tokens) ──────────
    conviction = evaluate_conviction(
        ticker, price_data, direction=initial_dir,
        allow_priced_momentum=fresh_catalyst,
    )

    # En event mode: Gate 2 técnico se reemplaza por análisis de velas 1m.
    # Usa initial_dir (no "LONG" hardcodeado): un earnings miss / FDA reject es SHORT,
    # y la estabilización debe evaluarse en ESA dirección (lower_highs, no higher_lows).
    if event_mode:
        ev_g2 = event_gate2_score(price_data.get("candles_1m", []), direction=initial_dir)
        # Recalcular score total con el gate2 de eventos
        old_total = conviction["conviction_score"]
        conviction["gate2_score"]      = ev_g2
        conviction["conviction_score"] = conviction["gate1_score"] + ev_g2 + conviction["gate3_score"]
        # En event mode solo Gate1 puede bloquear — salvo continuación (catalizador fresco
        # sobre un movimiento ya priceado): esa pasa a la IA como ADVERTENCIA.
        conviction["skip_ai"] = (
            conviction["gate1_score"] == 0 and not conviction.get("momentum_continuation")
        )
        logger.info(
            f"[EVENT-MODE] {ticker} tipo={event_type} | "
            f"gate2 tecnico reemplazado por estabilizacion ({old_total}→{conviction['conviction_score']}/7)"
        )

    # Bypass de posición (2026-06-10): una noticia BAJISTA sobre una posición abierta
    # llega SIEMPRE a la IA aunque los gates digan skip (Gate1=0 = "ya priceado" es
    # exactamente el caso de un crash en curso — cuando más importa avisar). El costo
    # es ≤ unas pocas llamadas extra en días rojos; el riesgo de callar es perder plata.
    if conviction["skip_ai"] and article.get("held_bypass"):
        conviction["skip_ai"] = False
        conviction["reasoning"] = (
            (conviction.get("reasoning") or "")
            + " | held_bypass: noticia bajista sobre posición abierta → IA"
        )

    if conviction["skip_ai"]:
        stage = "gate1_blocked" if conviction["gate1_score"] == 0 else "gates_blocked"
        logger.info(
            f"[GATES] {ticker} score={conviction['conviction_score']}/7 "
            f"→ DESCARTADO sin IA | {conviction['reasoning']}"
        )
        if _metrics:
            _metrics.log_gate_event(
                ticker, source, conviction,
                ai_called=False,
                article_title=article.get("title", ""),
            )
            _metrics.log_article_filter({
                "ticker": ticker, "source": source,
                "title": article.get("title", ""), "age_minutes": age,
                "stage": stage, "reason": conviction.get("reasoning", ""),
                "keyword_score": score,
                "g1": conviction["gate1_score"], "g2": conviction["gate2_score"],
                "g3": conviction["gate3_score"], "conv": conviction["conviction_score"],
                "skip_ai": True, "event_mode": event_mode,
            })
        dedup.mark_seen(article_id, source)
        dedup.mark_ticker_event(ticker, fingerprint, source, sent_to_claude=False)
        return

    logger.info(
        f"[GATES] {ticker} score={conviction['conviction_score']}/7 → pasa a IA | "
        f"{'EVENT-MODE ' + event_type if event_mode else 'RSI=' + str(round(conviction.get('gate2_rsi', 0), 1))} | "
        f"{conviction['reasoning']}"
    )

    # ── Contexto por CÓDIGO (sector/macro, trayectoria, noticias previas) ─────
    # Todo lo computable sin IA va acá → la IA solo razona lo que no se puede calcular.
    earnings_flag = has_earnings_today_or_yesterday(ticker)
    recent_news   = _metrics.get_recent_news_for_ticker(ticker, minutes=90) if _metrics else None
    context_text, _ctx = build_context(
        ticker, price_data, conviction, article,
        recent_news=recent_news,
        earnings_soon=earnings_flag,
        momentum_continuation=conviction.get("momentum_continuation", False),
    )

    # ── Scoring IA (prompt con contexto — stops/targets los calcula conviction) ──
    # El motor/modelo del tier FUERTE lo resuelve claude_scorer vía resolve_engine_model
    # ("strong" → default deepseek-v4-pro). Override fino: AI_MODEL_STRONG.
    result = score_with_claude(
        article, price_data, conviction=conviction, context_text=context_text,
    )

    score_ia  = int(result.get("score_ia") or 0)
    prioridad = result.get("prioridad", "BAJA")
    direction = result.get("direccion", "")

    # ── NUEVO score de ACCIÓN (reemplaza al score de convicción como guía de decisión) ──
    # Estudio 2026-06-20: el score de convicción/IA NO predice la reacción (dir_ok ~50%).
    # Lo que sí importa: CATEGORÍA del catalizador + FRESCURA + confirmación de precio. Se
    # computa acá (necesita resumen+dirección de la IA) y gatea el SMS. Se persiste en raw_json.
    signal_cat = classify_category(
        f"{result.get('resumen_cataliz','')} {article.get('title','')} {article.get('url','')}"
    )
    signal_detail = compute_signal_score(
        signal_cat, article.get("age_minutes"), price_data.get("change_pct"), direction
    )
    result["signal_score"]     = signal_detail["score"]
    result["signal_categoria"] = signal_cat
    result["signal_breakdown"] = signal_detail

    # ── Gate 4: Playbook match (informativo) ──────────────────────────────────
    playbook_matches = find_matching_strategies(result.get("resumen_cataliz", ""))
    result["playbook_matches"] = playbook_matches

    # ── Rescate filtro EARNINGS (validado: el pop intradía en día de earnings es perdedor) ──
    # No descarta la alerta; la marca para que Oscar no persiga el pop y considere PED multi-día.
    if earnings_flag:
        result["earnings_day"] = True
        result["resumen_cataliz"] = (
            "[⚠️ EARNINGS hoy/ayer — el gap ya priceó, NO chase intradía. "
            "Si mega-cap+beat fuerte: jugar PED Day+2→Day+7] "
            + result.get("resumen_cataliz", "")
        )

    # ── Gate 5: Portfolio gate eToro (puede reducir score si no puede entrar) ─
    # Lee del account_cache (lo refresca el PositionTracker) → sin llamada sincrónica
    # a eToro que bloquearía este thread. Fallback a live si el cache está viejo.
    portfolio_gate = _portfolio_gate(ticker)
    result["portfolio_gate"] = portfolio_gate

    if not portfolio_gate["can_enter"] and score_ia >= 9:
        score_ia = max(7, score_ia - 2)
        result["score_ia"] = score_ia
        result["prioridad"] = prioridad  # sigue ALTA (7+)
        result["resumen_cataliz"] = (
            f"[Portfolio: {portfolio_gate['reason']}] "
            + result.get("resumen_cataliz", "")
        )

    log_claude_analysis(result)

    # Registrar en métricas
    if _metrics:
        _metrics.log_gate_event(
            ticker, source, conviction,
            ai_called=True,
            ai_prioridad=prioridad,
            article_title=article.get("title", ""),
        )
    dedup.mark_ticker_event(ticker, fingerprint, source, direction=direction, sent_to_claude=True)

    # Actualizar stats para heartbeat
    with _stats_lock:
        _stats["analyzed"] += 1
        if prioridad == "ALTA":
            _stats["alta"] += 1
        elif prioridad == "MEDIA":
            _stats["media"] += 1
        elif prioridad == "BAJA":
            _stats["baja"] += 1

    # Añadir flag de cambio de dirección si aplica
    if last_alert and last_alert.get("direction") and direction:
        if last_alert["direction"] != direction:
            result["resumen_cataliz"] = (
                f"[CAMBIO {last_alert['direction']}->{direction}] "
                + result.get("resumen_cataliz", "")
            )

    # ── Fase 3: SALIDA event-driven — noticia ADVERSA sobre una posición abierta ──
    # Si la noticia es bajista (direccion=SHORT) y de alta convicción sobre un ticker
    # que YA tenemos en cartera (sistema long-only), avisar URGENTE para considerar
    # salir. Inmediata 24/5 (Oscar opera en eToro fuera de horario USA) → NO se encola
    # al digest del domingo. Dedup: 1 alerta por ticker/día (re-avisa al día siguiente).
    adverse_exit = False
    if direction == "SHORT" and ticker.upper() in _held_tickers() \
            and (score_ia >= ADVERSE_EXIT_SCORE_MIN or event_mode):
        day = datetime.now(LIMA).strftime("%Y%m%d")
        flag_key = f"advnews_{ticker}_{day}"
        if not dedup.has_flag(flag_key):
            _send_adverse_news_alert(ticker, result, twilio_to)
            dedup.set_flag(flag_key, ttl_hours=20)
            with _stats_lock:
                _stats["sms_sent"] += 1
            logger.info(
                f"[AdverseNews] URGENTE — {ticker}: noticia adversa sobre posición "
                f"abierta (score={score_ia}, dir=SHORT, event={event_type})"
            )
        adverse_exit = True   # no duplicar con el flujo normal de oportunidad

    # Umbral de SMS por el NUEVO score de acción (categoría+frescura+confirmación). El score_ia
    # viejo ya no decide (no predecía). Tunable sin tocar código: config min_signal_score_sms.
    min_signal_sms = config.get("min_signal_score_sms", 6)
    sms_enviado = False
    # Movimiento ya priceado (continuación) = por defecto SOLO dashboard, sin SMS
    # (decisión Oscar 2026-06-01: cero ruido por drift). EXCEPCIÓN (Hueco 2, caso MRVL
    # 2-jun): si el catalizador es FUERTE y fresco (Tier-1 / earnings / FDA) y la IA igual
    # lo valoró, SÍ avisamos — un endorsement de CEO + nuevo máximo es tendencia montable
    # (Marea/PED), no ruido. Se manda UN SMS informativo de continuación, dedup 1/ticker/día,
    # con umbral propio (la convicción viene castigada por Gate1=0 → no exigir 7).
    is_warning = bool(conviction.get("momentum_continuation"))
    min_score_priced = config.get("min_score_priced_sms", 5)
    strong_priced = (
        is_warning and fresh_catalyst and score_ia >= min_score_priced
    )
    if strong_priced:
        is_warning = False   # deja de suprimirse: cae al flujo de SMS de abajo

    # Catalizador fuerte ya priceado → SMS informativo de continuación (no señal de entrada:
    # ya corrió). No usa el flujo de evento (watch + followup): no hay entrada que confirmar.
    if strong_priced and not adverse_exit:
        market_open   = is_market_open()
        crypto_ticker = ticker in CRYPTO_ALWAYS_ALERT
        day = datetime.now(LIMA).strftime("%Y%m%d")
        flag_key = f"priced_{ticker}_{day}"
        if (market_open or crypto_ticker) and not dedup.has_flag(flag_key):
            chg = price_data.get("change_pct")
            chg_txt = f"{chg:+.0f}% hoy" if chg is not None else "movimiento fuerte"
            # Cruce con Marea: si el ticker ya es líder ⭐/breakout, el catalizador NO es
            # ruido priceado — es la oportunidad que Marea rankeaba (caso MRVL).
            leader_tag = marea_leader_tag(ticker)
            lead_prefix = f"[{leader_tag}] " if leader_tag else ""
            result["resumen_cataliz"] = (
                lead_prefix
                + f"[🔥 YA PRICEADO ({chg_txt}) — CONTINUACIÓN/MOMENTUM, NO persigas el pop; "
                f"evaluá para TENDENCIA (Marea/PED), no scalp] "
                + result.get("resumen_cataliz", "")
            )
            sms_enviado = send_sms(result, twilio_from, twilio_to)
            if sms_enviado:
                dedup.set_flag(flag_key, ttl_hours=20)
                with _stats_lock:
                    _stats["sms_sent"] += 1
                logger.info(
                    f"[PricedMomentum] {ticker} SMS de continuación enviado "
                    f"(score_ia={score_ia}, change={chg}) — catalizador fuerte ya priceado"
                )
        adverse_exit = True   # no duplicar con el flujo normal de oportunidad de abajo

    if signal_detail["score"] >= min_signal_sms and not adverse_exit and not is_warning:
        market_open   = is_market_open()
        crypto_ticker = ticker in CRYPTO_ALWAYS_ALERT

        if market_open or crypto_ticker:
            if event_mode:
                # ── Flujo evento: SMS 1 (aviso) + SMS 2 encolado ─────────────
                sms1_body = format_event_watch_sms(
                    ticker, event_type, result, price_data, followup_minutes=7
                )
                sms_enviado = bool(_send_twilio_raw(sms1_body, twilio_to))
                if sms_enviado:
                    with _stats_lock:
                        _stats["sms_sent"] += 1
                    dedup.set_flag(f"smssent_{ticker}_{datetime.now(LIMA).strftime('%Y%m%d')}", ttl_hours=20)
                    queue_followup(
                        ticker, article, conviction, result,
                        event_type, twilio_to,
                    )
                    logger.info(f"[EVENT] {ticker} SMS1 enviado ({event_type}) — SMS2 encolado en 7 min")
            else:
                # ── Flujo normal ──────────────────────────────────────────────
                sms_enviado = send_sms(result, twilio_from, twilio_to)
                if sms_enviado:
                    with _stats_lock:
                        _stats["sms_sent"] += 1
                    dedup.set_flag(f"smssent_{ticker}_{datetime.now(LIMA).strftime('%Y%m%d')}", ttl_hours=20)
        else:
            # Mercado cerrado: acumular para el digest del domingo 7pm Lima
            with _weekend_lock:
                _weekend_queue.append(result)
                _save_weekend_queue()   # persistir → sobrevive reinicios de fin de semana
            logger.info(
                f"[ACUMULADO] {ticker} score={score_ia}/10 — mercado cerrado, "
                f"se incluirá en digest del domingo 7pm Lima "
                f"(cola: {len(_weekend_queue)} items)"
            )
            if _metrics:
                _metrics.log_article_filter({
                    "ticker": ticker, "source": source,
                    "title": article.get("title", ""), "age_minutes": age,
                    "stage": "market_closed", "reason": "mercado cerrado — digest domingo",
                    "keyword_score": score,
                    "g1": conviction["gate1_score"], "g2": conviction["gate2_score"],
                    "g3": conviction["gate3_score"], "conv": conviction["conviction_score"],
                    "ai_score": score_ia, "prioridad": prioridad, "event_mode": event_mode,
                })

    if score_ia >= 1:  # loguear todo lo que la IA puntuó (excepto errores)
        # Scoreboard de auditoría: registra TODA señal puntuada (medir todo forward; en
        # producción solo se surfacean las de valor). Resolver horario la mide a 48h.
        try:
            scoreboard.record_signal(
                _METRICS_DB, "noticias", ticker, direction, signal_cat,
                signal_detail["score"], source, age,
                datetime.now(timezone.utc).isoformat(),
                price_data.get("current_price") or result.get("entry_price"),
            )
        except Exception as _e:
            logger.debug(f"[scoreboard] record noticias: {_e}")
        log_alert(result, sms_enviado=sms_enviado, _skip_analysis_log=True)
        if _metrics:
            _metrics.log_alert(result, conviction=conviction, sms_enviado=sms_enviado)
            _refresh_summary_throttled()
            # Log final del pipeline para todos los que pasaron IA
            _metrics.log_article_filter({
                "ticker": ticker, "source": source,
                "title": article.get("title", ""), "age_minutes": age,
                "stage": "sent" if sms_enviado else ("ai_alta" if prioridad == "ALTA" else
                          "ai_media" if prioridad == "MEDIA" else "ai_baja"),
                "reason": result.get("resumen_cataliz", ""),
                "keyword_score": score,
                "g1": conviction["gate1_score"], "g2": conviction["gate2_score"],
                "g3": conviction["gate3_score"], "conv": conviction["conviction_score"],
                "ai_score": score_ia, "prioridad": prioridad,
                "sms_sent": sms_enviado, "event_mode": event_mode,
            })

    # Brief en background: una noticia ALTA es "algo que vale la pena mostrar" → regenera
    # la narrativa del Brief Diario (throttled, en thread aparte: no bloquea el pipeline).
    if prioridad == "ALTA":
        _trigger_brief_regen(f"evento ALTA: {ticker}")

    dedup.mark_seen(article_id, source)


# ── Loops de fuentes ─────────────────────────────────────────────────────────

def edgar_loop(config, watchlist, dedup, **kwargs):
    interval = config.get("intervals_seconds", {}).get("edgar", 90)
    while True:
        try:
            live_wl = get_live_watchlist() or watchlist
            articles = fetch_8k_filings(live_wl)
            with _stats_lock:
                dedup_before = _stats["dedup"]
                filtered_before = _stats["filtered"]
                analyzed_before = _stats["analyzed"]
            for article in articles:
                process_article(article, live_wl, dedup, config, **kwargs)
            with _stats_lock:
                n_dedup = _stats["dedup"] - dedup_before
                n_filtered = _stats["filtered"] - filtered_before
                n_claude = _stats["analyzed"] - analyzed_before
            if n_dedup > 0 or n_filtered > 0 or n_claude > 0:
                logger.info(
                    f"EDGAR ciclo: {len(articles)} filings | "
                    f"{n_dedup} ya vistos | {n_filtered} sin keywords | {n_claude} → IA"
                )
            _beat("EDGAR", interval)
        except Exception as e:
            logger.error(f"Error en EDGAR poll: {e}")
        time.sleep(interval)


def finnhub_loop(config, watchlist, dedup, **kwargs):
    interval = config.get("intervals_seconds", {}).get("finnhub", 120)
    ticker_index = 0
    batch_size = 5
    cycle = 0
    while True:
        try:
            live_wl = get_live_watchlist() or watchlist
            batch = live_wl[ticker_index: ticker_index + batch_size]
            ticker_index = (ticker_index + batch_size) % len(live_wl)
            fetched = 0
            with _stats_lock:
                dedup_before    = _stats["dedup"]
                filtered_before = _stats["filtered"]
                analyzed_before = _stats["analyzed"]
            for ticker in batch:
                articles = fetch_company_news(ticker, hours_back=2)
                fetched += len(articles)
                for article in articles:
                    process_article(article, live_wl, dedup, config, **kwargs)
            market_articles = fetch_market_news()
            for article in market_articles:
                # Radar Trump sobre la noticia de MERCADO general (antes del filtro de
                # ticker): aquí aparecen las declaraciones macro de Trump (tariffs, guerra,
                # Fed) que no nombran un ticker del universo y que process_article saltaría.
                try:
                    from utils import trump_tracker
                    if trump_tracker.mentions_trump(article):
                        trump_tracker.capture_headline(article, watchlist=live_wl, held=_held_tickers())
                except Exception:
                    pass
                if not article.get("tickers_found"):
                    from filters.keyword_filter import extract_tickers_from_text
                    article["tickers_found"] = extract_tickers_from_text(
                        article.get("raw_text", ""), live_wl
                    )
                if article.get("tickers_found"):
                    process_article(article, live_wl, dedup, config, **kwargs)
                    fetched += 1
            with _stats_lock:
                n_dedup    = _stats["dedup"]    - dedup_before
                n_filtered = _stats["filtered"] - filtered_before
                n_claude   = _stats["analyzed"] - analyzed_before
            cycle += 1
            tickers_done = cycle * batch_size
            progress = min(tickers_done, len(live_wl))
            logger.info(
                f"Finnhub: ciclo {cycle} | batch {batch[0] if batch else '?'}-{batch[-1] if batch else '?'} "
                f"({progress}/{len(live_wl)} tickers) | {fetched} obtenidos | "
                f"{n_dedup} vistos | {n_filtered} sin keywords | {n_claude} → IA"
            )
            _beat("Finnhub", interval)
        except Exception as e:
            logger.error(f"Error en Finnhub poll: {e}")
        time.sleep(interval)


_alpaca_news_queue = None      # cola compartida WS → worker (se crea una sola vez)
_alpaca_news_worker_started = False


def alpaca_news_loop(config, watchlist, dedup, **kwargs):
    """
    Stream de noticias en tiempo real de Alpaca (fuente Benzinga) via WebSocket.
    alpaca_stream_news() ya reconecta solo con backoff; este wrapper es la red de
    seguridad por si el stream lanza una excepcion no controlada.

    BUG FIX 2026-06-10: antes process_article corría SINCRÓNICO en el thread del WS
    (fetch Alpaca 8s + IA + Twilio = el recv() quedaba bloqueado). En días de ráfaga
    (selloff = decenas de titulares) los mensajes se encolaban en el server y, si el
    stall superaba el keepalive, Alpaca desconectaba SIN replay → noticias perdidas.
    Ahora el WS solo encola; un worker dedicado procesa.
    """
    import queue as _queue
    global _alpaca_news_queue, _alpaca_news_worker_started

    if _alpaca_news_queue is None:
        _alpaca_news_queue = _queue.Queue(maxsize=500)

    def _news_worker():
        while True:
            article = _alpaca_news_queue.get()
            try:
                live_wl = get_live_watchlist() or watchlist
                process_article(article, live_wl, dedup, config, **kwargs)
            except Exception as e:
                logger.error(f"[AlpacaNews] error procesando noticia (worker): {e}")
            finally:
                _alpaca_news_queue.task_done()

    # Un solo worker (mantiene el orden y no agrava la race de dedup cross-source).
    # Flag global: si el supervisor reinicia este loop, NO duplicar el worker.
    if not _alpaca_news_worker_started:
        threading.Thread(target=_news_worker, name="AlpacaNewsWorker", daemon=True).start()
        _alpaca_news_worker_started = True

    def on_article(article):
        try:
            _alpaca_news_queue.put_nowait(article)
        except _queue.Full:
            # Backpressure: descartar lo MÁS VIEJO de la cola, no lo recién llegado
            try:
                dropped = _alpaca_news_queue.get_nowait()
                _alpaca_news_queue.task_done()
                logger.warning(
                    f"[AlpacaNews] cola llena — descartada noticia vieja: "
                    f"{dropped.get('title', '')[:60]}"
                )
            except _queue.Empty:
                pass
            try:
                _alpaca_news_queue.put_nowait(article)
            except _queue.Full:
                logger.warning("[AlpacaNews] cola llena — noticia descartada")

    # Getter de watchlist viva: el stream refresca su filtro periódicamente, así los
    # tickers añadidos por el dashboard entran sin reiniciar (fallback al snapshot inicial).
    def _live_watchlist():
        return get_live_watchlist() or watchlist

    while True:
        try:
            alpaca_stream_news(_live_watchlist, on_article)
        except Exception as e:
            logger.error(f"Error en Alpaca News stream: {e}")
        time.sleep(5)


def reddit_loop(config, watchlist, dedup, **kwargs):
    interval = config.get("intervals_seconds", {}).get("reddit", 600)
    while True:
        try:
            for article in fetch_reddit_mentions(watchlist):
                process_article(article, watchlist, dedup, config, **kwargs)
        except Exception as e:
            logger.error(f"Error en Reddit poll: {e}")
        time.sleep(interval)


def price_update_loop():
    """Actualiza precio_24h_despues en alertas del día anterior."""
    while True:
        time.sleep(3600)
        try:
            updated = update_24h_prices(lambda t: get_realtime_price(t))
            if updated > 0:
                logger.info(f"Precios 24h actualizados: {updated} alertas")
        except Exception as e:
            logger.error(f"Error en price_update_loop: {e}")


def weekend_digest_loop(twilio_from: str, twilio_to: str):
    """
    Cada domingo a las 19:00 Lima envía un digest de las oportunidades
    acumuladas durante el fin de semana (mercado cerrado).
    """
    while True:
        now = datetime.now(LIMA)
        # Calcular próximo domingo 19:00 Lima
        days_until_sunday = (6 - now.weekday()) % 7
        target = now.replace(hour=19, minute=0, second=0, microsecond=0)
        if days_until_sunday > 0:
            target += timedelta(days=days_until_sunday)
        elif now >= target:
            # Ya pasó el domingo 19:00 de esta semana → siguiente domingo
            target += timedelta(days=7)

        wait_secs = (target - now).total_seconds()
        logger.info(
            f"[WeekendDigest] Próximo digest en {wait_secs/3600:.1f}h "
            f"(domingo 19:00 Lima)"
        )
        time.sleep(wait_secs)

        # 2026-06-10: la cola NO se vacía hasta confirmar el envío — antes se limpiaba
        # ANTES de mandar: si Twilio fallaba, toda la acumulación de la semana se perdía.
        with _weekend_lock:
            items = list(_weekend_queue)

        if not items:
            logger.info("[WeekendDigest] Sin oportunidades acumuladas este fin de semana.")
            continue

        # Ordenar: ALTA primero, luego MEDIA; dentro de cada grupo por pct_estimado desc
        priority_order = {"ALTA": 0, "MEDIA": 1}
        items.sort(
            key=lambda r: (
                priority_order.get(r.get("prioridad", "MEDIA"), 2),
                -(r.get("pct_estimado") or 0),
            )
        )

        top = items[:8]  # máximo 8 en el digest

        n_alta = sum(1 for r in items if r.get("prioridad") == "ALTA")
        n_media = sum(1 for r in items if r.get("prioridad") == "MEDIA")

        lines = [
            f"📋 *Digest fin de semana* — {len(items)} oportunidades",
            f"({n_alta} ALTA · {n_media} MEDIA)",
            "",
        ]
        for r in top:
            ticker = r.get("ticker", "?")
            prio = r.get("prioridad", "?")
            dire = r.get("direccion", "?")
            pct = r.get("pct_estimado")
            entrada = r.get("entrada_rango", "N/A")
            stop = r.get("stop", "N/A")
            target_p = r.get("target", "N/A")
            cataliz = r.get("resumen_cataliz", "")[:80]
            pct_str = f" ~+{pct:.0f}%" if pct else ""
            emoji = "🚨" if prio == "ALTA" else "⚠️"
            lines.append(f"{emoji} *{ticker}* [{dire}]{pct_str}")
            lines.append(f"   {cataliz}")
            lines.append(f"   Entrada {entrada} · Stop {stop} · Target {target_p}")
            lines.append("")

        lines.append("_Mercado abierto desde ahora — verificar precios antes de entrar_")
        msg_body = "\n".join(lines)

        sent = _send_twilio_raw(msg_body, twilio_to)
        if sent:
            with _weekend_lock:
                _weekend_queue.clear()
                _save_weekend_queue()   # vaciar SOLO tras envío confirmado
            logger.info(
                f"[WeekendDigest] Digest enviado: {len(top)} oportunidades "
                f"({n_alta} ALTA, {n_media} MEDIA)"
            )
        else:
            # La cola queda intacta (persistida): se incluirá en el próximo digest
            # o sobrevive un reinicio. send_raw_message ya reintentó 3 veces.
            logger.error("[WeekendDigest] Error enviando digest — cola conservada para reintento")


def _startup_ping(twilio_to: str):
    """
    Envía mensaje de arranque al iniciar el sistema.
    Sirve como verificación de canal WhatsApp y aviso de reinicio.
    Si no llega → sesión sandbox expirada → enviar 'join <palabra>' al +14155238886.
    """
    now_str = datetime.now(LIMA).strftime("%d/%m/%Y %H:%M")
    msg = (
        f"OportunityAlert iniciado — {now_str} Lima\n"
        f"Sistema activo. Si ves este mensaje, el canal WhatsApp funciona.\n"
        f"Si NO recibes alertas → reenviar 'join <palabra>' al +14155238886"
    )
    sent = _send_twilio_raw(msg, twilio_to)
    if sent:
        logger.info("[Startup] Ping de arranque enviado — canal WhatsApp activo")
    else:
        logger.error(
            "[Startup] CANAL WHATSAPP NO RESPONDE — "
            "sesion sandbox probablemente expirada. "
            "Reenviar 'join <palabra>' al +14155238886"
        )


def heartbeat_loop(twilio_from: str, twilio_to: str):
    """
    Envía SMS diario a las 8am Lima con resumen del sistema.
    Si no recibes este SMS → el sistema está caído o la sesión WhatsApp expiró.
    """
    while True:
        now_lima = datetime.now(LIMA)
        # Calcular segundos hasta las 8:00am Lima del día siguiente
        target = now_lima.replace(hour=8, minute=0, second=0, microsecond=0)
        if now_lima >= target:
            target += timedelta(days=1)
        wait_secs = (target - now_lima).total_seconds()
        time.sleep(wait_secs)

        try:
            with _stats_lock:
                analyzed = _stats["analyzed"]
                alta = _stats["alta"]
                media = _stats["media"]
                baja = _stats["baja"]
                sms_sent = _stats["sms_sent"]
                dedup_total = _stats["dedup"]
                filtered_total = _stats["filtered"]
                _stats.update({"analyzed": 0, "alta": 0, "media": 0, "baja": 0,
                               "sms_sent": 0, "dedup": 0, "filtered": 0})

            fecha = datetime.now(LIMA).strftime("%d/%m/%Y")
            # Estado del mercado y cola acumulada
            market_open = is_market_open()
            with _weekend_lock:
                queued = len(_weekend_queue)

            if market_open:
                market_line = "📈 Mercado: ABIERTO"
            else:
                market_line = f"🔒 Mercado: CERRADO — {queued} oportunidad{'es' if queued != 1 else ''} acumulada{'s' if queued != 1 else ''}"

            # Una sola llamada a eToro (antes hacía health_check + get_portfolio = 2).
            pf = get_portfolio()
            etoro_line = (
                f"eToro: OFFLINE ({str(pf['error'])[:30]}) — gates 1-3 activos"
                if pf.get("error")
                else f"eToro: OK (${pf.get('available_cash', 0):.0f} cash)"
            )

            # Watchdog (2026-06-10): denunciar loops que llevan horas sin un ciclo OK
            # — un thread COLGADO (no muerto) era invisible para el supervisor.
            stale = _stale_threads()
            stale_line = f"⚠️ Threads sin actividad: {', '.join(stale)}" if stale else ""

            msg_body = (
                f"OportunityAlert activo — {fecha}\n"
                f"Analizadas: {analyzed} | SMS enviados: {sms_sent}\n"
                f"ALTA:{alta}  MEDIA:{media}  BAJA:{baja}\n"
                f"Gates filtraron: {dedup_total} dedup + {filtered_total} sin keywords\n"
                f"{market_line}\n"
                f"{etoro_line}"
            )
            if stale_line:
                msg_body += f"\n{stale_line}"
            sent = _send_twilio_raw(msg_body, twilio_to)
            if sent:
                logger.info(f"Heartbeat enviado: {msg_body.replace(chr(10), ' | ')}")
            else:
                logger.error(
                    "[Heartbeat] FALLO DE ENTREGA — canal WhatsApp no responde. "
                    "Sesion sandbox probablemente expirada. "
                    "Reenviar 'join <palabra>' al +14155238886"
                )
        except Exception as e:
            logger.error(f"Error en heartbeat: {e}")

        # Mantenimiento diario (2026-06-10): purga de SQLite. Antes los DELETE solo
        # corrían al ARRANCAR el proceso — semanas de uptime = crecimiento sin límite
        # (position_snapshots 144 filas/día/posición, article_filter_log ~300/día).
        try:
            DedupStore("data/seen_ids.db").cleanup()
            if _metrics:
                _metrics.cleanup()
        except Exception as e:
            logger.error(f"[Heartbeat] Error en mantenimiento diario: {e}")


# ── Position Tracker ─────────────────────────────────────────────────────────


def _send_position_alert(alert: dict, twilio_from: str, twilio_to: str):
    level = alert["level"]
    ticker = alert["ticker"]
    pnl_pct = alert["pnl_pct"]
    message = alert["message"]
    action = alert["action"]

    emoji = {"URGENTE": "🔴", "ATENCION": "⚠️", "INFO": "📊"}.get(level, "📊")
    body = (
        f"{emoji} {level} — {ticker}\n"
        f"P&L: {pnl_pct:+.1f}%\n"
        f"{message}\n"
        f"→ {action}"
    )

    _send_twilio_raw(body, twilio_to)


# ── Fase 3: salida event-driven por noticia adversa ─────────────────────────────
ADVERSE_EXIT_SCORE_MIN = 7   # score_ia mínimo para una alerta de salida por noticia (alta convicción)


def _held_tickers() -> set:
    """
    Tickers en cartera REAL (eToro), leídos del account_cache que escribe el
    PositionTracker cada 10 min. Sin llamada sincrónica a eToro. Si el cache no existe
    o está viejo (>60 min ≈ tracker caído), retorna set() → la alerta de salida por
    noticia simplemente no dispara (fail-safe, nunca avisa con datos podridos).
    """
    try:
        from utils.position_strategy import read_account_cache, account_cache_age_minutes
        cache = read_account_cache()
        age = account_cache_age_minutes()
        if cache and age is not None and age < 60 and "positions" in cache:
            return {p.get("ticker", "").upper() for p in cache.get("positions", [])
                    if p.get("ticker")}
    except Exception as e:
        logger.debug(f"[AdverseNews] cache no usable ({e})")
    return set()


def _send_adverse_news_alert(ticker: str, result: dict, twilio_to: str) -> None:
    """Alerta URGENTE: noticia adversa sobre una posición abierta. Inmediata (24/5)."""
    resumen = (result.get("resumen_cataliz") or "")[:200]
    body = (
        f"🔴 URGENTE — NOTICIA ADVERSA sobre tu posición {ticker}\n"
        f"{resumen}\n"
        f"→ Considerá SALIR en eToro (podés actuar 24/5). Revisá si rompe tu tesis."
    )
    _send_twilio_raw(body, twilio_to)


MOVE_EXPLAIN_THRESHOLD = 3.0   # % de cambio en una posición que merece explicación
MOVE_EXPLAIN_COOLDOWN  = 60    # minutos entre explains del mismo ticker


def _explain_large_moves(
    current_positions: list,
    prev_snapshot: dict,
    metrics: "MetricsStore",
    twilio_to: str,
    dedup: DedupStore = None,
):
    """
    Compara posiciones actuales con el snapshot anterior.
    Si alguna se movió >3% entre polls, envía SMS explicando si hay noticia o es volatilidad.
    """
    if not prev_snapshot:
        return

    for pos in current_positions:
        ticker  = pos["ticker"]
        current = pos.get("current_rate", 0)
        prev    = prev_snapshot.get(ticker, {}).get("current_rate", 0)

        if not prev or not current:
            continue

        move_pct = (current - prev) / prev * 100

        if abs(move_pct) < MOVE_EXPLAIN_THRESHOLD:
            continue

        # Dedup: no spamear el mismo ticker en menos de 60 min.
        # 2026-06-10: reusa el DedupStore del tracker — antes instanciaba uno NUEVO
        # por posición por ciclo (con _init_db completo cada vez).
        dedup_exp = dedup or DedupStore("data/seen_ids.db")
        flag_key  = f"explain_{ticker}_{int(pos.get('open_rate', 0) * 100)}"
        if dedup_exp.has_flag(flag_key):
            continue

        # Buscar noticias recientes para explicar el move
        recent_news = metrics.get_recent_news_for_ticker(ticker, minutes=90)
        direction   = pos.get("direction", "BUY")
        against     = (move_pct < 0 and direction == "BUY") or (move_pct > 0 and direction == "SELL")

        if recent_news:
            cataliz = recent_news[0].get("resumen_cataliz", "")[:100]
            prio    = recent_news[0].get("prioridad", "?")
            news_line = f"Noticia detectada ({prio}): {cataliz}"
            advice = "Revisar si el catalizador sigue vigente." if against else "Catalizador alineado con tu posicion."
        else:
            # 2026-06-10: mirar también los titulares que el FILTRO descartó — antes
            # decía "sin catalizador" mientras "Why Is MU Falling" dormía en el filter log.
            filtered = metrics.get_recent_filtered_headlines(ticker, minutes=90)
            if filtered:
                news_line = f"Titular reciente (filtrado, sin analisis IA): {filtered[0].get('title', '')[:120]}"
                advice = "Revisar el titular — puede explicar el movimiento."
            else:
                news_line = "Sin catalizador identificado en los ultimos 90 min."
                advice    = "Volatilidad normal del mercado." if abs(move_pct) < 6 else "Movimiento fuerte sin noticia — verificar manualmente."

        direction_tag = "CONTRA tu posicion" if against else "A FAVOR de tu posicion"
        body = (
            f"[POSICION] {ticker} {direction}\n"
            f"Movimiento: {move_pct:+.1f}% en el ultimo poll ({direction_tag})\n"
            f"Precio: ${prev:.2f} → ${current:.2f}\n"
            f"P&L actual: {pos.get('net_profit_pct', 0):+.1f}%\n"
            f"\n"
            f"{news_line}\n"
            f"→ {advice}"
        )

        _send_twilio_raw(body, twilio_to)
        dedup_exp.set_flag(flag_key, ttl_hours=MOVE_EXPLAIN_COOLDOWN / 60)
        logger.info(
            f"[PositionTracker] Explain enviado: {ticker} {move_pct:+.1f}% "
            f"({'con noticia' if recent_news else 'sin noticia'})"
        )


def _sync_position_watchlist(metrics: "MetricsStore", held_tickers: set):
    """
    Asegura que las posiciones abiertas de eToro estén en el universo de noticias.
    Añade los tickers que falten (category='position') y retira los auto-añadidos
    que ya no se tienen. Las entradas manuales del usuario nunca se tocan.
    """
    try:
        wl = {r["ticker"]: r.get("category") for r in metrics.get_watchlist()}
        for tk in held_tickers:
            if tk not in wl:
                metrics.add_ticker(tk, category="position", notes="auto: posición eToro")
                logger.info(f"[PositionTracker] {tk} añadido al universo de noticias (posición)")
        for tk, cat in wl.items():
            if cat == "position" and tk not in held_tickers:
                metrics.remove_ticker(tk)
                logger.info(f"[PositionTracker] {tk} retirado del universo (posición cerrada)")
    except Exception as e:
        logger.debug(f"[PositionTracker] sync watchlist: {e}")


def position_tracker_loop(twilio_from: str, twilio_to: str):
    """
    Thread cada 10 min. ADVISOR de salida por estrategia (Marea/PED) sobre las
    posiciones reales de eToro. NO ejecuta órdenes — solo aconseja por SMS/dashboard.
    """
    POLL_INTERVAL = 600   # 10 minutos

    dedup   = DedupStore("data/seen_ids.db")
    metrics = MetricsStore()
    empty_streak = 0   # ciclos consecutivos con positions=[] (guard anti-cierres fantasma)

    while True:
        try:
            etoro_data = get_portfolio()

            if etoro_data.get("error"):
                logger.debug(f"[PositionTracker] eToro no disponible: {etoro_data['error']}")
                _beat("PositionTracker", POLL_INTERVAL)   # el loop está vivo; es eToro el caído
                time.sleep(POLL_INTERVAL)
                continue

            positions = etoro_data.get("positions", [])

            # GUARD anti-cierres fantasma (2026-06-10): un 200 de eToro con positions=[]
            # transitorio (glitch parcial de la API) registraba TODAS las posiciones como
            # trades cerrados y vaciaba el watchlist de posiciones. Exigir 2 ciclos
            # consecutivos vacíos antes de creer que el portafolio realmente quedó flat.
            prev_tickers = metrics.get_open_tickers_last_snapshot()
            if not positions and prev_tickers:
                empty_streak += 1
                if empty_streak < 2:
                    logger.warning(
                        "[PositionTracker] eToro devolvió 0 posiciones (había "
                        f"{len(prev_tickers)}) — esperando confirmación en el próximo ciclo"
                    )
                    _beat("PositionTracker", POLL_INTERVAL)
                    time.sleep(POLL_INTERVAL)
                    continue
            else:
                empty_streak = 0

            # Auto-incluir posiciones abiertas en el universo de noticias (y retirar cerradas)
            _sync_position_watchlist(metrics, {p["ticker"] for p in positions})

            # Snapshot ANTERIOR (capturado antes de escribir el actual) — se usa tanto
            # para detectar cierres como para explicar movimientos bruscos.
            if prev_tickers and positions is not None:
                current_keys = {p["ticker"] for p in positions}
                # Evaluaciones del ciclo anterior (account_cache): traen estrategia y la
                # última razón de salida conocida → la tabla trades queda auditable por
                # estrategia ("¿Marea/PED/manual gana dinero?").
                try:
                    from utils.position_strategy import read_account_cache
                    last_evals = read_account_cache().get("evals", {}) or {}
                except Exception:
                    last_evals = {}
                for ticker_closed, snap in prev_tickers.items():
                    if ticker_closed not in current_keys:
                        # Posición desapareció → cerrada. close_rate via get_realtime_price,
                        # que ahora es eToro-primaria → precio del MISMO broker donde cerró
                        # la posición (P&L histórico fiel; #4 resuelto). Fallback: último
                        # current_rate del snapshot eToro previo.
                        try:
                            from utils.alpaca_price import get_realtime_price
                            pd = get_realtime_price(ticker_closed)
                            close_rate = pd.get("current_price") or snap.get("current_rate", 0)
                        except Exception:
                            close_rate = snap.get("current_rate", 0)
                        from datetime import datetime as _dt
                        close_ts = _dt.now(LIMA).strftime("%Y-%m-%dT%H:%M:%S")
                        ev_prev = last_evals.get(ticker_closed, {})
                        strategy = ev_prev.get("strategy", "")
                        if not strategy:
                            try:
                                from utils.position_strategy import resolve_strategy
                                strategy = resolve_strategy(ticker_closed)["strategy"]
                            except Exception:
                                strategy = ""
                        # reason del último ciclo: "chandelier"/"ped_time" = salida con señal;
                        # "chandelier_ok"/"ped_hold" = Oscar cerró SIN señal (discrecional).
                        exit_reason = ev_prev.get("reason", "") or "sin_eval"
                        metrics.record_closed_trade(
                            ticker_closed, snap, close_rate, close_ts,
                            strategy=strategy, exit_reason=exit_reason,
                        )
                        metrics.refresh_daily_summary()

            # Guardar snapshot actual (aunque sea lista vacía, marca el estado)
            if positions:
                metrics.log_position_snapshot(positions)

            from utils.position_strategy import evaluate_position, write_account_cache
            from utils.equity_history import record_equity_snapshot

            cash  = etoro_data.get("available_cash", 0)
            total = etoro_data.get("total_value", 0)

            # Historial diario de equity REAL (idempotente por día; reusa los valores que
            # el tracker ya tiene → cero llamadas extra a eToro). Cubre flat y con posiciones.
            record_equity_snapshot(total, cash)

            if not positions:
                logger.debug("[PositionTracker] Sin posiciones abiertas")
                # positions=[] explícito → el Gate 5 de noticias sabe que el portafolio
                # está flat sin tener que llamar a eToro en vivo.
                write_account_cache(cash, total, {}, positions=[])
                _beat("PositionTracker", POLL_INTERVAL)
                time.sleep(POLL_INTERVAL)
                continue

            alerts_to_send = []
            evals = {}   # ticker -> evaluación de salida (la consume el dashboard via cache)

            # Detectar movimientos bruscos y explicarlos. Usa prev_tickers (snapshot
            # ANTERIOR, capturado antes de escribir el actual) — antes leía el snapshot
            # recién escrito y comparaba current-vs-current (siempre 0 → nunca disparaba).
            _explain_large_moves(positions, prev_tickers, metrics, twilio_to, dedup=dedup)

            # Salida POR ESTRATEGIA (Marea=chandelier, PED=tiempo, manual=solo contexto).
            # Reemplaza las heurísticas viejas (T1+8%/breakeven/retroceso) que cortaban
            # ganadores de trend-following. La decisión es mecánica y validada por backtest.
            for pos in positions:
                ticker  = pos["ticker"]
                pnl_pct = pos["net_profit_pct"]
                current = pos["current_rate"]

                try:
                    ev = evaluate_position(ticker, pos.get("open_datetime", ""), current)
                except Exception as e:
                    logger.warning(f"[PositionTracker] evaluate {ticker}: {e}")
                    continue

                evals[ticker] = ev   # para el dashboard (estado de salida en vivo)

                # AVISO INTRADÍA informativo (2026-06-10): el precio eToro EN VIVO ya
                # perforó el chandelier aunque el cierre previo siga arriba. La regla
                # validada confirma al CIERRE — esto solo informa (la vigilancia intradía
                # como regla de ejecución se midió y rechazó). Sin esto, el dashboard
                # decía "dejar correr" todo el día mientras la posición se hundía.
                if ev.get("intraday_breach") and not ev["exit"]:
                    day = datetime.now(LIMA).strftime("%Y%m%d")
                    flag_key = f"intraday_stop_{ticker}_{day}"
                    if not dedup.has_flag(flag_key):
                        alerts_to_send.append({
                            "level": "ATENCION",
                            "ticker": ticker,
                            "pnl_pct": pnl_pct,
                            "message": (
                                f"[{ev['strategy'].upper()}] Precio INTRADÍA ${current:.2f} "
                                f"cruzó el chandelier ${ev['stop_price']:.2f}. "
                                f"La regla confirma al CIERRE."
                            ),
                            "action": (
                                "Vigilar de cerca. Si cierra abajo, mañana llega la señal "
                                "de VENDER — eToro 24/5 te deja salir YA si decidís no esperar."
                            ),
                        })
                        dedup.set_flag(flag_key, ttl_hours=20)

                if not ev["exit"]:
                    continue  # manual / mantener / sin datos → no dispara SMS de salida

                # Una alerta de salida por ticker+día (re-avisa al día siguiente si no actúa).
                day = datetime.now(LIMA).strftime("%Y%m%d")
                flag_key = f"exit_{ev['reason']}_{ticker}_{day}"
                if dedup.has_flag(flag_key):
                    continue

                if ev["strategy"] == "marea":
                    level, action = "URGENTE", "VENDER al próximo open (eToro). Chandelier roto."
                else:  # ped
                    level, action = "INFO", "Cerrar al próximo open (eToro). Hold PED cumplido."
                # Viernes/sábado: el "próximo open" operable es el DOMINGO ~19:00 Lima
                # (eToro 24/5) — sin este recordatorio la salida esperaba hasta el lunes.
                if datetime.now(LIMA).weekday() >= 4:
                    action += " Ojo: eToro reabre el DOMINGO ~19:00 Lima — no esperes al lunes."

                alerts_to_send.append({
                    "level": level,
                    "ticker": ticker,
                    "pnl_pct": pnl_pct,
                    "message": f"[{ev['strategy'].upper()}] {ev['detail']}",
                    "action": action,
                })
                dedup.set_flag(flag_key, ttl_hours=20)

            for alert in alerts_to_send:
                _send_position_alert(alert, twilio_from, twilio_to)
                logger.info(
                    f"[PositionTracker] {alert['level']} — {alert['ticker']}: "
                    f"{alert['message']}"
                )

            # Cache para el dashboard/API (cash + valor + estado de salida por posición)
            # + posiciones para el Gate 5 de noticias (evita llamadas eToro sincrónicas).
            write_account_cache(cash, total, evals, positions=positions)

            logger.debug(
                f"[PositionTracker] Ciclo OK — "
                f"{len(positions)} posiciones, {len(alerts_to_send)} alertas enviadas"
            )

            _beat("PositionTracker", POLL_INTERVAL)
        except Exception as e:
            logger.error(f"[PositionTracker] Error en ciclo: {e}")

        time.sleep(POLL_INTERVAL)


# (Pre-market Scanner ARCHIVADO 2026-05-30 -> _deprecated/ — sin edge validado)


# ── Gap Scanner (Hueco 1): señal por PRECIO, no por noticia ────────────────────
# El sistema es news-driven: un catalizador nocturno/Asia (caso MRVL 2-jun, endorsement
# de Jensen Huang en Computex ~10pm Lima) llega a los feeds US HORAS después, ya priceado,
# y no dispara nada a tiempo. El gap scanner cubre ese hueco: barre la watchlist en la
# ventana pre-market→sesión y avisa de movimientos grandes AUNQUE no haya titular fresco.
GAP_SCAN_DEFAULT_SECONDS = 900     # 15 min — barre la watchlist
GAP_MIN_DEFAULT_PCT      = 12      # piso de gap; por ticker usa el umbral de "priceado"


def _is_gap_scan_window() -> bool:
    """Escanea siempre que eToro permita operar acciones US (sesión 24/5: pre-market,
    regular, after-hours y la reapertura del domingo). Así cubre catalizadores de horario
    ASIA/nocturnos (caso MRVL): la acción US ya se mueve en el quote 24/5 de eToro aunque
    el titular llegue tarde, y el SMS queda esperando cuando Oscar despierta. El dedup
    1/ticker/día + la cadencia de 15 min mantienen acotado el costo de API."""
    return is_market_open()


GAP_POSITION_DEFAULT_PCT = 4.0   # caída intradía de una POSICIÓN que merece SMS


def _check_gap(ticker: str, gap_min: float, dedup, twilio_to: str,
               held: set = None, gap_pos_min: float = GAP_POSITION_DEFAULT_PCT) -> bool:
    """Evalúa un ticker; si su |change_pct| supera el umbral de priceado y no hubo ya
    una alerta hoy (ni de noticia ni de gap), manda UN SMS de gap. Dedup 1/ticker/día.

    2026-06-10: para tickers EN CARTERA el umbral de caída es gap_pos_min (default −4%):
    el umbral de oportunidad (12-30%) era para descubrir catalizadores, no para avisarte
    que TU posición se desangra. Además el mute ahora exige SMS REAL enviado hoy — antes
    cualquier análisis IA (aunque alcista y sin SMS) silenciaba 12h la alarma del crash."""
    from utils.conviction_gates import PRICED_THRESHOLDS, DEFAULT_THRESHOLD
    day = datetime.now(LIMA).strftime("%Y%m%d")
    is_held = bool(held) and ticker.upper() in held
    gap_flag = f"gap_{ticker}_{day}"
    if dedup.has_flag(gap_flag):
        return False
    # Mute solo si hoy YA salió un SMS de noticia para este ticker. Las posiciones no
    # se mutean por noticias de oportunidad (un PT raised a la mañana no debe apagar
    # la alarma de caída de la tarde).
    if not is_held:
        if dedup.has_flag(f"priced_{ticker}_{day}") or dedup.has_flag(f"smssent_{ticker}_{day}"):
            return False

    price_data = get_realtime_price(ticker)
    change = price_data.get("change_pct")
    if change is None:
        return False

    # Umbral por ticker = el de "ya priceado" en Gate1 (descubrimiento de oportunidad).
    threshold = max(PRICED_THRESHOLDS.get(ticker, DEFAULT_THRESHOLD), gap_min)
    held_drop = is_held and change <= -abs(gap_pos_min)   # caída de una posición
    if not held_drop and abs(change) < threshold:
        return False

    price = price_data.get("current_price")
    price_txt = f" (${price:.2f})" if price else ""
    arrow = "📈" if change > 0 else "📉"
    leader_tag = marea_leader_tag(ticker)
    lead_line = f"{leader_tag}\n" if leader_tag else ""
    if held_drop and abs(change) < threshold:
        body = (
            f"📉 TU POSICIÓN {ticker} cae {change:+.1f}% hoy{price_txt}\n"
            f"{lead_line}"
            f"Detectado por PRECIO (sin titular fresco en los feeds).\n"
            f"→ Revisá el contexto: ¿es sistémico (todo el sector cae) o específico? "
            f"La salida Marea decide al CIERRE vs chandelier — no vendas en pánico, "
            f"pero mirá la card de la posición en el dashboard."
        )
    else:
        body = (
            f"{arrow} GAP fuerte SIN alerta de noticia — {ticker} {change:+.0f}% hoy{price_txt}\n"
            f"{lead_line}"
            f"Movimiento grande detectado por PRECIO (posible catalizador nocturno/Asia que "
            f"los feeds US reportan tarde).\n"
            f"→ Revisá el catalizador. Si hay tendencia, evaluá Marea/PED — NO persigas el pop."
        )
    if _send_twilio_raw(body, twilio_to):
        dedup.set_flag(gap_flag, ttl_hours=20)
        with _stats_lock:
            _stats["sms_sent"] += 1
        logger.info(f"[GapScanner] {ticker} {change:+.1f}% — SMS de gap enviado "
                    f"({'posición' if held_drop else 'sin noticia'})")
        # Auditoría (2026-06-10): los SMS de gap eran invisibles para metrics.db
        if _metrics:
            _metrics.log_article_filter({
                "ticker": ticker, "source": "GAP_SCANNER",
                "title": f"Gap {change:+.1f}% detectado por precio",
                "stage": "gap_position_sms" if held_drop else "gap_sms",
                "reason": body[:200], "sms_sent": True,
            })
        return True
    return False


def truth_social_loop(config, watchlist, dedup, **kwargs):
    """Fuente Truth Social (mirror RSS trumpstruth.org): Trump postea ahí PRIMERO (la frescura
    es el lever — estudio 2026-06-20). Cada post market-relevant va al radar Trump
    (capture_headline) y al pipeline normal (process_article filtra/dedup por su cuenta:
    si nombra un ticker de watchlist pasa, si es macro se descarta barato)."""
    from sources.truth_social import fetch_recent
    interval = config.get("intervals_seconds", {}).get("truth_social", 120)
    while True:
        try:
            live_wl = get_live_watchlist() or watchlist
            posts = fetch_recent(max_items=40, max_age_min=interval / 60 + 30)
            from utils import trump_tracker
            n = 0
            for art in posts:
                trump_tracker.capture_headline(art, watchlist=live_wl, held=_held_tickers())
                process_article(art, live_wl, dedup, config, **kwargs)
                n += 1
            if n:
                logger.info(f"TruthSocial ciclo: {n} posts relevantes procesados")
            _beat("TruthSocial", interval)
        except Exception as e:
            logger.error(f"Error en truth_social_loop: {e}")
        time.sleep(interval)


def fed_loop(config, watchlist, dedup, **kwargs):
    """Fuente Fed (RSS oficial federalreserve.gov): comunicados de la Reserva Federal como
    MARKET MOVER oficial (FOMC = mayor impacto macro). Van al radar de market movers
    (capture_headline; bypass del gate Trump por ser fuente oficial). Volumen bajo → poll lento."""
    from sources.fed import fetch_recent
    from utils import trump_tracker
    interval = config.get("intervals_seconds", {}).get("fed", 1800)   # 30 min
    while True:
        try:
            live_wl = get_live_watchlist() or watchlist
            posts = fetch_recent(max_age_min=interval / 60 + 60)
            n = 0
            for art in posts:
                if trump_tracker.capture_headline(art, watchlist=live_wl, held=_held_tickers()):
                    n += 1
            if n:
                logger.info(f"Fed ciclo: {n} comunicados nuevos al radar de market movers")
            _beat("Fed", interval)
        except Exception as e:
            logger.error(f"Error en fed_loop: {e}")
        time.sleep(interval)


def official_movers_loop(config, watchlist, dedup, **kwargs):
    """Fuentes OFICIALES de market movers vía RSS (SEC + Treasury cuando aplique). Mismo patrón
    que Fed: cada comunicado → radar de market movers (bypass del gate Trump por ser fuente
    oficial). Volumen bajo → poll lento. Fed tiene su propio loop por ser el de mayor impacto."""
    from utils import trump_tracker
    interval = config.get("intervals_seconds", {}).get("official_movers", 1800)
    while True:
        try:
            live_wl = get_live_watchlist() or watchlist
            posts = []
            try:
                from sources.sec import fetch_recent as _sec
                posts += _sec(max_age_min=interval / 60 + 60)
            except Exception as e:
                logger.debug(f"[OfficialMovers] SEC: {e}")
            try:
                from sources.treasury import fetch_recent as _tre
                posts += _tre(max_age_min=interval / 60 + 60)
            except Exception as e:
                logger.debug(f"[OfficialMovers] Treasury: {e}")
            n = 0
            for art in posts:
                if trump_tracker.capture_headline(art, watchlist=live_wl, held=_held_tickers()):
                    n += 1
            if n:
                logger.info(f"OfficialMovers ciclo: {n} comunicados (SEC/Treasury) al radar")
            _beat("OfficialMovers", interval)
        except Exception as e:
            logger.error(f"Error en official_movers_loop: {e}")
        time.sleep(interval)


def scoreboard_resolver_loop(config, watchlist, dedup, **kwargs):
    """Resuelve las señales abiertas del scoreboard cada hora: las mide a 48h con la maquinaria
    de reacción (anormal vs QQQ + MFE/MAE, precio dual Alpaca/Binance perp). Idempotente."""
    interval = config.get("intervals_seconds", {}).get("scoreboard", 3600)
    while True:
        try:
            n = scoreboard.resolve_open(_METRICS_DB)
            if n:
                logger.info(f"[Scoreboard] {n} señales resueltas/actualizadas")
            _beat("Scoreboard", interval)
        except Exception as e:
            logger.error(f"Error en scoreboard_resolver_loop: {e}")
        time.sleep(interval)


def intraday_paper_loop(config, watchlist, dedup, **kwargs):
    """Paper-forward del edge overnight de semis (Binance perps). Abre/actualiza/cierra los
    registros de la canasta cada ~2 min para verlos en vivo (ABIERTO/CERRADO) en el dashboard.
    No opera: solo registra precio, %max/%min, contexto y % final. Ver utils/intraday_paper.py."""
    from utils import intraday_paper
    interval = config.get("intervals_seconds", {}).get("intraday_paper", 120)
    while True:
        try:
            intraday_paper.tick()
            _beat("IntradayPaper", interval)
        except Exception as e:
            logger.error(f"Error en intraday_paper_loop: {e}")
        time.sleep(interval)


def gap_scanner_loop(config, watchlist, dedup, **kwargs):
    """Barre la watchlist cada N seg en la ventana pre-market→sesión buscando gaps grandes
    sin noticia. Cubre catalizadores nocturnos/internacionales (Hueco 1, caso MRVL)."""
    interval = config.get("gap_scan_seconds", GAP_SCAN_DEFAULT_SECONDS)
    gap_min  = config.get("gap_alert_threshold_pct", GAP_MIN_DEFAULT_PCT)
    gap_pos  = config.get("gap_position_threshold_pct", GAP_POSITION_DEFAULT_PCT)
    twilio_to = kwargs.get("twilio_to")
    while True:
        try:
            if _is_gap_scan_window() and twilio_to:
                live_wl = get_live_watchlist() or watchlist
                held = _held_tickers()
                n_alerts = 0
                for ticker in live_wl:
                    try:
                        if _check_gap(ticker, gap_min, dedup, twilio_to,
                                      held=held, gap_pos_min=gap_pos):
                            n_alerts += 1
                    except Exception as e:
                        logger.debug(f"[GapScanner] {ticker}: {e}")
                if n_alerts:
                    logger.info(f"[GapScanner] ciclo: {n_alerts} gap(s) alertado(s) sobre {len(live_wl)} tickers")
            _beat("GapScanner", interval)
        except Exception as e:
            logger.error(f"[GapScanner] Error en ciclo: {e}")
        time.sleep(interval)


# ── Macro Sentinel (2026-06-10): "super alerta" de caída de mercado ───────────────
# El crash de jun-2026 pasó sin UN solo SMS: el pipeline entero era per-ticker y
# alcista. Este loop mira el MERCADO por PRECIO (QQQ/SPY intradía, velocidad 5d,
# breadth del universo Marea) y avisa 1 vez/día cuando hay señal de riesgo sistémico.
# También avisa cuando el RÉGIMEN macro de Marea (SMA200 ±3%) cambia de estado —
# antes el flip solo se veía leyendo el mensaje diario de las 17:00.
MACRO_SENTINEL_DEFAULT_SECONDS = 900   # 15 min, como el gap scanner


def _held_with_stops() -> list:
    """Posiciones reales + su chandelier (del account_cache) + precio en vivo.
    Para que la alerta macro diga QUÉ posiciones están cerca de su stop."""
    out = []
    try:
        from utils.position_strategy import read_account_cache
        cache = read_account_cache()
        evals = cache.get("evals", {}) or {}
        for p in cache.get("positions", []) or []:
            tk = p.get("ticker")
            if not tk:
                continue
            stop = (evals.get(tk) or {}).get("stop_price")
            if not stop:
                continue
            pd = get_realtime_price(tk)
            cur = pd.get("current_price")
            if cur:
                out.append({"ticker": tk, "current": cur, "stop": stop})
    except Exception as e:
        logger.debug(f"[Sentinel] held stops: {e}")
    return out


def market_sentinel_loop(config, twilio_to: str):
    """Cada 15 min: (1) ¿cambió el régimen macro? → SMS; (2) ¿señales de caída
    de mercado? → 1 SMS de riesgo por día. Solo informa — no cambia reglas."""
    from utils.market_sentinel import check_market_risk, regime_change

    interval   = config.get("macro_sentinel_seconds", MACRO_SENTINEL_DEFAULT_SECONDS)
    thresholds = config.get("macro_sentinel", {})
    dedup = DedupStore("data/seen_ids.db")

    while True:
        try:
            if is_market_open() and twilio_to:
                # 1) Cambio de régimen (barato: barras QQQ cacheadas 1/día)
                chg = regime_change()
                if chg:
                    _, cur = chg
                    if cur:
                        body = (
                            "🟢 CAMBIO DE RÉGIMEN MACRO — vuelve ALCISTA\n"
                            "QQQ recuperó la SMA200 +3% (histéresis). "
                            "Marea reabre compras nuevas."
                        )
                    else:
                        body = (
                            "🔴 CAMBIO DE RÉGIMEN MACRO — risk-OFF\n"
                            "QQQ cerró bajo la SMA200 −3% (histéresis). Marea NO abre "
                            "compras nuevas. Las posiciones abiertas siguen con su "
                            "chandelier individual (no vender en pánico)."
                        )
                    if _send_twilio_raw(body, twilio_to):
                        with _stats_lock:
                            _stats["sms_sent"] += 1

                # 2) Riesgo de mercado — 1 SMS/día máximo
                day = datetime.now(LIMA).strftime("%Y%m%d")
                if not dedup.has_flag(f"macro_alert_{day}"):
                    risk = check_market_risk(_held_with_stops(), thresholds)
                    if risk:
                        lines = [
                            "🚨 ALERTA MACRO — riesgo de caída de mercado",
                            "Señales: " + " · ".join(risk["triggers"]),
                        ]
                        if risk["at_risk"]:
                            lines.append("Posiciones cerca del stop: " + ", ".join(risk["at_risk"]))
                        lines.append(
                            "→ Riesgo SISTÉMICO (no específico de tus empresas). "
                            "No abrir compras nuevas hoy. La regla Marea decide al "
                            "CIERRE vs chandelier, pero eToro 24/5 te deja reducir "
                            "YA si lo ves necesario."
                        )
                        if _send_twilio_raw("\n".join(lines), twilio_to):
                            dedup.set_flag(f"macro_alert_{day}", ttl_hours=20)
                            with _stats_lock:
                                _stats["sms_sent"] += 1
                            logger.info(f"[Sentinel] ALERTA MACRO enviada: {risk['triggers']}")
                            if _metrics:
                                _metrics.log_article_filter({
                                    "ticker": "MARKET", "source": "MACRO_SENTINEL",
                                    "title": " · ".join(risk["triggers"])[:200],
                                    "stage": "macro_alert_sms",
                                    "reason": ", ".join(risk["at_risk"])[:200] or "sin posiciones en riesgo",
                                    "sms_sent": True,
                                })
            _beat("MacroSentinel", interval)
        except Exception as e:
            logger.error(f"[Sentinel] Error en ciclo: {e}")
        time.sleep(interval)


# ── Brief Diario en background ────────────────────────────────────────────────
# La narrativa del Brief la genera SOLO el background — nunca una visita a la página
# (eso disparaba 1 llamada IA por apertura). Dos disparadores: (1) cada mañana
# pre-market; (2) cuando salta una noticia ALTA (algo que vale la pena mostrar),
# con throttle para no re-llamar a la IA en cada alerta de un día movido.
BRIEF_AM_HOUR = 6
BRIEF_AM_MINUTE = 30
BRIEF_REGEN_MIN_INTERVAL = 1200   # 20 min mínimo entre regeneraciones por evento

_brief_regen_last = 0.0
_brief_regen_lock = threading.Lock()


def _trigger_brief_regen(reason: str):
    """Regenera el Brief en un thread aparte (throttled). No bloquea el pipeline de noticias."""
    global _brief_regen_last
    with _brief_regen_lock:
        if time.time() - _brief_regen_last < BRIEF_REGEN_MIN_INTERVAL:
            return
        _brief_regen_last = time.time()

    def _run():
        try:
            from utils.daily_brief import regenerate
            regenerate(reason=reason)
        except Exception as e:
            logger.warning(f"[DailyBrief] regen background falló ({reason}): {e}")

    threading.Thread(target=_run, name="BriefRegen", daemon=True).start()


def daily_brief_loop():
    """
    Genera el Brief Diario en background: 1 vez al arrancar (si no hay narrativa de hoy)
    y cada mañana ~06:30 Lima (pre-market). Las visitas a la página solo LEEN este cache.
    """
    from utils.daily_brief import regenerate, has_narrative_today
    try:
        if not has_narrative_today():
            regenerate(reason="arranque del sistema")
    except Exception as e:
        logger.warning(f"[DailyBrief] generación de arranque falló: {e}")
    _beat("DailyBrief", 24 * 3600)

    while True:
        now = datetime.now(LIMA)
        target = now.replace(hour=BRIEF_AM_HOUR, minute=BRIEF_AM_MINUTE, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        logger.info(f"[DailyBrief] próxima generación AM en {wait_secs/3600:.1f}h (06:30 Lima)")
        time.sleep(wait_secs)
        try:
            regenerate(reason="pre-market AM")
        except Exception as e:
            logger.error(f"[DailyBrief] generación AM falló: {e}")
        _beat("DailyBrief", 24 * 3600)


# ── Comando status ────────────────────────────────────────────────────────────

def show_status():
    """Muestra resumen rápido sin arrancar el monitor."""
    from alerts.alert_logger import get_recent_alerts

    alerts = get_recent_alerts(hours=24)
    n_alta = sum(1 for a in alerts if a.get("prioridad") == "ALTA")
    n_media = sum(1 for a in alerts if a.get("prioridad") == "MEDIA")
    n_baja = sum(1 for a in alerts if a.get("prioridad") == "BAJA")
    n_sms = sum(1 for a in alerts if a.get("sms_enviado"))

    # Leer última línea del log para saber si el proceso está vivo
    last_log = ""
    try:
        with open("opportunity_alert.log", "r", encoding="utf-8") as f:
            lines = f.readlines()
            last_log = lines[-1].strip() if lines else ""
    except Exception:
        pass

    now_lima = datetime.now(LIMA).strftime("%d/%m/%Y %H:%M Lima")
    sep = "=" * 56

    print(f"\n{sep}")
    print(f"  OPPORTUNITY ALERT — Status  {now_lima}")
    print(sep)
    print(f"  Alertas ultimas 24h: {len(alerts)}")
    print(f"    ALTA:  {n_alta}  |  MEDIA: {n_media}  |  BAJA: {n_baja}")
    print(f"    SMS enviados: {n_sms}")
    print(f"\n  Ultimo log:")
    print(f"    {last_log[:80]}")
    print(f"\n  Ver historial completo: python show_history.py")
    print(f"  Ver/editar watchlist:   dashboard web (tab Watchlist) — fuente: metrics.db")
    print(f"{sep}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
        return

    _setup_logging()
    logger.info("=" * 56)
    logger.info("OportunityAlert v2.0 — Iniciando")
    logger.info("=" * 56)

    config = load_config("config.json")
    watchlist = get_watchlist(config)
    logger.info(f"Watchlist: {len(watchlist)} tickers")

    api_keys = config.get("api_keys", {})
    ai_engine = os.environ.get("AI_ENGINE", "deepseek").lower()
    if ai_engine == "deepseek":
        get_env_or_die("DEEPSEEK_API_KEY")
    elif ai_engine == "glm":
        get_env_or_die("GLM_API_KEY")
    elif ai_engine == "claude":
        get_env_or_die(api_keys.get("anthropic_key_env", "ANTHROPIC_API_KEY"))
    get_env_or_die(api_keys.get("finnhub_key_env", "FINNHUB_API_KEY"))

    if not os.environ.get(api_keys.get("alpaca_key_env", "ALPACA_API_KEY")):
        logger.warning("ALPACA_API_KEY no configurada — sin precios real-time")
    if not os.environ.get(api_keys.get("twilio_sid_env", "TWILIO_ACCOUNT_SID")):
        logger.warning("TWILIO no configurado — sin SMS")
    if not os.environ.get("DASHBOARD_PASS"):
        logger.warning(
            "DASHBOARD_PASS vacia — el dashboard queda ABIERTO en 0.0.0.0:8081 "
            "(portfolio real + endpoints de escritura expuestos si el firewall de "
            "Oracle no bloquea el puerto). Configurar DASHBOARD_PASS en .env."
        )

    twilio_from = api_keys.get("twilio_from", "")
    twilio_to = api_keys.get("twilio_to", "")
    send_priorities = config.get("send_sms_priorities", ["ALTA", "MEDIA"])
    log_priorities = config.get("log_only_priorities", ["BAJA"])

    dedup = DedupStore("data/seen_ids.db")
    logger.info(f"DedupStore: {dedup.count()} entradas previas")

    global _metrics
    _metrics = MetricsStore()
    scoreboard.ensure_table(_METRICS_DB)   # libro mayor de outcomes (los 3 brazos)
    logger.info("MetricsStore inicializado")

    # Seed watchlist desde config si la tabla está vacía (primera vez)
    seeded = _metrics.init_watchlist_from_list(watchlist)
    if seeded:
        logger.info(f"Watchlist inicial cargada en DB: {seeded} tickers")

    start_worker()
    logger.info("DelayedAlerts worker iniciado")

    from utils import trump_tracker
    trump_tracker.start_worker()   # worker de resúmenes IA del radar Trump

    _load_weekend_queue()   # recargar oportunidades acumuladas si hubo reinicio

    # Pre-warm del mapa símbolo→instrumentId de eToro en un thread aparte: la 1ª llamada
    # baja ~15k instrumentos (~4.5s). Sin esto, esa demora caería sobre la primera noticia
    # que llegue. En background no retrasa el arranque ni bloquea nada.
    def _prewarm_etoro_map():
        try:
            from utils.etoro_market import get_instrument_id
            if get_instrument_id("NVDA"):
                logger.info("[Startup] Mapa de instrumentos eToro pre-cargado")
        except Exception as e:
            logger.debug(f"[Startup] pre-warm eToro falló (no crítico): {e}")
    threading.Thread(target=_prewarm_etoro_map, name="PrewarmEtoro", daemon=True).start()

    market_status = "ABIERTO" if is_market_open() else "CERRADO (modo acumulación activo)"
    logger.info(f"Estado mercado al inicio: {market_status}")

    # Ping de arranque: verifica que el canal WhatsApp está activo.
    # Si no recibes este mensaje → sesión sandbox expirada → envía "join <palabra>" al +14155238886
    _startup_ping(twilio_to)

    shared = dict(
        twilio_from=twilio_from,
        twilio_to=twilio_to,
        send_priorities=send_priorities,
        log_priorities=log_priorities,
    )

    # Lanzar todos los threads con supervisor de auto-reinicio
    # Reddit desactivado: IPs de Oracle Cloud son bloqueadas por Reddit (403).
    # Para reactivar: descomentar la línea de Reddit en threads_cfg.
    threads_cfg = [
        ("EDGAR",         edgar_loop,           (config, watchlist, dedup), shared),
        ("AlpacaNews",    alpaca_news_loop,     (config, watchlist, dedup), shared),
        ("Finnhub",       finnhub_loop,         (config, watchlist, dedup), shared),
        # ("Reddit",      reddit_loop,          (config, watchlist, dedup), shared),
        ("Price24h",      price_update_loop,    (),                         {}),
        ("Heartbeat",     heartbeat_loop,       (twilio_from, twilio_to),   {}),
        ("WeekendDigest",    weekend_digest_loop,      (twilio_from, twilio_to),   {}),
        ("PositionTracker",  position_tracker_loop,    (twilio_from, twilio_to),   {}),
        ("GapScanner",       gap_scanner_loop,         (config, watchlist, dedup), shared),
        ("TruthSocial",      truth_social_loop,        (config, watchlist, dedup), shared),
        ("Fed",              fed_loop,                 (config, watchlist, dedup), shared),
        ("OfficialMovers",   official_movers_loop,     (config, watchlist, dedup), shared),
        ("Scoreboard",       scoreboard_resolver_loop, (config, watchlist, dedup), shared),
        ("IntradayPaper",    intraday_paper_loop,      (config, watchlist, dedup), shared),
        ("MacroSentinel",    market_sentinel_loop,     (config, twilio_to),        {}),
        ("DailyBrief",       daily_brief_loop,         (),                            {}),
        ("Dashboard",        run_dashboard,            (),                            {}),
    ]
    logger.info("Fuentes activas: EDGAR, AlpacaNews (Benzinga/WS), Finnhub, PositionTracker, GapScanner, MacroSentinel (Reddit desactivado; PreMarket archivado)")

    for name, fn, args, kwargs in threads_cfg:
        t = threading.Thread(
            target=_supervised_thread,
            args=(name, fn, *args),
            kwargs=kwargs,
            daemon=True,
            name=name,
        )
        t.start()
        logger.info(f"Thread supervisado iniciado: {name}")

    _stop_event = threading.Event()

    def _handle_stop(signum, frame):
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        logger.info(f"OportunityAlert detenido ({sig_name}).")
        _stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    logger.info("Sistema activo 24/7. Ctrl+C para detener.")
    _stop_event.wait()
    sys.exit(0)


if __name__ == "__main__":
    main()
