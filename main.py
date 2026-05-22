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
from filters.keyword_filter import passes_filter
from filters.claude_scorer import score_with_claude
from alerts.twilio_sms import send_sms, format_sms
from alerts.alert_logger import log_alert, log_claude_analysis, update_24h_prices, get_recent_alerts
from utils.alpaca_price import get_realtime_price
from utils.dedup_store import DedupStore

LIMA = timezone(timedelta(hours=-5))

# Umbrales de "ya priceado" por sector (% de movimiento desde cierre)
# Sectores más volátiles toleran mayor movimiento antes de descartar
PRICED_THRESHOLDS = {
    "QBTS": 30, "IONQ": 30, "RGTI": 30, "QUBT": 30,
    "GME": 30, "AMC": 30,
    "MARA": 25, "RIOT": 25, "MSTR": 25,
    "SOUN": 25, "BBAI": 25, "INOD": 25, "APLD": 25, "IREN": 25,
    "RKLB": 20, "LUNR": 20, "JOBY": 20, "ACHR": 20,
    "NVDA": 15, "TSLA": 15, "SMCI": 15, "RIVN": 15, "LCID": 15,
    "WOLF": 15, "PLTR": 15, "APP": 15,
}

# Tickers crypto-correlacionados: alertar inmediatamente aunque mercado esté cerrado
# (informan trades en Binance o preparación para apertura del lunes)
CRYPTO_ALWAYS_ALERT = {"MSTR", "MARA", "RIOT", "IREN"}

# ── Contadores globales para heartbeat ───────────────────────────────────────
_stats = {"analyzed": 0, "alta": 0, "media": 0, "baja": 0, "sms_sent": 0}
_stats_lock = threading.Lock()

# ── Cola de acumulación durante mercado cerrado ───────────────────────────────
_weekend_queue: list[dict] = []
_weekend_lock = threading.Lock()


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
    try:
        from watchlist import get_tickers
        tickers = get_tickers()
        if tickers:
            return tickers
    except Exception:
        pass
    wl = config.get("watchlist", {})
    tickers = []
    for cat in ["primary", "extended"]:
        tickers.extend(wl.get(cat, []))
    return list(dict.fromkeys(tickers))


def get_env_or_die(env_var: str) -> str:
    val = os.environ.get(env_var, "")
    if not val:
        logger.error(f"Variable requerida no configurada: {env_var}")
        sys.exit(1)
    return val


# ── Supervisor de threads ─────────────────────────────────────────────────────

def is_market_open() -> bool:
    """
    Retorna True si el mercado eToro está accesible para acciones US.
    Cerrado: viernes 3pm Lima → domingo 7pm Lima.
    """
    now = datetime.now(LIMA)
    weekday = now.weekday()   # 0=lun … 4=vie … 5=sab … 6=dom
    hour = now.hour
    minute = now.minute
    total_minutes = hour * 60 + minute

    if weekday == 4 and total_minutes >= 15 * 60:   # viernes ≥ 15:00 Lima
        return False
    if weekday == 5:                                 # sábado completo
        return False
    if weekday == 6 and total_minutes < 19 * 60:    # domingo antes de 19:00 Lima
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
        try:
            logger.info(f"[{name}] Thread iniciado")
            target_fn(*args, **kwargs)
            # Si retorna sin error (no debería en loops normales)
            logger.warning(f"[{name}] Loop terminó inesperadamente. Reiniciando en {delay}s")
        except Exception as exc:
            logger.error(f"[{name}] Caida inesperada: {exc}. Reiniciando en {delay}s")
        time.sleep(delay)
        delay = min(delay * 2, 60)


