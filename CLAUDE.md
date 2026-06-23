# OportunityAlert v2.1 — Instrucciones para Claude Code
# Sistema de alertas + convicción + gestión de portafolio + event mode
# Proyecto de Oscar Navarro — v2.1 (2026-05-24)

---

## ⚡ ACTUALIZACIÓN 2026-05-30 — LEE ESTO PRIMERO (reescritura en curso)

El sistema está en reconstrucción hacia UN proyecto unificado (Noticias + Pre-market +
Marea + Posiciones). **La fuente de verdad viva de pendientes/decisiones es `REBUILD_PLAN.md`.**
Lo de abajo (v2.1) sigue siendo válido como base salvo estos cambios ya aplicados:

- **Sistema 100% MANUAL.** El auto-trading fue DESMANTELADO: `etoro_trader.py` +
  `position_lock.py` → `_quarantine/`; eliminados de `api/app.py` la función
  `_execute_auto_trade` y los endpoints `/api/watcher/auto-trade` y `/api/position-lock`.
  eToro READ-ONLY ahora es realidad consolidada, no solo intención.
- **Watcher:** murió como motor de trades intradía (1m scalping net-negativo por fees,
  auditado). Se reutilizará como ADVISOR de salida de posiciones (pendiente).
- **Pre-market scanner:** reorientado — universo large-cap 24/5 only (`LARGE_CAP_24X5`),
  exclusión de earnings (`_has_earnings_blackout`), gate `code_score>=6` REMOVIDO
  (validado que dañaba). Edge validado pero MU-dependiente → falta muestra + clasificación IA.
- **Keyword filter:** upgrades de analista promovidos a Tier-1.
- **Watchlist:** fuente única canónica = tabla `watchlist` en `metrics.db` (frontend);
  `config.json` solo seed; API anota `tradeable_24x5`. `watchlist.py/txt` → `_deprecated/`.
- **Estructura nueva:** `research/` (backtests/auditorías), `_quarantine/` (auto-trader),
  `_deprecated/` (CLI watchlist legacy).

---

## ROL Y MISIÓN

Eres el asistente de desarrollo para **OportunityAlert**: un programa Python 24/7 que monitorea noticias financieras, filtra catalizadores y alerta a Oscar por SMS/WhatsApp. Evalúa convicción técnica antes de llamar a la IA, maneja eventos extraordinarios con flujo de dos alertas, y monitorea posiciones abiertas en eToro para notificarle cuándo actuar.

---

## REGLA DE COSTOS — NO NEGOCIABLE

**Código antes que IA. Siempre.**

| Tarea | Cómo hacerla |
|-------|-------------|
| RSI, EMA, ATR, Bollinger | Código puro — fórmulas sobre barras diarias Alpaca |
| Precio actual | Alpaca API (`utils/alpaca_price.py`) |
| Detección "catalizador ya priceado" | Código: `abs(change_pct) > threshold` |
| Volumen alto vs. normal | Código: comparar velas 1m vs. promedio |
| Calcular stop y target | Código: `precio ± N × ATR14` |
| Estabilización post-spike (event mode) | Código: velas 1m en tiempo real |
| T1 alcanzado | Código: `pnl_pct >= 8.0` |
| Stop en riesgo | Código: `precio_actual <= stop_alert × 1.01` |
| Clasificar catalizador (FDA, contrato, upgrade…) | IA — solo esto |
| Dirección LONG/SHORT y magnitud % | IA — prompt reducido |

**Resultado actual:** ~60-70% de reducción de llamadas a IA vs. v1.0.

---

## ARQUITECTURA v2.1

