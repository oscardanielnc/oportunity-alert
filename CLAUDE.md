# 🚨 OportunityAlert — Sistema de Alertas de Trading en Tiempo Real
# Instrucciones completas para Claude Code
# Proyecto de Oscar Navarro — v1.0

---

## ROL Y OBJETIVO

Eres el asistente de desarrollo para el sistema **OportunityAlert**: un programa Python que corre 24/7 en una VM de Oracle, monitorea noticias financieras en tiempo real, filtra las relevantes y envía alertas SMS vía Twilio cuando detecta catalizadores de alta prioridad para trading.

**El problema que resuelve:** El 21 de mayo 2026, D-Wave (QBTS) recibió $100M del gobierno. El filing SEC (8-K) apareció a la 1:54am Lima. El mercado reaccionó a las 5am Lima (+33%). Con este sistema, la alerta hubiera llegado al celular de Oscar a las 1:56am.

---

## ARQUITECTURA DEL SISTEMA (3 capas)

```
CAPA 1: INGESTA (HTTP polling, sin WebSocket)
    SEC EDGAR RSS   → cada 90 segundos
    Finnhub News    → cada 120 segundos
    Reddit PRAW     → cada 10 minutos

         ↓ ~500-2000 eventos/día
         
CAPA 2: FILTRO PYTHON (sin IA, <5ms por noticia)
    ¿Ticker en watchlist?
    ¿Keyword crítica presente?
    ¿Noticia tiene <45 minutos?
    ¿Ya fue procesada? (dedup por ID/URL)
    
         ↓ ~10-30 candidatos/día

CAPA 3: CLAUDE SONNET SCORING (solo candidatos filtrados)
    Evalúa: ¿priceado ya? + ¿dirección? + ¿magnitud? + prioridad
    Consulta precio REAL AH via Alpaca WebSocket antes de clasificar
    (precio actual de mercado, no cierre — incluye pre-market y AH)
    Output: ALTA / MEDIA / BAJA + acción operativa
    
         ↓
    ALTA → SMS Twilio inmediato
    MEDIA → SMS Twilio inmediato (con indicación de menor urgencia)
    BAJA → Solo registro en archivo
```

---

## DECISIONES DE DISEÑO (no negociar estas)

### Por qué HTTP polling y no WebSocket
- SEC EDGAR no tiene WebSocket — solo RSS/HTTP
- Finnhub WebSocket es para precios, no para noticias
- HTTP polling cada 90-120s es suficiente para capturar noticias antes de que el mercado reaccione (ventana real: 3-15 minutos tras filing)
- Más simple, más estable para 24/7, más fácil de debuggear

### Por qué SEC EDGAR + Finnhub (y no otros)
- **SEC EDGAR:** gratis, oficial, cubre 8-K (contratos, awards, eventos materiales, insider buying). Es el caso de uso más valioso — fue el QBTS de hoy.
- **Finnhub:** ya tiene credenciales Oscar, cubre upgrades de analistas, earnings, noticias generales. Complementa perfectamente a EDGAR.
- **Reddit PRAW:** sentiment social, squeeze alerts. Señal de confirmación, no primaria.
- NO usar: Bloomberg, Refinitiv, Reuters (muy caros). NewsAPI (noticias financieras débiles).

### Por qué Claude Sonnet para scoring
- Oscar tiene API Anthropic configurada (usa Claude Code)
- Sonnet es la mejor relación calidad/costo para análisis de noticias
- Costo real: ~5-8 USD/mes con 30 análisis diarios
- Alternativa económica: Haiku (si el costo sube, cambiar en config)

### Sobre la suscripción Claude Pro de $100/mes
- Esa suscripción es para claude.ai (web UI) — NO incluye API
- La API se factura por tokens en console.anthropic.com
- Con los análisis de este sistema: ~$5-8/mes adicional
- Oscar ya tiene API key configurada (usa Claude Code)

---

## FUENTES DE DATOS — DETALLES TÉCNICOS

### SEC EDGAR RSS Feed
```
URL: https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom
Rate limit: 10 req/seg max — nosotros usamos 1 req/90s = completamente seguro
No requiere API key
Headers requeridos: User-Agent con email real (obligatorio por SEC policy)
Formato: XML/Atom feed con filings recientes
```
Tipos de 8-K más valiosos para trading:
- Item 1.01: Material Definitive Agreement (contratos, partnerships)
- Item 5.02: Departure/Appointment of Directors (insider moves)
- Item 8.01: Other Events (catch-all — aquí entran los government awards)