# ── Lógica central ────────────────────────────────────────────────────────────

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

    if dedup.is_seen(article_id):
        return

    passes, reason = passes_filter(
        article=article,
        watchlist=watchlist,
        seen_ids=set(),
        max_age_minutes=config.get("max_article_age_minutes", 45),
    )
    if not passes:
        logger.debug(f"Filtro [{reason}]: {article.get('title', '')[:70]}")
        dedup.mark_seen(article_id, article.get("source", ""))
        return

    ticker = (article.get("tickers_found") or ["UNKNOWN"])[0]
    source = article.get("source", "")
    age = article.get("age_minutes", 0)
    score = article.get("keyword_score", 0)
    raw_text = article.get("raw_text", "")

    # Dedup cross-source (mismo evento, distinta fuente — ventana 20 min)
    fingerprint = dedup.get_event_fingerprint(ticker, raw_text)
    if dedup.is_cross_source_duplicate(ticker, fingerprint, window_minutes=20):
        logger.info(f"[CROSS-DUP] {ticker} mismo evento <20min — skip | {article.get('title', '')[:60]}")
        dedup.mark_seen(article_id, source)
        return

    logger.info(
        f"[CANDIDATO] {ticker} | {source} | {age:.0f}min | "
        f"score={score} | {article.get('title', '')[:70]}"
    )

    # Pre-check de precio (evita llamar a Claude si ya está priceado sin alertas previas)
    # Noticias breaking (<5 min) tienen el doble de margen — el precio puede seguir corriendo
    price_data = get_realtime_price(ticker)
    change_pct = price_data.get("change_pct")
    last_alert = dedup.get_last_alert_on_ticker(ticker, hours=4)
    article_age = article.get("age_minutes", 999)

    if change_pct is not None and last_alert is None:
        threshold = PRICED_THRESHOLDS.get(ticker, config.get("already_priced_threshold_pct", 10.0))
        # Breaking news (<5 min): usar el doble del umbral — el precio puede seguir corriendo
        effective_threshold = threshold * 2 if article_age < 5 else threshold
        if abs(change_pct) > effective_threshold:
            logger.info(
                f"[PRE-CHECK] {ticker} movio {change_pct:+.1f}% "
                f"(umbral={effective_threshold:.0f}%, noticia={article_age:.0f}min) "
                f"→ auto-DESCARTADO (ahorra 1 llamada Claude)"
            )
            dedup.mark_seen(article_id, source)
            dedup.mark_ticker_event(ticker, fingerprint, source, sent_to_claude=False)
            return

    # Claude scoring
    model = os.environ.get("CLAUDE_MODEL", config.get("claude_model", "claude-sonnet-4-6"))
    result = score_with_claude(article, price_data, model=model)

    prioridad = result.get("prioridad", "BAJA")
    direction = result.get("direccion", "")

    log_claude_analysis(result)
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

    sms_enviado = False
    if prioridad in send_priorities:
        market_open = is_market_open()
        crypto_ticker = ticker in CRYPTO_ALWAYS_ALERT

        if market_open or crypto_ticker:
            # Envío inmediato: mercado abierto o ticker crypto-proxy
            sms_enviado = send_sms(result, twilio_from, twilio_to)
            if sms_enviado:
                with _stats_lock:
                    _stats["sms_sent"] += 1
        else:
            # Mercado cerrado: acumular para el digest del domingo 7pm Lima
            with _weekend_lock:
                _weekend_queue.append(result)
            logger.info(
                f"[ACUMULADO] {ticker} {prioridad} — mercado cerrado, "
                f"se incluirá en digest del domingo 7pm Lima "
                f"(cola: {len(_weekend_queue)} items)"
            )

    if prioridad in send_priorities or prioridad in log_priorities:
        log_alert(result, sms_enviado=sms_enviado, _skip_analysis_log=True)

    dedup.mark_seen(article_id, source)


# ── Loops de fuentes ─────────────────────────────────────────────────────────

def edgar_loop(config, watchlist, dedup, **kwargs):
    interval = config.get("intervals_seconds", {}).get("edgar", 90)
    while True:
        try:
            articles = fetch_8k_filings(watchlist)
            for article in articles:
                process_article(article, watchlist, dedup, config, **kwargs)
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
            batch = watchlist[ticker_index: ticker_index + batch_size]
            ticker_index = (ticker_index + batch_size) % len(watchlist)
            fetched = 0
            for ticker in batch:
                articles = fetch_company_news(ticker, hours_back=2)
                fetched += len(articles)
                for article in articles:
                    process_article(article, watchlist, dedup, config, **kwargs)
            market_articles = fetch_market_news()
            for article in market_articles:
                if not article.get("tickers_found"):
                    from filters.keyword_filter import extract_tickers_from_text
                    article["tickers_found"] = extract_tickers_from_text(
                        article.get("raw_text", ""), watchlist
                    )
                if article.get("tickers_found"):
                    process_article(article, watchlist, dedup, config, **kwargs)
                    fetched += 1
            cycle += 1
            tickers_done = cycle * batch_size
            progress = min(tickers_done, len(watchlist))
            logger.info(
                f"Finnhub: ciclo {cycle} | batch {batch[0] if batch else '?'}-{batch[-1] if batch else '?'} "
                f"({progress}/{len(watchlist)} tickers) | {fetched} artículos obtenidos"
            )
        except Exception as e:
            logger.error(f"Error en Finnhub poll: {e}")
        time.sleep(interval)


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

        with _weekend_lock:
            items = list(_weekend_queue)
            _weekend_queue.clear()

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

        try:
            from twilio.rest import Client as TwilioClient
            from alerts.twilio_sms import WHATSAPP_SANDBOX_FROM
            sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
            token = os.environ.get("TWILIO_AUTH_TOKEN", "")
            if sid and token and twilio_to:
                client = TwilioClient(sid, token)
                channel = os.environ.get("NOTIFICATION_CHANNEL", "whatsapp").lower()
                if channel == "whatsapp":
                    client.messages.create(
                        body=msg_body,
                        from_=WHATSAPP_SANDBOX_FROM,
                        to=f"whatsapp:{twilio_to}",
                    )
                else:
                    client.messages.create(body=msg_body, from_=twilio_from, to=twilio_to)
                logger.info(
                    f"[WeekendDigest] Digest enviado: {len(top)} oportunidades "
                    f"({n_alta} ALTA, {n_media} MEDIA)"
                )
        except Exception as e:
            logger.error(f"[WeekendDigest] Error enviando digest: {e}")