```
NOTICIA RECIBIDA (EDGAR / Finnhub / Reddit)
         |
[CAPA 1] Keyword filter  — score >= 3 para pasar
         |
[DETECCION] detect_event_type()
         |                    |
    EVENT MODE           MODO NORMAL
    (earnings/FDA/       (todos los demas)
     breaking/etc.)
         |                    |
[CAPA 2] Conviction Gates (codigo puro, <1 seg)
  Gate 1: Catalizador no priceado   (0 o 3 pts)  -- ambos modos
  Gate 2: Tecnico favorable         (0-3 pts)
     - NORMAL: RSI14 + EMA20 + Bollinger
     - EVENT:  event_gate2_score() -- velas 1m stabilization
  Gate 3: Volumen confirma          (0 o 1 pt)
  Score 0-7
  - NORMAL: si < 4 -> DESCARTAR sin IA
  - EVENT:  Gate 1 = 0 -> DESCARTAR; si pasa Gate 1 -> siempre a IA
         |
[CAPA 3] AI Scoring reducido
  Solo pide: tipo catalizador + direccion + magnitud %
  Stops y targets los calcula codigo (ATR-based)
         |
[CAPA 4] Gates post-IA (codigo)
  Gate 4: Playbook match           (bonus informativo)
  Gate 5: Portfolio gate eToro     (puede bloquear)
         |                    |
    EVENT MODE           MODO NORMAL
         |                    |
  SMS 1: aviso inmediato    SMS unico con entrada
  "No entrar aun"
  queue_followup() -> 7 min
         |
  SMS 2 (7 min despues):
  analyze_stabilization() con precio fresco
  ENTRAR_AHORA / ESPERAR / DESCARTAR

[THREAD: PositionTracker — cada 10 min]
  - Compara snapshots -> detecta posiciones cerradas
  - Stop en riesgo (vs portfolio.md)     -> SMS URGENTE
  - T1 alcanzado (P&L >= 8%)             -> SMS INFO
  - Retroceso desde pico (>= 4%)         -> SMS ATENCION
  - Movimiento brusco (>3%) sin noticia  -> SMS explicacion
    (news lookup en metrics_store, 90 min ventana)

[THREAD: MetricsStore — siempre activo]
  - Loggea cada alerta, gate_event, snapshot de posicion, trade
  - CLI: python show_metrics.py [--days N]
```

---

## ESTRUCTURA DEL PROYECTO (v2.1 — estado actual)

```
opportunity_alert/
├── main.py                     <- loop principal 24/7, threads
├── config.json                 <- watchlist, API keys, intervalos
├── show_metrics.py             <- CLI dashboard de metricas
├── filters/
│   ├── keyword_filter.py       <- filtro keywords Tier-1/2
│   └── claude_scorer.py        <- Gemini/Claude con prompt reducido + conviction
├── alerts/
│   ├── twilio_sms.py           <- SMS/WhatsApp Twilio (conviction_score incluido)
│   └── alert_logger.py         <- escribe alerts.json
├── utils/
│   ├── alpaca_price.py         <- precio real-time + velas 1m
│   ├── dedup_store.py          <- SQLite dedup + tracker_flags + position_peaks
│   ├── conviction_gates.py     <- Gates 1-3 (codigo puro, RSI/EMA/ATR/BB)
│   ├── etoro_client.py         <- Wrapper READ-ONLY eToro + circuit breaker
│   ├── playbook_matcher.py     <- keyword -> estrategia del playbook
│   ├── metrics_store.py        <- SQLite metrics (5 tablas)
│   ├── event_gate.py           <- deteccion eventos + stabilization analysis
│   └── delayed_alerts.py       <- worker thread SMS 2 (followup 7 min)
├── sources/
│   ├── edgar.py                <- SEC EDGAR RSS 90s
│   ├── finnhub_news.py         <- Finnhub 120s
│   └── reddit_monitor.py       <- Reddit 600s (desactivado en Oracle Cloud)
└── data/
    ├── alerts.json             <- log JSON Lines
    ├── claude_analysis.log     <- log legible de todos los analisis
    ├── seen_ids.db             <- SQLite dedup
    └── metrics.db              <- SQLite metricas (gates, alertas, trades)
```