### Finnhub News API
```
Endpoint: https://finnhub.io/api/v1/company-news?symbol=TICKER&from=DATE&to=DATE&token=API_KEY
Rate limit: 60 req/min (free) — usamos ~20 req/min total
API key: en api_credentials.json del proyecto trading (~/trading/api_credentials.json)
```

### Reddit PRAW
```
Subreddits: wallstreetbets, stocks, investing, SecurityAnalysis
Keywords de squeeze: short squeeze, gamma squeeze, unusual options
Credenciales: en ~/trading/api_credentials.json
```

### Alpaca Markets — Precio Real-Time (FUENTE PRINCIPAL DE PRECIOS)
```
Por qué Alpaca y NO eToro para precios:
- eToro API devuelve precios de cierre o forex — no está diseñada para polling sistemático
- Alpaca es gratuito, da precio ACTUAL incluyendo pre-market y after-hours
- Cubre desde las 4:00am ET (3am Lima) hasta las 8:00pm ET (7pm Lima)
- WebSocket para precio en streaming — sin polling, precio llega en <100ms
- REST API para velas OHLCV (1m, 5m, 15m) también incluyendo horas AH

Setup Alpaca (Oscar debe hacer esto una vez):
1. Crear cuenta gratuita en alpaca.markets (no requiere depósito de dinero)
2. En el dashboard → API Keys → crear Paper Trading key
3. Guardar en config.json: alpaca_key y alpaca_secret

Endpoints usados:
  Precio actual: GET https://data.alpaca.markets/v2/stocks/{ticker}/quotes/latest
  Velas AH:      GET https://data.alpaca.markets/v2/stocks/{ticker}/bars
                 params: timeframe=1Min, feed=iex, start=<hace 2h>
  WebSocket:     wss://stream.data.alpaca.markets/v2/iex

Parámetro crítico: feed=iex (IEX feed, gratuito, incluye AH)
  - feed=sip es de pago (requiere suscripción)
  - feed=iex es gratuito y suficiente para nuestro uso

Lo que obtiene el sistema en cada evaluación:
  - current_price: precio de la última transacción (AH o regular)
  - change_pct: variación vs cierre anterior
  - last_trade_time: confirmar que el precio es fresco (<5 min)
  - candles_1m: últimas 10 velas de 1 minuto para ver momentum
```

### Polygon.io (opción upgrade — $29/mes)
```
Usar si Alpaca tiene limitaciones o se quiere news + precios en un solo proveedor
Ventajas sobre Alpaca: también tiene feed de noticias (puede reemplazar Finnhub)
Desventaja: costo mensual
Recomendación: empezar con Alpaca (gratis), migrar a Polygon si es necesario
```

### eToro API (solo como referencia, NO usar para precios en este sistema)
```
eToro no está diseñado para polling sistemático de precios de acciones
Sus endpoints devuelven precios de cierre o rates de forex/CFDs
Usar eToro solo para ejecutar órdenes manualmente — nunca como fuente de precio
```

---

## WATCHLIST DE TICKERS (cargar desde archivo externo)

```json
{
  "primary": ["NVDA", "TSM", "QBTS", "IONQ", "RGTI", "QUBT", "PLTR", "APP",
              "AVGO", "AMD", "ASML", "ARM", "RDDT", "CRM", "MSFT", "GOOG",
              "CCJ", "TLN", "BE", "EME", "NOC", "RTX", "LMT", "KTOS", "AVAV",
              "UBER", "SHOP", "MU", "COHR", "TSM"],
  "extended": ["QQQ", "SPY", "IBM", "INTC", "XOM", "CVX"],
  "crypto": ["XRP"]
}
```
La watchlist se lee desde `config.json` — Oscar puede editarla sin tocar código.

---

## CAPA 2: KEYWORDS DE FILTRO PYTHON