def heartbeat_loop(twilio_from: str, twilio_to: str):
    """
    Envía SMS diario a las 8am Lima con resumen del sistema.
    Si no recibes este SMS → el sistema está caído.
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
                _stats.update({"analyzed": 0, "alta": 0, "media": 0, "baja": 0, "sms_sent": 0})

            fecha = datetime.now(LIMA).strftime("%d/%m/%Y")
            channel = os.environ.get("NOTIFICATION_CHANNEL", "whatsapp").lower()

            # Estado del mercado y cola acumulada
            market_open = is_market_open()
            with _weekend_lock:
                queued = len(_weekend_queue)

            if market_open:
                market_line = "📈 Mercado: ABIERTO"
            else:
                market_line = f"🔒 Mercado: CERRADO — {queued} oportunidad{'es' if queued != 1 else ''} acumulada{'s' if queued != 1 else ''}"

            msg_body = (
                f"✅ *OportunityAlert* activo — {fecha}\n"
                f"Analizadas: {analyzed} | Alertas enviadas: {sms_sent}\n"
                f"ALTA:{alta}  MEDIA:{media}  BAJA:{baja}\n"
                f"{market_line}"
            )
            from twilio.rest import Client as TwilioClient
            from alerts.twilio_sms import WHATSAPP_SANDBOX_FROM
            sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
            token = os.environ.get("TWILIO_AUTH_TOKEN", "")
            if sid and token and twilio_to:
                client = TwilioClient(sid, token)
                if channel == "whatsapp":
                    client.messages.create(
                        body=msg_body,
                        from_=WHATSAPP_SANDBOX_FROM,
                        to=f"whatsapp:{twilio_to}",
                    )
                else:
                    client.messages.create(
                        body=msg_body, from_=twilio_from, to=twilio_to
                    )
                logger.info(f"Heartbeat enviado: {msg_body.replace(chr(10), ' | ')}")
        except Exception as e:
            logger.error(f"Error en heartbeat: {e}")


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
    print(f"  Ver watchlist:          python watchlist.py")
    print(f"{sep}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
        return

    _setup_logging()
    logger.info("=" * 56)
    logger.info("OportunityAlert v1.0 — Iniciando")
    logger.info("=" * 56)

    config = load_config("config.json")
    watchlist = get_watchlist(config)
    logger.info(f"Watchlist: {len(watchlist)} tickers")

    api_keys = config.get("api_keys", {})
    ai_engine = os.environ.get("AI_ENGINE", "claude").lower()
    if ai_engine == "claude":
        get_env_or_die(api_keys.get("anthropic_key_env", "ANTHROPIC_API_KEY"))
    get_env_or_die(api_keys.get("finnhub_key_env", "FINNHUB_API_KEY"))

    if not os.environ.get(api_keys.get("alpaca_key_env", "ALPACA_API_KEY")):
        logger.warning("ALPACA_API_KEY no configurada — sin precios real-time")
    if not os.environ.get(api_keys.get("twilio_sid_env", "TWILIO_ACCOUNT_SID")):
        logger.warning("TWILIO no configurado — sin SMS")

    twilio_from = api_keys.get("twilio_from", "")
    twilio_to = api_keys.get("twilio_to", "")
    send_priorities = config.get("send_sms_priorities", ["ALTA", "MEDIA"])
    log_priorities = config.get("log_only_priorities", ["BAJA"])

    dedup = DedupStore("data/seen_ids.db")
    logger.info(f"DedupStore: {dedup.count()} entradas previas")

    market_status = "ABIERTO" if is_market_open() else "CERRADO (modo acumulación activo)"
    logger.info(f"Estado mercado al inicio: {market_status}")

    shared = dict(
        twilio_from=twilio_from,
        twilio_to=twilio_to,
        send_priorities=send_priorities,
        log_priorities=log_priorities,
    )

    # Lanzar todos los threads con supervisor de auto-reinicio
    threads_cfg = [
        ("EDGAR",         edgar_loop,           (config, watchlist, dedup), shared),
        ("Finnhub",       finnhub_loop,         (config, watchlist, dedup), shared),
        ("Reddit",        reddit_loop,          (config, watchlist, dedup), shared),
        ("Price24h",      price_update_loop,    (),                         {}),
        ("Heartbeat",     heartbeat_loop,       (twilio_from, twilio_to),   {}),
        ("WeekendDigest", weekend_digest_loop,  (twilio_from, twilio_to),   {}),
    ]

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