**Archivos de configuracion (dentro del proyecto, no commitear con credenciales reales):**
- `data/etoro_config.json` — credenciales eToro API
- `data/portfolio.md` — stops, horizontes, posiciones abiertas
- `utils/playbook_matcher.py` — estrategias hardcodeadas (no archivo externo)

---

## MODULO: `utils/conviction_gates.py`

Puntua 0-7 usando codigo puro (sin IA). Si `skip_ai=True`, se descarta sin llamar a Gemini/Claude.

### Gate 1 — Catalizador no priceado (0 o 3 pts)
- `change_pct < PRICED_THRESHOLDS.get(ticker, 12)` → 3 pts; si supera → 0 pts
- Gate 1 = 0 → `skip_ai=True` inmediato, no evaluar Gate 2 ni 3

### Gate 2 — Confirmacion tecnica (0-3 pts, 1 por sub-gate)
- Fetches barras diarias Alpaca (`/v2/stocks/{ticker}/bars?timeframe=1Day&limit=60`)
- Sub-gates LONG: RSI14 < 72 | precio > EMA20 × 0.97 | precio < BB_upper × 0.99
- Sub-gates SHORT: RSI14 > 28 | precio < EMA20 × 1.03 | precio > BB_lower × 1.01
- Si Alpaca falla en barras: gate2_score = 1 (continua)

### Gate 3 — Volumen confirma (0 o 1 pt)
- Usa `candles_1m` ya disponibles de `get_realtime_price()`
- `recent_vol (3 velas) > avg_vol × 1.3` → 1 pt
- Si mercado cerrado >30 min o <5 velas: gate3_score = 1 (ok, AH no representativo)

### Calculos ATR (por codigo, no IA)
```python
ATR_MULT = {
    "QUANTUM":  (2.0, 4.0),   # QBTS, IONQ, RGTI, QUBT
    "SMALL_AI": (2.0, 3.5),   # SOUN, BBAI, INOD, APLD, IREN
    "SPACE":    (1.8, 3.0),   # RKLB, LUNR, JOBY, ACHR
    "HIGHVOL":  (1.5, 2.5),   # NVDA, TSLA, PLTR, APP, SMCI
    "DEFAULT":  (1.5, 2.5),
}
```
`stop_code = entry ± stop_mult × atr14`, `target_code = entry ∓ target_mult × atr14`

### Logica skip_ai
```python
skip_ai = (gate1_score == 0) or (conviction_score < 4)
# En EVENT MODE: solo Gate 1 puede bloquear (skip_ai = gate1_score == 0)
```

---

## MODULO: `utils/event_gate.py`

Maneja eventos donde los indicadores tecnicos historicos (RSI/EMA) son irrelevantes porque el precio reacciona a informacion nueva, no a patrones pasados.

### Deteccion de tipo de evento
```python
detect_event_type(article, price_data) -> str
# "earnings" | "fda" | "government_contract" | "breaking_news" | "large_move" | "normal"
```
- Earnings: keywords (eps, revenue, beat, miss, guidance, quarterly…)
- FDA: keywords (fda, approval, pdufa, clinical trial, phase 3…)
- Government contract: keywords (dod, pentagon, chips act, award…)
- Breaking news: `age_minutes < 4`
- Large move: `abs(change_pct) > 10`

### Analisis de estabilizacion (reemplaza Gate 2 en event mode)
```python
analyze_stabilization(candles_1m, direction) -> dict
# signal: "ENTRAR_AHORA" | "ESPERAR_CONFIRMACION" | "DESCARTAR" | "SIN_DATOS"
# confidence: "ALTA" | "MEDIA" | "BAJA"
# event_gate_score: 0-3
```
Analiza 3 criterios (1 pt c/u):
1. **Volumen sostenido**: `avg_recent >= avg_older × 0.7`
2. **Rango achicando**: `avg_recent_range < avg_older_range × 0.85` (volatilidad decrece)
3. **Direccion confirmada**: higher_lows (LONG) o lower_highs (SHORT) en 4 velas