### Keywords que SIEMPRE pasan el filtro (prioridad máxima)
```python
CRITICAL_KEYWORDS = [
    # Contratos y gobierno
    "CHIPS Act", "government award", "contract award", "DoD contract",
    "Department of Commerce", "federal contract", "grant", "billion award",
    "million award", "government stake", "equity stake",
    # FDA / Biofarma
    "FDA approval", "FDA approved", "FDA rejected", "PDUFA", "NDA approval",
    "BLA approval", "clinical trial results", "Phase 3",
    # M&A
    "acquisition", "merger", "takeover bid", "buyout", "strategic review",
    # Earnings/Guidance
    "beats estimates", "raises guidance", "guidance raised", "EPS beat",
    "revenue beat", "record revenue",
    # Analistas
    "price target raised", "upgraded to buy", "upgraded to overweight",
    "strong buy", "initiated coverage",
    # Eventos directos
    "Investor Day", "analyst day", "special dividend", "share buyback",
    "partnership announced", "strategic partnership",
]

# Keywords que degradan prioridad (señal débil o ya priceada)
WEAK_KEYWORDS = [
    "rumored", "sources say", "could", "might consider",
    "analyst speculates", "market chatter"
]
```

### Lógica de filtro Python
```python
def passes_filter(article):
    # 1. Ticker en watchlist
    if not any(ticker in article['text'] for ticker in WATCHLIST):
        return False
    # 2. Noticia reciente (< 45 minutos)
    if article['age_minutes'] > 45:
        return False
    # 3. No procesada antes
    if article['id'] in SEEN_IDS:
        return False
    # 4. Al menos 1 keyword crítica
    if not any(kw.lower() in article['text'].lower() for kw in CRITICAL_KEYWORDS):
        return False
    return True
```

---

## CAPA 3: PROMPT DE CLAUDE SONNET

Este prompt es el corazón del sistema. Está diseñado para ser DIRECTO y NO excesivamente cauteloso.

```
SISTEMA: Eres un analista de trading de alta frecuencia. Tu trabajo es evaluar si una noticia es un catalizador accionable AHORA. Sesgas hacia alertar cuando hay duda — perder una oportunidad es peor que una alerta falsa.

NOTICIA A EVALUAR:
Ticker: {ticker}
Fuente: {source}
Publicada: hace {age_minutes} minutos
Precio actual REAL: ${current_price} (precio live AH/pre-market via Alpaca)
Precio cierre anterior: ${prev_close}
Cambio desde cierre: {pct_change:+.1f}%
Momentum últimas velas: {candle_summary}  ← ej: "3 velas verdes consecutivas, vol creciente"
Hora del último trade: {last_trade_time} Lima
Titular: {headline}
Resumen: {summary}

EVALÚA en este orden exacto:

1. ESTADO DEL CATALIZADOR:
   - ¿El evento principal ya ocurrió o está pendiente?
   - ¿El precio ya se movió >5% por esta noticia? (SÍ/NO)
   - Si movió >5% → el catalizador YA ESTÁ PRICEADO → PRIORIDAD: DESCARTADO

2. DIRECCIÓN:
   - LONG o SHORT
   - Razón en 1 línea

3. MAGNITUD ESTIMADA:
   - % potencial de movimiento en las próximas 4-12 horas
   - Basado en tipo de catalizador + sector + capitalización

4. PRIORIDAD:
   - ALTA: catalizador directo + empresa nombrada + no priceado + >8% potencial
   - MEDIA: catalizador real + empresa relacionada OR potencial 3-8%
   - BAJA: señal débil, sector general, o potencial <3%
   - DESCARTADO: ya priceado (precio movió >5%) o noticia vieja

5. OPERATIVA (solo si ALTA o MEDIA):
   - Entrada: rango de precio
   - Stop: precio de cancelación
   - Target: precio objetivo
   - Timing: cuándo actuar (¿ahora en AH? ¿apertura mañana?)
   - Riesgo principal: 1 línea

FORMATO DE RESPUESTA (JSON estricto, sin texto adicional):
{
  "ticker": "XXXX",
  "prioridad": "ALTA|MEDIA|BAJA|DESCARTADO",
  "tipo_catalizador": "fundamental|hype|mixto",
  "direccion": "LONG|SHORT",
  "pct_estimado": 12.5,
  "catalizador_priceado": false,
  "resumen_cataliz": "descripción en 1 línea del catalizador",
  "entrada_rango": "$X.XX - $X.XX",
  "stop": "$X.XX",
  "target": "$X.XX",
  "timing_entrada": "ahora AH | apertura 8:30am Lima | esperar pullback a $X",
  "horizonte_tiempo": "3 días | mismo día | Day+1",
  "salida_fecha": "2026-05-24 3:00pm Lima si no llegó a target",
  "salida_anticipada": "salir si baja de $X.XX (stop) o si hype no confirma en 2h",
  "riesgo": "descripción del riesgo principal",
  "confianza": "ALTA|MEDIA|BAJA"
}
```

---

## FORMATO DEL ARCHIVO DE ALERTAS (alerts.json)

Human-readable JSON Lines — una línea por alerta:

```json
{"timestamp": "2026-05-21T06:56:00-05:00", "ticker": "QBTS", "prioridad": "ALTA", "direccion": "LONG", "pct_estimado": 28.0, "catalizador": "$100M CHIPS Act Award — D-Wave recibe equity stake del gobierno", "entrada_rango": "$19.00 - $20.00", "stop": "$16.50", "target": "$26.00", "timing": "Pre-market apertura 8:30am Lima", "riesgo": "Sector cuántico especulativo — si mercado baja puede no moverse", "fuente": "SEC EDGAR 8-K", "sms_enviado": true, "precio_al_alerta": 19.30, "precio_24h_despues": null}
```

El campo `precio_24h_despues` se rellena automáticamente 24h después para tracking de accuracy.

---

## FORMATO SMS TWILIO

```
🚨 ALTA — QBTS LONG
$100M CHIPS Award confirmado (SEC 8-K)
Precio: $19.30 | No priceado
Entrada: $19.00-20.00
Stop: $16.50 | Target: $26.00
Timing: apertura 8:30am Lima
→ Verificar en eToro ahora
```

SMS de MEDIA:
```
⚠️ MEDIA — NVDA LONG
MS sube PT a $320 (+8% potencial)
Precio: $285 | Mov. día: +1.2%
Entrada: $284-287 | Stop: $272
```

---

## ESTRUCTURA DEL PROYECTO

```
opportunity_alert/
├── CLAUDE.md              ← este archivo
├── main.py                ← loop principal 24/7
├── config.json            ← watchlist, intervalos, API keys paths
├── requirements.txt       ← dependencias Python
├── sources/
│   ├── __init__.py
│   ├── edgar.py           ← SEC EDGAR RSS polling
│   ├── finnhub_news.py    ← Finnhub news API
│   └── reddit_monitor.py  ← Reddit PRAW sentiment
├── filters/
│   ├── __init__.py
│   ├── keyword_filter.py  ← Capa 2: filtro rápido Python
│   └── claude_scorer.py   ← Capa 3: Claude Sonnet scoring
├── alerts/
│   ├── __init__.py
│   ├── twilio_sms.py      ← envío SMS
│   └── alert_logger.py    ← escritura alerts.json
├── utils/
│   ├── __init__.py
│   ├── alpaca_price.py    ← precio real-time AH + velas via Alpaca
│   └── dedup_store.py     ← SQLite para deduplicación
└── data/
    ├── alerts.json        ← log de todas las alertas
    └── seen_ids.db        ← SQLite dedup store
```

---

## config.json (template)

```json
{
  "watchlist": {
    "primary": ["NVDA", "TSM", "QBTS", "IONQ", "RGTI", "QUBT", "PLTR", "APP",
                "AVGO", "AMD", "ASML", "ARM", "RDDT", "CRM", "MSFT", "GOOG",
                "CCJ", "TLN", "BE", "EME", "NOC", "RTX", "LMT", "KTOS", "AVAV",
                "UBER", "SHOP", "MU", "COHR", "IBM", "XOM", "CVX"],
    "extended": ["QQQ", "SPY"],
    "crypto": ["XRP"]
  },
  "intervals_seconds": {
    "edgar": 90,
    "finnhub": 120,
    "reddit": 600
  },
  "max_article_age_minutes": 45,
  "min_pct_for_alta": 8.0,
  "min_pct_for_media": 3.0,
  "already_priced_threshold_pct": 5.0,
  "api_keys": {
    "anthropic_key_env": "ANTHROPIC_API_KEY",
    "finnhub_key_env": "FINNHUB_API_KEY",
    "alpaca_key_env": "ALPACA_API_KEY",
    "alpaca_secret_env": "ALPACA_SECRET_KEY",
    "alpaca_base_url": "https://data.alpaca.markets",
    "alpaca_feed": "iex",
    "twilio_sid_env": "TWILIO_ACCOUNT_SID",
    "twilio_token_env": "TWILIO_AUTH_TOKEN",
    "twilio_from": "+1XXXXXXXXXX",
    "twilio_to": "+51XXXXXXXXXX"
  },
  "claude_model": "claude-sonnet-4-6",
  "send_sms_priorities": ["ALTA", "MEDIA"],
  "log_only_priorities": ["BAJA"]
}
```