Score 3 → ENTRAR_AHORA/ALTA | Score 2 → ENTRAR_AHORA/MEDIA | Score 1 → ESPERAR/BAJA | Score 0 → DESCARTAR/BAJA

### Formato SMS eventos
- `format_event_watch_sms()` → SMS 1: aviso inmediato, sin señal de entrada
- `format_event_confirm_sms()` → SMS 2: confirmacion o descarte con precio fresco

---

## MODULO: `utils/delayed_alerts.py`

Worker daemon thread que procesa la cola de followups (SMS 2).

### Flujo
1. `start_worker()` — llamar una sola vez en `main()`; inicia daemon thread
2. `queue_followup(ticker, article, conviction, result, event_type, twilio_to, delay_seconds=420)`:
   - Dedup por ticker: si ya hay un followup pendiente, retorna False
   - Encola item con `fire_at = time.time() + delay_seconds`
3. Worker espera hasta `fire_at`, luego llama `_process_followup()`
4. `_process_followup()`:
   - Re-fetches precio fresco de Alpaca
   - Corre `analyze_stabilization()` con velas 1m actualizadas
   - Recalcula stop/target con precio fresco + ATR del conviction original
   - Envia SMS 2 con veredicto de entrada

### Dedup
- `_queued_tickers: set` protegido con `threading.Lock()`
- Un ticker solo puede tener un followup pendiente a la vez

---

## MODULO: `utils/etoro_client.py`

Wrapper READ-ONLY para la eToro API. NUNCA ejecuta ordenes.

### Endpoint correcto (verificado en vivo 2026-05-30)
```
GET https://public-api.etoro.com/api/v1/trading/info/portfolio
Headers: x-api-key, x-user-key, x-request-id, Content-Type
```
**NOTA CRITICA**: host = `public-api.etoro.com` (NO `api.etoro.com`) y prefijo `/api/v1/`.
La ruta sin `/api/v1/` o contra `api.etoro.com` devuelve 404. `api_base` en
`etoro_config.json` = `https://public-api.etoro.com`.

### Estructura de respuesta
```
data["clientPortfolio"]["positions"][]  <- lista de posiciones
data["clientPortfolio"]["credit"]       <- efectivo disponible
```

### Circuit breaker
- `CIRCUIT_OPEN_THRESHOLD = 5` fallos consecutivos
- `CIRCUIT_PAUSE_MINUTES = 30` pausa cuando se abre el circuito
- 401/403 → `_notify_token_expired()` envia SMS alerta (una sola vez, no spam)
- `reset_token_alert()` → resetear despues de actualizar `etoro_config.json`

### Token de sesion
- `user_key` es un blob base64 (NO es JWT estandar, no tiene campo `exp`)
- Decodifica a: `{"ci": "...", "ean": "UnregisteredApplication", "ek": "..."}`
- No hay forma de leer expiry del token; se detecta via 401/403

### Funciones principales
- `get_portfolio() -> dict` — posiciones + cash + total_value
- `check_portfolio_gate(ticker) -> dict` — verifica si puede entrar (ya en posicion, cash < $200, sector > 40%)
- `health_check() -> dict` — ping rapido para heartbeat (no cuenta en circuit breaker)

---

## MODULO: `utils/metrics_store.py`

SQLite en `data/metrics.db` con 5 tablas. Instanciar como singleton global en `main()`.

### Tablas
| Tabla | Proposito |
|-------|-----------|
| `alerts` | Cada alerta enviada (ticker, prioridad, conviction_score, direccion…) |
| `gate_events` | Cada evaluacion de gates (gate1/2/3 score, skip_ai, event_mode) |
| `position_snapshots` | Snapshot eToro cada 10 min (pnl_pct, current_price…) |
| `trades` | Posiciones cerradas detectadas por snapshot comparison |
| `daily_summary` | Resumen diario (totales, tasas de precision) |