---

## LÓGICA DEL LOOP PRINCIPAL (main.py)

```
INICIO:
  Cargar config.json
  Inicializar dedup store (SQLite)
  Inicializar conexiones: EDGAR, Finnhub, Reddit
  Inicializar Twilio client
  Log: "OportunityAlert iniciado — monitoreando X tickers"

LOOP INFINITO:
  Cada 90s  → poll EDGAR RSS → procesar artículos nuevos
  Cada 120s → poll Finnhub   → procesar artículos nuevos
  Cada 600s → poll Reddit    → procesar posts nuevos
  
  Para cada artículo nuevo:
    1. passes_filter(article) → si False: skip
    2. get_realtime_price(ticker) via Alpaca API
       → devuelve: current_price, change_pct, last_trade_time, candles_1m[-10:]
       → si last_trade_time > 10 min de antigüedad → usar precio con advertencia
    3. score_with_claude(article, price_data)
    4. if result.prioridad in ["ALTA", "MEDIA"]:
         log_alert(result)
         send_sms(result)
    5. elif result.prioridad == "BAJA":
         log_alert(result)  # solo archivo, no SMS
    6. mark_as_seen(article.id)
  
  Manejo de errores:
    - API timeout → retry 3x con backoff exponencial
    - Rate limit → esperar y reintentar
    - Fallo Twilio → log error, continuar (no detener el sistema)
    - Fallo Claude → log error, marcar noticia para revisión manual

  Cada 24h → actualizar precio_24h_despues en alertas del día anterior
```

---

## TIPOS DE CATALIZADOR Y SU HORIZONTE DE SALIDA

El sistema reconoce DOS grandes familias de catalizadores. Ambas son válidas y accionables.

---

### FAMILIA 1: CATALIZADORES FUNDAMENTALES
Basados en eventos concretos y verificables. Movimiento más sostenido.

| Tipo | Ejemplos | Horizonte salida | Prioridad max |
|------|----------|-----------------|---------------|
| Gobierno/CHIPS/DoD directo | $100M CHIPS Award a empresa nombrada | 3–7 días | ALTA |
| FDA approval/rejection | Aprobación NDA específica | 1–3 días | ALTA |
| M&A confirmado | Adquisición con precio por acción | Días hasta cierre | ALTA |
| Earnings beat + guidance raise | AH, precio no reaccionó aún | Mismo día / Day+1 | ALTA |
| Upgrade tier-1 analista | MS/GS/JPM sube PT >15% | 2–5 días | MEDIA |
| Contrato importante | Contrato nombrado con monto | 2–5 días | MEDIA |
| Insider buying masivo | CEO compra >$1M en acciones propias | 3–10 días | MEDIA |

---

### FAMILIA 2: CATALIZADORES DE HYPE/SENTIMIENTO
Basados en momentum social, retail FOMO o narrativa viral. Movimiento rápido pero más frágil.
**Son catalizadores reales — ignorarlos es perder oportunidades como QBTS enero 2025 (+800%) o cualquier squeeze de WSB.**

| Tipo | Señal detectable | Horizonte salida | Prioridad max |
|------|-----------------|-----------------|---------------|
| Reddit squeeze emergente | WSB menciones x3 en <2h + squeeze_alert | **Mismo día / 24h** | ALTA si RSI<60 |
| Google Trends spike | Interés +100% vs semana anterior en <3h | **Mismo día / 48h** | MEDIA |
| Sector hype gubernamental | Presidente/funcionario menciona sector | **1–3 días** | ALTA si ticker directo |
| Meme stock revival | Stock tendencia + volumen x5 normal pre-market | **Horas** | MEDIA |
| Hype post-Investor Day | Bookings/guidance sorprenden, comunidad viral | **2–5 días** | MEDIA |
| News volume spike | 5+ artículos en <1h de distintas fuentes | **Horas a 1 día** | MEDIA |

#### Cómo detectar hype en el código:
```python
def detect_hype_signals(ticker, reddit_client, trends_client, alpaca_client):
    signals = []

    # 1. Reddit: spike de menciones en últimas 2h vs promedio
    reddit_data = reddit_client.get_mentions(ticker, hours=2)
    if reddit_data['mentions'] > reddit_data['avg_hourly'] * 3:
        signals.append({
            'type': 'reddit_spike',
            'strength': round(reddit_data['mentions'] / reddit_data['avg_hourly'], 1),
            'squeeze_alert': reddit_data['squeeze_alert']
        })

    # 2. Google Trends: cambio rápido de interés
    trends = trends_client.get_interest(ticker)
    if trends['change_pct'] > 100 and trends['current_interest'] > 60:
        signals.append({'type': 'trends_spike', 'pct': trends['change_pct']})

    # 3. Volumen pre-market anormal via Alpaca
    volume = alpaca_client.get_premarket_volume(ticker)
    if volume['ratio_vs_20d_avg'] > 3.0:
        signals.append({'type': 'volume_spike', 'ratio': volume['ratio_vs_20d_avg']})

    return signals
```

#### Reglas para clasificar hype:
- **Hype puro (sin fundamental):** horizonte = mismo día / 24h máximo. Stop ajustado (-5%). Monto máximo 10% capital.
- **Hype + fundamental (ej: $100M award + momentum social):** horizonte = 3–7 días. Stop normal. Puede ser ALTA.
- **Sector hype sin ticker directo de watchlist:** no alertar. Demasiado difuso.
- **El hype es frágil:** si precio no sube en primeras 2h → Claude debe indicar salida.

---

## HORIZONTE DE SALIDA — OBLIGATORIO EN CADA ALERTA

**Toda alerta debe incluir un horizonte de salida explícito en tiempo Y en precio.**
Sin esto, Oscar no sabe cuándo salir y puede quedarse dentro de un trade que revirtió.

### Tabla de horizontes por tipo de catalizador:

| Catalizador | Horizonte tiempo | Salida por precio | Salida por tiempo |
|-------------|-----------------|-------------------|-------------------|
| Gobierno directo (CHIPS/DoD) | 3–7 días | Target: +20–35% | Salir Day+5 si no llegó |
| Reddit squeeze | Mismo día / 24h | Target: +15–30% | Salir al cierre del día |
| FDA approval | 1–2 días | Target: +20–40% | Salir Day+2 si no llegó |
| Earnings AH beat | Overnight / Day+1 | Target: +8–15% | Salir apertura Day+1 si no sube |
| Upgrade analista | 2–5 días | Target: +8–15% | Salir Day+4 si no llegó |
| Google Trends spike | Horas / 48h | Target: +10–20% | Salir siguiente jornada si no confirma |
| M&A confirmado | Semanas | Target: precio oferta | Mantener hasta cierre o ruptura |
| Meme/volumen spike | Horas | Target: +15–25% | Salir mismo día antes del cierre |

### Formato obligatorio en el JSON de alerta:
```json
{
  "horizonte_tiempo": "2 días / salir Day+2",
  "salida_precio": "$26.00",
  "salida_tiempo": "2026-05-23 3:00pm Lima si no llegó a target",
  "salida_anticipada": "salir si precio baja de $21.00 (stop)"
}
```

### En el SMS, el horizonte va siempre al final:
```
🚨 ALTA — QBTS LONG
$100M CHIPS Award (SEC 8-K — hace 2 min)
Entrada: $19–20 | Stop: $16.50 | Target: $26
⏱ Salir: Day+5 o $26 lo que llegue primero
```

---

## CÓMO DETECTAR SI EL CATALIZADOR ESTÁ PRICEADO

### Señales de que YA ESTÁ PRICEADO (degradar a DESCARTADO):
1. `pct_change` actual vs close anterior > 5% (verificado via Alpaca AH)
2. Noticia publicada hace >45 minutos y precio ya reaccionó
3. Titular dice "stock soars", "stock jumps", "surges" → subida ya ocurrió
4. Mismo evento cubierto por 5+ fuentes distintas → noticia masiva, todos ya saben

### Señales de RUIDO (filtrar en Capa 2, nunca llegan a Claude):
1. Sin empresa específica de watchlist nombrada
2. Artículo de opinión sin catalizador nuevo
3. Rehash de noticias de hace >2 días
4. "Sources say", "rumored", "could consider" sin confirmación
5. Hype sin volumen o menciones que lo respalden