### Metodos clave
- `log_alert(ticker, result, event_type)` — registra alerta
- `log_gate_event(ticker, conviction, event_mode)` — registra evaluacion de gates
- `log_position_snapshot(positions)` — registra snapshot de cartera
- `get_open_tickers_last_snapshot()` — tickers en posicion en snapshot anterior
- `record_closed_trade(ticker, entry, exit_price, pnl_pct)` — registra trade cerrado, auto-matchea con alerta
- `get_recent_news_for_ticker(ticker, minutes=90)` — busca alertas recientes para explicar movimientos
- `get_accuracy_summary(days)`, `get_gate_efficiency(days)`, `get_recent_trades(limit)`

### CLI
```bash
python show_metrics.py          # ultimos 7 dias
python show_metrics.py --days 30
```

---

## MODULO: `utils/dedup_store.py`

SQLite en `data/seen_ids.db`. Tres tablas:

| Tabla | Proposito |
|-------|-----------|
| `seen_articles` | Dedup de articulos procesados |
| `tracker_flags` | Cooldown flags para Position Tracker (con TTL) |
| `position_peaks` | Pico de P&L por posicion (ticker + entry_price) |

### Metodos nuevos en v2.0
- `set_flag(key, ttl_hours=48.0)` — setea flag con expiry
- `has_flag(key) -> bool` — verifica si flag existe y no expiro
- `update_peak_pnl(ticker, entry, pnl_pct)` — actualiza si nuevo pnl > peak
- `get_peak_pnl(ticker, entry) -> float` — retorna peak guardado

---

## MODULO: `utils/playbook_matcher.py`

Mapeo de keywords en `resumen_cataliz` a estrategias del playbook de Oscar.

```python
find_matching_strategies(catalyst_summary) -> list[str]
# Ejemplos: "contract" -> ["patron_a"], "earnings beat" -> ["ped","e1","e2"]
```

Se usa en Gate 4 (informativo, no bloquea).

---

## MODULO: `filters/claude_scorer.py`

### Cambios vs v1.0
- Prompt reducido ~55%: la IA ya NO calcula stops ni targets
- `score_with_claude(article, price_data, model=None, conviction=None)` — acepta `conviction`
- Post-procesamiento inyecta `entrada_rango`, `stop`, `target` desde `conviction_gates`
- `_calc_exit_date(horizonte)` — "mismo dia"→14:45 Lima, "Day+1"→09:00+1, "3-5 dias"→14:00+4
- `max_tokens` reducido de 1024 a 512

### JSON que pide a la IA
```json
{
  "ticker", "prioridad", "tipo_catalizador", "direccion",
  "pct_estimado", "catalizador_priceado", "resumen_cataliz",
  "timing_entrada", "horizonte_tiempo", "riesgo", "confianza"
}
```
Stops y targets NO van en el JSON — se calculan por codigo.

---

## FLUJO `process_article()` en `main.py`

```python
1. detect_event_type(article, price_data)
   -> event_mode = is_event_mode(event_type)

2. evaluate_conviction(ticker, price_data, direction)
   - En event_mode: Gate 2 = event_gate2_score() (no RSI/EMA)
   - En event_mode: skip_ai solo si gate1_score == 0

3. log_gate_event(_metrics, ...)

4. Si skip_ai: return (descartar sin IA)

5. score_with_claude(article, price_data, conviction=conviction)

6. Gate 4: find_matching_strategies(result["resumen_cataliz"])
7. Gate 5: check_portfolio_gate(ticker)
   - Si can_enter=False y prioridad=ALTA: bajar a MEDIA

8. log_alert(_metrics, ...)

9. SMS:
   - EVENT MODE: format_event_watch_sms() + queue_followup() -> SMS 2 en 7 min
   - NORMAL:     send_sms() con conviction_score, playbook, portfolio
```