---

## SESGO HACIA ALERTAR (anti-exceso-de-cautela)

Regla fija del sistema: **cuando Claude tenga duda entre MEDIA y BAJA → usar MEDIA.**

Justificación: Oscar prefiere recibir 5 alertas mediocres que perderse 1 oportunidad real como QBTS (+33%). El costo de un SMS innecesario es $0.01. El costo de perder una entrada como QBTS May 21 2026 fue ~$1,000+ de P&L potencial.

El sistema NO es el tomador de decisión final — Oscar lo es. El sistema es el detector y alertador. Oscar decide si entra o no.

---

## COMANDO DE INICIO

Cuando Oscar escribe **"empezar"** en una sesión nueva:

1. Crear toda la estructura de carpetas y archivos
2. Instalar dependencias (`requirements.txt`)
3. Crear `config.json` con template y pedir a Oscar que complete:
   - Número de teléfono Twilio (from y to)
   - Alpaca API key + secret (cuenta gratuita en alpaca.markets → Paper Trading keys)
   - Confirmar Finnhub API key (ya tiene en ~/trading/api_credentials.json)
4. Crear todos los módulos Python con lógica completa
5. Crear script de test: `python test_alert.py` que simula 1 noticia y verifica SMS
6. Crear `run.sh` para Oracle VM con `nohup python main.py &`
7. Mostrar instrucciones de deployment en la VM

---

## NOTAS PARA EL DEVELOPER CLAUDE

- Los paths de credenciales son relativos a la VM de Oracle, no al Windows de Oscar
- En la VM: los archivos de `~/trading/` deben estar disponibles (o replicar config relevante)
- La VM ya tiene Python instalado según Oscar
- Prioridad de desarrollo: EDGAR → Filtro Python → Claude scorer → Twilio → Reddit (en ese orden)
- Testear primero con QBTS como caso de prueba: el 8-K del 21-mayo-2026 debe generar ALTA
- El sistema debe correr con `nohup` o como `systemd service` para sobrevivir desconexiones SSH
- Logging a archivo + stdout para debuggear desde la VM
- NO usar asyncio en v1.0 — usar threading simple para mantener el código legible
- Código comentado en español para que Oscar pueda leerlo

---

## FLUJO COMPLETO — DE LA ALERTA A LA DECISIÓN

```
OportunityAlert detecta catalizador
         ↓
Escribe en ~/opportunity_alert/data/alerts.json
         ↓
Envía SMS Twilio a Oscar:
  "🚨 ALTA — QBTS LONG | $100M CHIPS Award | Entrada $19–20 | Stop $16.50"
         ↓
Oscar abre Claude Code (proyecto trading) y escribe:
  "alerta: analiza QBTS"
         ↓
Claude lee alerts.json para contexto completo
Claude verifica precio actual (Alpaca o pregunta a Oscar)
Claude da decisión en <60 segundos:
  ✅ ENTRAR / ❌ NO ENTRAR / ⏳ ESPERAR
         ↓
Oscar ejecuta manualmente en eToro
```

**El SMS debe incluir siempre** el ticker, dirección, catalizador en 1 línea,
rango de entrada y stop — para que Oscar tenga el contexto mínimo antes de
abrir Claude. Si Claude no está disponible en ese momento, Oscar puede actuar
solo con el SMS.

---

## HISTORIAL DE OPORTUNIDADES PERDIDAS (contexto del problema)

Este sistema existe para evitar repetir estos casos:

| Fecha | Ticker | Catalizador | Ventana perdida | P&L potencial |
|-------|--------|-------------|-----------------|---------------|
| 2026-05-21 | QBTS | $100M CHIPS Award (8-K SEC 1:54am Lima) | 3 horas | +33% en apertura |
| 2026-05-21 | IONQ | Mismo programa gobierno $2B | 3 horas | +27% |
| 2026-05-21 | ARM  | Ola de upgrades (Bernstein+RBC+Jefferies) | 2 días | +36% |

Estos tres casos ocurrieron la misma semana. El sistema habría capturado los 3.

---

*Sistema diseñado por Oscar Navarro + Claude Code — v1.0 2026-05-21*
*Proyecto: OportunityAlert — alertas de trading en tiempo real*