---

## FLUJO `position_tracker_loop()` en `main.py`

Corre cada 10 min en thread separado.

ADVISOR de salida POR ESTRATEGIA (no ejecuta órdenes). Reescrito 2026-05-30:
las heurísticas viejas (T1 +8% / breakeven / retroceso 4% / portfolio.md) se ELIMINARON
porque cortaban ganadores de trend-following.

```python
1. get_portfolio()           <- eToro API (read-only)
2. _sync_position_watchlist() <- auto-añade posiciones abiertas al universo de noticias
                                 (category='position'); retira las cerradas
3. Compara tickers vs snapshot anterior -> posicion desaparecida = record_closed_trade()
4. _explain_large_moves()    <- movimiento >3% -> news lookup (90 min) = CONTEXTO, no decisión
5. Para cada posicion abierta -> utils/position_strategy.evaluate_position():
   - estrategia: HÍBRIDA = override dashboard > auto-match pilot_state.json > "manual"
   - marea  -> chandelier (hh_desde_entrada - 4×ATR22) sobre cierre diario -> SMS URGENTE "VENDER al open"
   - ped    -> tiempo (days_held >= 6 ~ Day+7)                              -> SMS INFO "cerrar al open"
   - manual -> sin salida automática (solo contexto de noticias)
   - dedup: 1 alerta por ticker+regla+día (re-avisa al día siguiente si no actúa)
6. log_position_snapshot(_metrics, positions)
```

Tags de override en `data/position_tags.json`. entry_date sale del `openDateTime` real de eToro.
Barras diarias cacheadas por día (≤1 fetch Alpaca por ticker/día).

---

## VARIABLES DE ENTORNO

```bash
# Ya deben estar en .env
# ── Motor de IA: DeepSeek, dos tiers (migración 2026-06-19) ──
AI_ENGINE=deepseek         # deepseek (default) | glm | gemini | claude
DEEPSEEK_API_KEY=sk-...     # obligatoria si AI_ENGINE=deepseek (platform.deepseek.com)
AI_MODEL_STRONG=deepseek-v4-pro    # tier FUERTE: noticias / decisiones (razonador)
AI_MODEL_CHEAP=deepseek-chat       # tier BARATO: radar Trump (no-pensante)
# GLM_API_KEY=...          # alternativa si AI_ENGINE=glm (api.z.ai)
# ANTHROPIC_API_KEY / GEMINI_API_KEY  # opcionales: solo si AI_ENGINE=claude|gemini
FINNHUB_API_KEY=...
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_TO=+51...           # numero destino (también receptor del WhatsApp del Brief)
TWILIO_FROM=+1...          # numero origen (SMS) — no necesario en WhatsApp
NOTIFICATION_CHANNEL=callmebot  # "callmebot" (WhatsApp gratis) | "whatsapp" | "sms"
```

**Selección de motor/modelo (centralizado en `utils/ai_client.py`):** todos los módulos
usan `resolve_engine_model(tier)` — tier `"strong"` (noticias + brief diario) o `"cheap"` (radar Trump).
DeepSeek/GLM se hablan por REST compatible-OpenAI (con `requests`, sin deps nuevas).
Overrides por sección: `DAILY_BRIEF_ENGINE`/`DAILY_BRIEF_MODEL`, `TRUMP_ENGINE`/`TRUMP_MODEL`.

No se necesitan nuevas variables para eToro — lee `etoro_config.json` directamente.

---

## PARAMETROS Y UMBRALES

```python
# Gates
GATE_SKIP_THRESHOLD = 4       # score minimo para llamar IA (modo normal)
BREAKING_AGE_THRESHOLD = 4    # minutos: noticia < 4 min = breaking_news

# Event mode
FOLLOWUP_DELAY_SECONDS = 420  # 7 minutos entre SMS 1 y SMS 2

# ATR multipliers
QUANTUM_STOP = 2.0,  QUANTUM_TARGET = 4.0
SMALL_AI_STOP = 2.0, SMALL_AI_TARGET = 3.5
SPACE_STOP = 1.8,    SPACE_TARGET = 3.0
HIGHVOL_STOP = 1.5,  HIGHVOL_TARGET = 2.5
DEFAULT_STOP = 1.5,  DEFAULT_TARGET = 2.5

# Position tracker
POSITION_POLL = 600           # 10 minutos
T1_THRESHOLD = 8.0            # % P&L para T1
PEAK_RETRACE = 4.0            # % retroceso desde pico para alertar
ALERT_COOLDOWN_T1 = 48        # horas antes de re-notificar T1
ALERT_COOLDOWN_STOP = 4       # horas antes de re-notificar stop
EXPLAIN_MOVE_THRESHOLD = 3.0  # % movimiento brusco a explicar
EXPLAIN_COOLDOWN = 60         # minutos entre explicaciones del mismo ticker

# Portfolio
MIN_CAPITAL_USD = 200
POSITION_BASE_PCT = 0.10      # 10% del disponible
POSITION_MAX_PCT = 0.20       # maximo 20% del total
MAX_SECTOR_PCT = 0.40         # maximo 40% en un sector
```

---

## NOTAS CRITICAS

1. **eToro API es READ-ONLY** — `etoro_config.json` tiene `"environment": "real"`. Este proyecto NUNCA ejecuta ordenes. Solo lee posiciones y precios.

2. **Endpoint eToro correcto**: `https://public-api.etoro.com/api/v1/trading/info/portfolio`. Host `public-api.etoro.com` + prefijo `/api/v1/` son obligatorios; `api.etoro.com` o sin `/api/v1/` devuelve 404.

3. **Token eToro**: `user_key` es base64 de `{"ci":"...","ean":"UnregisteredApplication","ek":"..."}`. No es JWT, no tiene campo `exp`. La expiry se detecta via 401/403.

4. **Windows cp1252**: Evitar emojis en output de consola/logs en Windows. `show_metrics.py` usa solo ASCII. Los SMS si pueden tener emojis (va por Twilio, no por consola).

5. **Rutas independientes**: `etoro_config.json` y `portfolio.md` viven en `data/` dentro del proyecto. No hay dependencias externas fuera del repo.

6. **Gemini > Claude para este proyecto**: `AI_ENGINE=gemini` ahorra dinero. Gemini 2.0 Flash es gratuito hasta 1,500 req/dia. Con los gates filtrando ~70% de noticias, el consumo de IA es bajo.

7. **Logs con prefijos**: Filtrar con `grep -i "[GATES]\|[PositionTracker]\|[DelayedAlert]\|[eToro]\|[EventGate]"` para diagnosticar subsistemas especificos.

8. **No duplicar indicadores**: `conviction_gates.py` implementa EMA, RSI, ATR puros con barras Alpaca. No usar `C:/Users/LENOVO/trading/indicators.py` (usa yfinance, latencia innecesaria).

---

## COMANDOS DE VERIFICACION

```bash
# Verificar conviction gates
python -c "from utils.conviction_gates import evaluate_conviction; import json; print(json.dumps(evaluate_conviction('NVDA', {'change_pct': 3.5, 'candles_1m': [], 'current_price': 130.0}), indent=2))"

# Verificar event gate
python -c "from utils.event_gate import detect_event_type; print(detect_event_type({'title': 'NVDA beats EPS estimates', 'summary': 'quarterly earnings beat', 'age_minutes': 2}, {'change_pct': 5.0}))"

# Verificar eToro
python -c "from utils.etoro_client import get_portfolio; import json; print(json.dumps(get_portfolio(), indent=2))"

# Ver metricas
python show_metrics.py --days 7

# Correr sistema
python main.py
```

---

*OportunityAlert v2.1 — Oscar Navarro + Claude Code — 2026-05-24*
