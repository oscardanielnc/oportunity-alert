# 📍 ESTADO ACTUAL DEL SISTEMA — léeme para continuar
# Última actualización: 2026-06-21 (TARDE) — Earnings (Marea/PED) desplegado + horizonte por-señal +
#   digest-notify semanal + NUEVO tool LOCAL earnings_shape (playbooks intradía, NO va a la VM).
# Dueño: Oscar Navarro | Asistente: Claude

---

## ✅ CIERRE 2026-06-21 — SECCIÓN EARNINGS (separada del Piloto) + PRE-RUN-UP VALIDADO + TRACKING

> Fuente de verdad viva del brazo Earnings: memoria `ped-earnings-arm`. Todo DESPLEGADO en la VM.

### 🧩 Reestructura: Piloto = solo Marea · Earnings = sección propia
- **Piloto** quedó **solo Marea** (momentum swing): PED removido del runner, K=8 completo
  (`pilot/run_pilot.py`, `pilot/momentum_signals.py` recibe `days_held`). `pilot/ped_signals.py`
  se mantiene como librería de math-de-señal (la reusan `earnings/` y `utils/position_strategy`).
- **Earnings = módulo nuevo `earnings/`** (NO simula trades — genera candidatos/señales y decide
  Oscar): `calendar.py` (Finnhub a futuro), `strategies.py` (registro extensible), `run_earnings.py`
  (runner diario → `data/earnings_dashboard.json` + scoreboard). Endpoint `GET /api/earnings` + tab
  "📅 Earnings". **Cron en la VM**: `15 22 * * 1-5 ... -m earnings.run_earnings`.

### 🔬 Veredictos de estrategias de earnings (disciplina: causal + neto fees + OOS)
- **PED** ✅ única validada de POST (drift, mega-cap, reacción ≥+5%, Day+2→~Day+7).
- **Short-PED** ❌ y **Cañonazo** ❌ (el "pase" del cañonazo era LOOKAHEAD; corregido por gap en
  `research/backtest_canon_gap.py` → sin edge). NO recablear.
- **Pre-run-up** ✅ (PRE) validado en `research/backtest_prerun.py` (n=589, EXCESS vs QQQ, OOS):
  entrar ~5 ruedas antes del reporte SI sobre SMA50, salir último cierre pre-reporte. win 56%,
  excess +0.64%/raw +1.3% mediana a fee 0.3% RT. Modesto y sensible a fees → solo mega-caps líquidos.

### 🎯 Cableado del pre-run-up (sobre el calendario, sin duplicar)
- `earnings/strategies.py`: `prerun_for()` (filtro SMA50) + `PRERUN_PROJ`. El calendario de la sección
  anota cada earning próximo con recomendación (upcoming/enter_now/holding/skip), día de entrada y
  proyección. PED queda en el bloque "candidatos reactivos (POST)".

### 📈 Tracking en Scoreboard (el día que se activa la entrada)
- PED → t0 = próxima sesión (Day+2 open); pre-run-up → t0 = día de apertura de ventana. arm='earnings',
  cat='ped'/'prerun'. **Limitación conocida**: el scoreboard cierra a 48h (PED aguanta ~7d, prerun ~5d)
  → lectura PARCIAL del drift temprano (NO contamina el evento: 48h no llega al reporte). Refinamiento
  futuro: horizonte por-señal (medir hasta el exit real).

### 🐛 Bug arreglado + UI
- `utils/scoreboard.py`: el resolver crasheaba al mezclar ts naive (market_movers) y aware (noticias)
  → `_parse_ts` + normalización en `record_signal` (commit `4a89e1a`).
- Dashboard: badge vistoso de fuente en Market Movers (Trump/Fed/Treasury/SEC con color) + 1 emoji por
  tab en el sidebar (📰 Noticias · 💼 Posiciones · 👁 Watchlist).

### ✅ AÑADIDO LA MISMA TARDE (desplegado VM, commits hasta `c454edb`)
- **Horizonte por-señal en el Scoreboard** (HECHO): `utils/scoreboard.py` columnas `horizon_h`+`abn_final`;
  resolve_open mide hasta el exit real por-fila (PED ~202h, prerun=entrada→víspera reporte), migración
  idempotente. `run_earnings` pasa los horizontes. Panel dashboard + digest con columna "exit".
- **#2 base lista**: `scoreboard.expected_impact()` (impacto empírico por categoría; falta cablear).
- **Recordatorio automático**: `research/scoreboard_digest.py --notify` + **cron VM** `30 23 * * 0`
  (domingo) → WhatsApp con estado del scoreboard + marca "#2 listo (n≥8)". `PENDIENTES.md` documenta #2/#3.

### 🧪 NUEVO — `research/earnings_shape.py` (LOCAL, NO va a la VM)
Herramienta de research del proyecto **earnings-shape** (memoria `earnings-shape-project`): perfila
por-ticker la forma intradía de reacción a earnings (1m [-1h,+2h] + 1h post-días, % normalizado),
repetibilidad, clasificador por gap, doble clasificación (resultado/forma-AH) con head-fakes, y un
**HTML local interactivo** (`data/earnings_shape.html`) con grupos personalizados (localStorage),
cards con EPS auto + Rev/Guidance manuales. Hallazgos: MU/PLTR = playbooks fuertes; APP/AVAV no (forma).
**Corre SOLO en local** (como los demás `research/*.py`); no se importa en main.py ni tiene cron.
Revenue surprise/guidance NO disponibles gratis (verificado) → manuales.

### 🔜 PENDIENTE
- Refinar convicción de earnings → cablear `expected_impact` cuando el scoreboard acumule muestra (#2).
- Lectura del scoreboard (#3) cuando cierren señales (el cron semanal ya avisa).
- earnings-shape v2: ampliar biblioteca de tickers; fuente paga si se quiere auto revenue/guidance.
- Más estrategias tradeables solo si pasan el bar (Short-PED/Cañonazo descartados).

---

## ✅ CIERRE 2026-06-20 (NOCHE) — REDISEÑO NOTICIAS + SCOREBOARD + MARKET MOVERS (DESPLEGADO, commit `85991de`)

> Sesión enorme. Fuente de verdad del rediseño: `RESTRUCTURE_3ARMS.md` + memorias
> `news-reaction-findings`, `news-reaction-measurement`. **TODO desplegado y vivo en la VM**
> (servicio active, 15 threads, 0 errores). **REGLA: no desplegar sin que Oscar lo pida** (memoria
> `feedback-no-deploy-sin-pedir`).

### 🔬 Estudio de reacción a noticias (qué descubrimos, con datos)
- El **score de convicción NO predice** la reacción (dir_ok ~50%). Llegábamos **TARDE a los saltos
  ≥12%**: 88% perdidos, lead negativo. Problema = **cobertura+velocidad+filtro**, no el modelo.
- Autopsia: los catalizadores reales llegaban en tiempo real (age 0) por ALPACA_BENZINGA y el
  **filtro de keywords los mataba**. NO hace falta pagar feed: el fix era el filtro (ya hecho).
- **Momentum ⛔ ARCHIVADO**: el edge no sobrevive muestra grande + fees (ver `momentum_validate2`).
- **Earnings = la prioridad tradeable** (DELL+40/SNOW+38 son earnings; PED del Piloto ya existe). → MAÑANA.

### 🛠 Brazo NOTICIAS rediseñado (en vivo)
- `filters/keyword_filter.py`: kill 8-K Item 5.07; degrade PT-maintains; + keywords cubeta-2
  (data center lease, prospectus/offering, MOU). Yahoo/CNBC fuera del ingreso Finnhub (laggy).
- `utils/signal_score.py` cableado: SMS por **categoría+frescura+confirmación de precio** (no el
  score viejo). Config `min_signal_score_sms`=6. La dirección solo es predecible en fresco+no-priceado.

### 📈 SCOREBOARD de auditoría (en vivo) — el validador central
- `utils/scoreboard.py` + tabla `signal_outcomes` + thread resolver horario + `/api/scoreboard` +
  pestaña dashboard. Mide TODA señal (noticias/market_movers) a **48h**: anormal vs QQQ + MFE/MAE +
  acierto. **Precio DUAL Binance-perp(24/7)/Alpaca(next-open)**. `scope=macro` → mide CRUDO + índice.
  `event_key` agrupa la cesta (1 señal POR ticker). Casos reales validados: FOMC 17-jun, Trump→INTC +5.5%.

### 📢 MARKET MOVERS — sección por VOZ (en vivo), separada de Noticias (por empresa)
- `sources/truth_social.py` (RSS trumpstruth.org), `sources/fed.py` (RSS FOMC/press), `sources/sec.py`
  (RSS press/speeches), `sources/treasury.py` (scrape HTML, frágil — Treasury no tiene RSS).
- `utils/trump_tracker.py` generalizado: `OFFICIAL_SOURCES={FED,TREASURY,SEC}` pasan el gate por
  fuente; prompt source-aware; tickers sanitizados; hook scoreboard arm='market_movers' + scope + event_key.
- Threads: `TruthSocial`, `Fed`, `OfficialMovers`(SEC+Treasury). Dashboard: "📢 Market Movers" con fuente etiquetada.
- **Brief diario** consume market movers (bloque que prioriza bajista/mixto+macro) y **se refresca al
  instante** ante un evento macro/bajista → avisa de cerrar antes de una caída.

### 📊 LECTURA SEMANAL (para el veredicto de afinado)
`research/scoreboard_digest.py`: traer la DB (`scp .../data/metrics.db data/metrics_vm.db`) y correr
`python research/scoreboard_digest.py --db data/metrics_vm.db --days 7` → win-rate/abn/MFE por
brazo+categoría+fuente + candidatos a suprimir + cesta por event_key.

### 🔜 PENDIENTE (mañana)
- **Earnings (PED)** — el brazo tradeable; revisar/validar el motor del Piloto + ejecución.
- Refinar: panel scoreboard por fuente; medir lead-time de Fed/SEC vs wires (con datos forward).
- Treasury scrape es frágil (sin RSS) — vigilar; SEC/Fed son RSS sólidos.

---

## ✅ CIERRE 2026-06-19 (NOCHE) — MIGRACIÓN A DEEPSEEK + DEDUP TRUMP + WHATSAPP DEL BRIEF (commit `5948d42`, DESPLEGADO Y VERIFICADO EN VM)

> **Contexto:** Oscar reportó dos quemadores de créditos y pidió migrar todo el sistema fuera de
> Claude/Gemini a un modelo fuerte y barato. Todo DESPLEGADO en la VM y verificado en vivo el mismo
> día (commit `5948d42` en origin/main; service `opportunity-alert` active, ping de arranque OK).

### 🤖 MIGRACIÓN A DEEPSEEK — dos tiers, motor centralizado
- **`utils/ai_client.py`:** nuevo `resolve_engine_model(tier)` = punto único de verdad del motor/modelo.
  Motores `deepseek` y `glm` por REST compatible-OpenAI (con `requests`, **sin deps nuevas**). Default
  del sistema ahora **deepseek** (antes gemini/claude).
- **Dos tiers (verificados en vivo con key real):**
  - **FUERTE** = `deepseek-v4-pro` (razonador) → scoring de noticias / decisiones financieras. `claude_scorer`
    subió `max_tokens` 768→2048 (el "pensamiento" consume tokens antes del JSON; con poco margen sale vacío).
  - **BARATO** = `deepseek-chat` (no-pensante) → Brief diario + radar Trump. Más rápido/barato y sin riesgo
    de truncar. ⚠️ `deepseek-chat` se **depreca 2026-07-24** → migrar a su sucesor no-pensante (override `AI_MODEL_CHEAP`).
- **Por qué DeepSeek:** índice AA V4-Pro 52 > Claude Sonnet 47, a $0.44/$0.87 (~1/17 del output de Sonnet).
  Flash/chat para tareas simples a $0.14/$0.28. GLM-5.1 quedó cableado como alternativa (`AI_ENGINE=glm`).
- **Migrados:** `filters/claude_scorer.py` (strong), `utils/daily_brief.py` y `utils/trump_tracker.py` (cheap);
  `main.py` arranque exige `DEEPSEEK_API_KEY` si engine=deepseek. Overrides: `DAILY_BRIEF_*`, `TRUMP_*`.

### 🔇 FIX — radar Trump repetía la MISMA noticia 5× (INTC) gastando 5 llamadas IA
- `utils/trump_tracker.py`: dedup ANTES era por **texto literal** del titular → el mismo evento llegado por
  5 fuentes con redacciones distintas pasaba como 5 eventos. Ahora dedup **SEMÁNTICA** por `(tickers + tema
  macro)` en ventana `TRUMP_DEDUP_HOURS=6h`, usando los **tickers estructurales del feed** (`tickers_found`/
  `symbols`) → "Intel"→INTC se unifica. Se sella en CAPTURA (frena la ráfaga antes de la IA). Fix de TZ naive/aware.

### 📲 FEATURE — WhatsApp del Brief solo si cambió
- `utils/daily_brief.py`: al regenerar, manda WhatsApp por `send_raw_message` (CallMeBot→Twilio a `TWILIO_TO`)
  **solo si `resumen+atención` difiere del cacheado** (decisión de Oscar: evitar spam de briefs idénticos).

### ✅ VERIFICADO EN VIVO (19-jun noche Lima)
- Key real probada: tier fuerte (`deepseek-v4-pro`, 6.9s) y barato (`deepseek-chat`, 2.6s) devuelven JSON
  correcto; `score_with_claude` end-to-end dio `RKLB LONG/ALTA`; brief end-to-end OK.
- VM: `bash deploy.sh` exitoso, service active, 10 threads arriba, ping WhatsApp de arranque enviado
  (CallMeBot). Que arrancara sin morir confirma `AI_ENGINE=deepseek` + `DEEPSEEK_API_KEY` presentes en
  `/etc/opportunity-alert.env`. Oscar verificó en vivo OK.

### 🔜 PENDIENTES de esta tanda
- **MAÑANA (2026-06-20): feature grande — registro/simulación de trades por noticia.** Por cada predicción
  de la IA (dirección + magnitud + horizonte) "ejecutar" un trade simulado con precios reales de Alpaca y
  puntuar P&L vs realidad → medir empíricamente qué modelo acierta más. (El benchmark elige el tier; el
  scoreboard de trades simulados elige al ganador real.) Ver memoria `ai-engine-deepseek`.
- **Rotar la `DEEPSEEK_API_KEY`** (Oscar dijo que cambiaría la key compartida en chat): actualizar en
  `/etc/opportunity-alert.env` + `sudo systemctl restart opportunity-alert`.
- **Cosmético:** los logs de ciclos de fuentes en `main.py` aún dicen `→ Claude` (texto hardcodeado); el
  motor real es DeepSeek. Cambiar a `→ IA` cuando se quiera.

---

## ✅ CIERRE 2026-06-10 — AUDITORÍA COMPLETA + 3 PAQUETES DESPLEGADOS Y VERIFICADOS EN LA VM

> **Contexto:** Oscar reportó pérdidas por el crash de la semana (CPI caliente, Irán, AVGO/SMCI).
> Queja central: "las validaciones de salida de Marea son muy rígidas, siempre me sugieren quedarme
> aun cuando todo se hunde; no tenemos buenos planes para salir a tiempo". Pidió una "super alerta
> macro" de caída global. Se hizo auditoría completa del sistema (3 agentes en paralelo) + 3 paquetes
> de mejoras. **TODO DESPLEGADO EN LA VM y verificado en vivo el mismo día** (commits `38b36c4`,
> `6b132bd`, `18027ce` en origin/main; service `opportunity-alert` active).

### 🔍 Diagnóstico raíz (por qué el crash pasó sin un solo SMS)
1. **Brazo de noticias 100% alcista:** la única keyword bajista era "FDA REJECTED". Log 24h del crash =
   326 noticias → 322 filtradas → 3 a IA (todas alcistas) → 0 SMS. "Why Is MU Falling" / "$7B equity
   raise" / "CPI rises most in 37 months" = score 0.
2. **Salida Marea tardaba 28-72h:** el tracker comparaba el cierre de AYER toda la sesión + sin SMS de
   régimen + sin aviso intradía. Chandelier NO-ratchet (se afloja en pánico — esto es CORRECTO, validado).
3. **Sin detección macro/market-wide:** todo era per-ticker.

### 🟢 PAQUETE A — alerta macro + ojo bajista + aviso intradía (commit `38b36c4`)
- **`utils/market_sentinel.py` (NUEVO) + thread `MacroSentinel`:** la "super alerta" por PRECIO
  (QQQ/SPY intradía, QQQ ret_5d, breadth del universo sobre EMA20, posiciones a <5% del stop), 1 SMS/día.
  + SMS al CAMBIO de régimen (SMA200 ±3%). Umbrales en la zona VALIDADA: ret5d −3% / breadth 35.
- **Keyword filter:** ~50 keywords bajistas Tier-1/2 (misses, guidance cuts, downgrades, offerings,
  dilución, investigaciones, quiebras) + bypass de posición (noticia bajista de un held SIEMPRE va a IA).
- **Aviso INTRADÍA de chandelier:** el tracker compara precio eToro en vivo vs stop → SMS "cruzó el
  chandelier, la regla confirma al cierre; eToro 24/5 te deja salir ya". + recordatorio "eToro reabre
  domingo" en señales de viernes.
- **Gap scanner −4% para posiciones** (antes 12-30%); explain-move lee titulares filtrados.
- **Bugs rojos:** candles_1m, hh día 0, WS encola (no bloquea), stop/target invertidos, mute 12h, timestamps.

### 📊 PAQUETE B — backtest de salidas (commit `6b132bd`, `research/backtest_marea_killswitch.py`)
2020-2026, 79 tickers. Veredictos: RATCHET ❌ (Sharpe 1.18, MaxDD −27.2 — vende el shakeout),
TIGHTEN ❌, SECTOR CAP ❌, DERISK-HALF ❌. **Único ganador: DERISK-ALL rápido −3%/35** (Sharpe 1.69,
MaxDD −14.2, DD2022 −2.8) PERO **4 de 5 activaciones son whipsaw** (se recompra más caro) y **NO aporta
sobre K=8 ya live**. **DECISIÓN OSCAR (acordada): el kill-switch queda como ALERTA informativa
(MacroSentinel), NO ejecución automática.** Memoria: `study_marea_killswitch`.

### 🔧 PAQUETE C — contabilidad auditable + robustez (commit `18027ce`)
- **Trades auditables:** open_datetime real de eToro (hold_hours correcto) + columnas strategy/exit_reason
  (migración ALTER TABLE idempotente, ya aplicada en la VM) + `get_pnl_by_strategy()` + sección
  [ESTRATEGIA] en `show_metrics.py`. Guard 2-ciclos anti-cierres-fantasma.
- **Robustez:** Twilio retry 3×; weekend digest no se autodestruye; WS ping/keepalive; watchdog de threads
  colgados en heartbeat; purga SQLite diaria; re-aviso token 24h; worker DelayedAlerts blindado;
  throttle refresh_daily_summary; WARNING si DASHBOARD_PASS vacía.

### ✅ VERIFICADO EN VIVO (10-jun, tarde-noche Lima)
- **Estreno del MacroSentinel: DISPARÓ a los 34s del arranque** — `[Sentinel] ALERTA MACRO enviada:
  ['QQQ -2.3% hoy', 'SPY -1.8% hoy', 'QQQ -7.1% en 5 días', 'solo 32% del universo sobre su EMA20']`
  (SID SMf3611d19...). Exactamente el escenario para el que se construyó.
- 10 threads vivos (incl. MacroSentinel); `market_sentinel_state.json` = risk_on; account_cache fresco;
  migración trades.strategy/exit_reason confirmada en la DB.
- **DASHBOARD_PASS configurado** en `/etc/opportunity-alert.env` (EnvironmentFile del service) — dashboard
  ya pide login (user `oscar`). Cierra la exposición del puerto 8081.

### 📌 NOTAS OPERATIVAS DE LA VM (para retomar)
- Service real: **`opportunity-alert`** (guion, doble 'p'). Path: `/home/opc/oportunity-alert` (una 'p').
- EnvironmentFile: **`/etc/opportunity-alert.env`** (owner root → editar con `sudo tee -a`; systemd lo
  relee solo al `restart`, no al reload).
- Log directo: `/home/opc/oportunity-alert/opportunity_alert.log` (los ciclos OK de sentinel/tracker son
  DEBUG → no salen al journal; grepear el log de archivo, no `journalctl`).
- Dashboard: `http://213.35.121.9:8081` (ahora con Basic Auth; es HTTP plano — para blindaje total,
  futuro: bloquear puerto en Security List de Oracle + túnel SSH).

### 🔜 PENDIENTES (ninguno urgente)
- En ~2-4 semanas: `python show_metrics.py --days 30` → P&L por estrategia con datos reales (la columna
  strategy se llena desde este deploy). Evaluar qué brazo gana / si las salidas discrecionales suman.
- Mañana con mercado abierto: ver en vivo avisos intradía de chandelier + noticias bajistas sobre posiciones.
- Blindaje opcional del dashboard (firewall + túnel SSH) cuando Oscar quiera.

---


## 🌙 CIERRE 2026-06-02 (NOCHE) — research de estrategias nuevas + visibilidad PED (commits en `main`, SIN PUSH)

> ⚠️ Estilo: Oscar quiere respuestas CONCISAS (ver memoria `feedback_concise_output`).

### 🔬 4 ideas probadas — las 4 DESCARTADAS con datos (cero plata arriesgada):
| Idea | Resultado | Por qué se cayó |
|---|---|---|
| Short del fade post-IPO | ❌ | Edge era artefacto de la burbuja 2021 (OOS 2015-19 no aguanta) |
| Gap pre-market → ¿continúa? | ❌ | Se desinfla; MRVL/QBTS eran sesgo de selección |
| Short-PED (espejo bajista) | ❌ | Cola de squeeze mata la media |
| Cañonazo bajista intradía | ❌ | **Look-ahead** (filtró por cierre, entra en open). Por gap real = 53% win |

Scripts en `research/ipo_*.py`, `gap_continuation.py`, `backtest_short_ped.py`, `backtest_earnings_intraday.py`, `backtest_cannon_stress.py`. Memorias: `study_ipo_postipo_fade`, `study_gap_continuation`, `study_cannon_lookahead`.
**Lección permanente:** validar OOS en régimen calmo + condicionar SIEMPRE por info disponible en la ENTRADA. **Núcleo sigue siendo PED + Marea.**

### ✅ Construido y desplegable: panel "Estrategias disponibles" (commit `48d6639`)
PED era invisible (sin panel, sin slot, solo badge tras comprar). Ahora:
- `run_pilot` expone TODOS los candidatos PED del día (`pending.ped_candidates`) + ⭐ si reacción ≥+10%.
- Dashboard: sección **"📅 Estrategias disponibles"** (genérica para futuras), muestra 🎯 comprar (slot libre) vs 👁 señal (sin slot).
- Cero lógica de trading tocada. Verificado: compila, pilot corre, json emite `strategy_signals`.

### 🚀 PARA SUBIR A LA VM (pendiente de Oscar): push + `deploy.sh` + `venv/bin/python -m pilot.run_pilot`
La mañana (MRVL: 3 huecos + cross-arm + badges) ya está en origin/deployada. **Pendiente = solo los 3 commits de la NOCHE** (panel PED + research + este doc). Tras deploy, correr el pilot repobla el JSON con `strategy_signals`. Validar en VM: panel "Estrategias disponibles" + que un gap/priceado real dispare SMS.

---


## ✅ CIERRE SESIÓN 2026-06-02 (TARDE) — caso MRVL RESUELTO + UX de badges (commits en main, SIN PUSH)

> **Origen:** la PRIORIDAD #1 (MRVL voló ~20-29% por endorsement de Jensen Huang en Computex y
> NO llegó alerta). Analizado el log REAL de la VM (`data/noticias_log_2026-06-02 (1).csv`, descargado
> por Oscar). Diagnóstico afinado: **NO fue que no se capturó** — el titular llegó fresco (Benzinga age 0)
> Y llegó a la IA, pero murió por 3 huecos. Resueltos los 3 en esta sesión.

**Los 3 huecos (RESUELTOS):**
1. **Keyword filter ciego al endorsement** (`filters/keyword_filter.py`): los titulares "next trillion-dollar
   company / endorses / crowns" daban **score 0** → filtrados antes de la IA. Agregados a **Tier-1**:
   `TRILLION-DOLLAR COMPANY/GIANT`, `NEXT TRILLION`, `$1 TRILLION CLUB`, `ENDORSES`, `CALLS IT THE NEXT`.
   Verificado: los 3 titulares de MRVL ahora puntúan 8-12 (pasaban con 0).
2. **Supresión de priceados** (`main.py`): un catalizador FUERTE+fresco ya priceado iba SOLO al dashboard
   (sin SMS). Ahora si es Tier-1/earnings/FDA + `score_ia>=min_score_priced_sms(5)` → **SMS de continuación**
   con prefijo `[🔥 YA PRICEADO (+X%) — NO persigas el pop; evaluá para TENDENCIA]`, dedup 1/ticker/día.
3. **Catalizador nocturno/Asia** (`main.py`, NUEVO thread `gap_scanner_loop`): señal **por PRECIO, no por
   noticia**. Barre la watchlist cada 15 min **siempre que eToro esté abierto (24/5)** — cubre la madrugada
   (la acción US ya se mueve en el quote 24/5 aunque el titular lague). Si |change%| supera el umbral de
   "priceado" (piso 12%) y no hubo alerta de noticia/gap ese día → SMS `📈 GAP fuerte SIN alerta de noticia`.

**Cross-arm Marea (el APRENDIZAJE de fondo):** los dos brazos operaban en silos. `utils/news_context.py` →
`marea_leader_tag(ticker)` lee los `leaders` del dashboard; cableado en el SMS de priceado y en el de gap.
Si el ticker ya es **líder top-K ⭐/breakout**, el SMS antepone `🌊 LÍDER MAREA #N (⭐ TOP + breakout, sector
#k/n, ~X% sug.)`. (Solo dispara para top-K reales; un breakout de radar NO recibe el tag.)

**UX de badges — colisión del 🆕 RESUELTA** (`api/dashboard.html`): el emoji 🆕 significaba DOS cosas opuestas:
en una fila del **Top-10** = "gate de entrada abierto, COMPRA validada"; en el panel **"Breakouts nuevos"** =
"radar de fuerza 6m baja, NO validado". Confundía (caso STX⭐🆕breakout vs MRVL🆕). Fix: badge del líder
top-10 ahora **`✅ breakout` verde** (clase `.pl-badge.entry`) + tooltip; el panel mantiene `🆕 NEW` índigo
pero con tooltip que lo marca RADAR. Cero cambio de lógica, solo presentación.

**Marco de decisión (cómo leer "¿comprar?") — aclarado con Oscar:**
- **Top-10 líder con `✅ breakout`** = compra Marea VALIDADA (entra al open, vol-size %, chandelier 4×ATR).
- **Panel "Breakouts nuevos" 🆕 (aun con ⭐)** = RADAR, no validado (Estudio B: peor riesgo-ajustado, revierte
  en bear). Discrecional: chico + stop 3×ATR. **MRVL cayó acá → baja prioridad, NO era compra Marea.**
- **"Ya priceado"** es concepto del brazo de NOTICIAS (no persigas el pop de hoy), NO de Marea (la tendencia
  sigue; el chandelier la gestiona). No descalifica una entrada de tendencia válida.

**Config nueva** (`config.json`): `min_score_priced_sms:5`, `gap_scan_seconds:900`, `gap_alert_threshold_pct:12`.

**Estado:** todo compila (py_compile OK), verificado con datos reales (keyword scoring, marea_leader_tag).
Commits en `main` SIN push. **FALTA: Oscar pushea + `deploy.sh` + correr `venv/bin/python -m pilot.run_pilot`
en la VM.** Validar en vivo: que un próximo gap/priceado fuerte dispare SMS y que los badges se vean bien.
⚠️ OJO: el `pilot_dashboard.json` LOCAL puede no reflejar la VM (local = solo frontend). La vista en vivo manda.

## 🚨 CIERRE SESIÓN 2026-06-02 — investigación a fondo + cambios live (TODO EN main, SIN PUSH — Oscar pushea+deploya)

### 🔴 PRIORIDAD #1 PRÓXIMA SESIÓN (resolver ANTES que nada, incluso antes de los 3 regímenes):
**Caso MRVL — voló ~20% por endorsement de Jensen Huang (Computex) y NO llegó alerta.** Detalle completo
en memoria [[issue_news_overnight_catalyst]]. Dos huecos: (a) catalizadores nocturnos/Asia (feeds US laguean
→ cuando reportan, ya está priceado) y (b) supresión de SMS para movimientos priceados (decisión previa de
Oscar → solo dashboard). A decidir: ¿detector de gap fuerte al open / cobertura overnight? ¿mandar SMS igual
si es Tier-1 + movimiento grande aunque esté priceado? CONFIRMAR EN VM: `grep MRVL data/noticias_log_2026-06-02.csv`.

### 🟡 PRIORIDAD #2: los 3 regímenes off (el "siguiente grande" del proyecto)
Evaluar a fondo motores específicos para los 3 regímenes risk-off (con **SHORTS y materias primas**, que el
long-only no captura). El régimen ya se identifica y muestra en el header (4 tipos). Ver [[study_marea_switch_regime]].

### ✅ CAMBIOS LIVE ADOPTADOS ESTA SESIÓN (commits en main):
1. **Histéresis ±3% en el régimen macro** (`1dbd2f0`) — anti-whipsaw, reemplaza el gate binario QQQ>SMA200.
   Mejora todo (Sharpe 1.35→1.58, MaxDD −23.8→−20.7%, bear 2022 −17→−6%) con menos trades. [[study_marea_regime_layer]]
2. **Header de régimen** (4 tipos: 🟢 ALCISTA / 🔵 deflacionario / 🟠 inflacionario / ⚪ lateral) en el dashboard
   global (`c0888d2`) — informativo, no cambia qué tradea.
3. **Tamaño sugerido (vol-sizing %) en las recomendaciones de Marea** (`c949926`) — antes no se mostraba CUÁNTO
   comprar; ahora cada líder muestra "~X% del capital". [[study_marea_sizing]]
4. **K=5 → K=8** (`9fe80a9`) — el mejor uso del cash = más nombres (MaxDD −15.8 vs −20.7%, misma vol). Frontend
   "X/8" dinámico. [[study_marea_sizing]]
5. **Quitar "Conv. X/7"** de las tarjetas de Noticias (`a912694`) — confundía con el 7/10 de la IA; la convicción
   es un filtro interno que ya hizo su trabajo. Queda solo el 7/10.

### 📊 ESTUDIOS MEDIDOS (research/, todos bear-inclusive, commits en main) — VEREDICTOS:
- **C — Régimen (histéresis):** ✅ ADOPTADO (arriba). `backtest_marea_regime.py`
- **C-Fase2 — Derisk (vender en bear):** ❌ no se gana la complejidad (+0.02 Sharpe, ventas ambiguas). `backtest_marea_derisk.py`
- **D — Vol targeting de cartera (tope 1.0x):** ❌ dial de riesgo, no edge (sin leverage). `backtest_marea_voltarget.py`
- **E — 2º motor descorrelacionado (trend multi-activo):** ⚠️ diversifica (corr +0.11) pero bajo retorno (~7%/año). `backtest_marea_2ndmotor.py`
- **F — Conmutar motores por régimen (idea de Oscar):** ✅ conmutar > mezclar, PERO el 2º motor ganador = T-bills, no el sleeve; y BIL no sirve en eToro. `backtest_marea_switch.py` → derivó en el header de régimen + los 3 regímenes como prioridad.
- **G — ¿Comprar la cima? (extensión+desaceleración):** ❌ NO existe señal que prediga el techo; estirado/sobrecomprado rinde IGUAL o MEJOR; el chandelier+vol-sizing ya gestionan. `backtest_marea_exhaustion.py`
- **H — Sizing (equal vs vol-sizing, params, cash):** ✅ vol-sizing VT=0.03 bien calibrado; equal-weight = +riesgo mismo Sharpe; mejor uso del cash = K8. `backtest_marea_sizing.py`
- **Trade-alrededor-del-core + timing de ejecución (MU/universo):** ❌ comprar dips/vender rips = NO (MU es momentum). Ejecución: comprar TEMPRANO (8:30-9:00 Lima, límite cerca del open), vender tarde (~13:30-14:30). Pre-market ≈ apertura (no descuento) y eToro lo da ilíquido. `intraday_*.py`, `premarket_top10.py`. [[study_trade_around_core]]

### Resumen para el dueño: NADA de Marea cambió en su núcleo (selección validada intacta). Se ajustó el RÉGIMEN
(histéresis), el SIZING (K8) y la PRESENTACIÓN (header régimen, tamaño %, sin Conv). Falta: Oscar pushea +
`deploy.sh` + correr el pilot en la VM para repoblar el JSON con regime/size_pct/max_positions.

## 🐛 BUG CRÍTICO eToro RESUELTO (2026-06-01) — equity falso −90% + "Sin posiciones"

> **Síntoma (Oscar, primer día con 5 posiciones reales):** dashboard mostraba equity
> $5,505 → **$500.34 (−90.91%)** y "Sin datos de posiciones" pese a tener 5 abiertas.
> **Causa raíz:** el endpoint `/trading/info/portfolio` de eToro devuelve cada posición con
> `instrumentID` NUMÉRICO y **sin símbolo ni currentRate**. `_extract_ticker` las marcaba
> UNKNOWN → `get_portfolio` las **descartaba** (las 5) → `invested=0` → `total = credit (cash)
> = $500`. **Path nunca ejecutado** porque hasta hoy había 0 posiciones. eToro es el núcleo de
> datos → arrastraba equity_history, /api/positions, P&L, Gate5.
>
> **Fix (verificado en vivo, equity ahora $5,656 = real):**
> - `etoro_market.py`: `get_symbol_for_id(iid)` (mapa inverso id→símbolo) + `fetch_price_by_id(iid)`
>   + refactor `_fetch_candles_by_id`/`_price_from_candles`.
> - `etoro_client.get_portfolio`: resuelve ticker por instrumentID, **nunca descarta** una
>   posición, trae precio actual por id → valor + P&L reales, `total = cash + valor_actual_posiciones`.
> - `equity_history` se autocorrige (idempotente por día): la fila mala de hoy se sobrescribe en el
>   próximo ciclo del tracker tras el deploy.
> - ✅ DESPLEGADO Y CONFIRMADO en la VM (2026-06-01): equity real correcto (~$5,656), las 5 posiciones visibles.
>
> **Bug #2 (mismo día, post-deploy del #1):** las 5 posiciones salían "Manual" (no auto-Marea) y el
> override del dashboard **no persistía** (volvía a Manual al refrescar). Causas: (a) `/api/positions`
> leía la estrategia del **cache del tracker** (10 min viejo) en vez del tag en vivo → el override se
> revertía; (b) `resolve_strategy` solo auto-matcheaba contra el paper portfolio → MU/DELL/ARM caían a
> manual (ARM ni está en el top-80, vive en MEGA_CAP_PED). **Fix:** (a) `/api/positions` ahora resuelve
> la estrategia SIEMPRE con `resolve_strategy` (tag-aware → override instantáneo; el estado de salida
> sigue del cache); (b) `resolve_strategy` cae a **marea** si el ticker está en el alcance del Piloto
> (universo top-80 ∪ MEGA_CAP_PED), origin "universe". ✅ DESPLEGADO Y CONFIRMADO (las 5 → marea, override persiste).

## ✅ CIERRE SESIÓN 2026-06-01 — todo desplegado y en validación
> Oscar confirmó "todo se ve bien". Sistema VIVO en la VM con: IA de noticias con contexto (Sonnet 4.6),
> Piloto con panel **Top-10 líderes** + **Breakouts nuevos** (reemplaza "Mañana al abrir"), flags de
> contexto/horizonte, y los **2 bugs de eToro resueltos** (equity real + atribución/override).
>
> **Catch-up de una vez tras el deploy (ya hecho):** el `pilot_dashboard.json` viejo no traía `leaders`
> → el panel mostraba "Sin datos de líderes". Se resolvió corriendo **`venv/bin/python -m pilot.run_pilot
> --no-alert`** en la VM (reescribe el JSON con `leaders`+`fresh_breakouts`). El cron diario ya lo genera solo.
>
> **MODO:** en validación — Oscar reporta lo que vea. **Regla nueva:** el asistente hace commits pero NO
> pushea (GCM se cuelga); Oscar pushea (`!git push origin main`). Ver [[feedback_no_push]].
>
> **PRÓXIMA SESIÓN — pendientes (ver sección "🚀 PENDIENTES PRÓXIMA SESIÓN" abajo):**
> (1) backtest de "Breakouts frescos" (¿tradeable o solo radar?), (2) rediseño completo del frontend.

## ⚡ SESIÓN 2026-06-01 — IA de noticias: contexto por código + análisis macro + lenguaje simple — ✅ DESPLEGADO Y VIVO EN SONNET 4.6 (commit `5e2ef5b`)

> **Origen:** Oscar notó que el evaluador de noticias (Sonnet 4.6) "no recomendaba nada acertado".
> Diagnóstico con el log real del día (`data/noticias_log_2026-06-01.csv`, 397 filas): **el modelo NO
> era el cuello de botella** — evaluaba a ciegas (solo título + 450 chars + precio, sin contexto) y sus
> rechazos eran correctos; simplemente **no hubo catalizador fresco no-priceado** ese día (PT raises ya
> priceados, ruido Bitcoin/SpaceX). Solo **11 llamadas a IA en todo el día → 0 SMS** (silencio correcto).

**Decisiones de Oscar (cerradas):**
- **Mantener Sonnet 4.6** — NO cambiar a Opus ni cascada DeepSeek. A 11 llamadas/día el costo es trivial;
  la cascada barata no se justifica. El arreglo correcto es **dar contexto, no más inteligencia**.
- **Principio:** todo lo computable por código se calcula (ahorra tokens/latencia); la IA solo razona
  lo NO computable (macro/sector/vientos en contra).
- **Mantener Finnhub/Yahoo** (más candidatos > pocos), aunque genere ruido por edad.

**Implementado (commit `5e2ef5b`, 6 archivos):**
1. **`utils/news_context.py` (NUEVO)** — arma contexto sin tokens: trayectoria (RSI/EMA20/EMA50/ATR%),
   **sector+macro leídos de `pilot_dashboard.json`** (sector_strength + macro_bullish, mapeo ticker→ETF
   vía `star_score.SECTOR_ETF`), noticias previas del ticker (90 min), estado priceado/continuación.
2. **`conviction_gates.py`** — `allow_priced_momentum`: un movimiento ya priceado CON catalizador fresco
   (Tier-1 / earnings / FDA) ya **no se descarta**; pasa marcado `momentum_continuation=True`.
3. **`claude_scorer.py`** — prompt de analista macro + campos nuevos `titular_simple` / `analisis_simple`
   (bueno-malo entendible, sin jerga ni "8-K Item X") / `contexto_sector` / `ventana` / `tipo_alerta`.
   `max_tokens` 512→768.
4. **`main.py`** — detecta catalizador fresco, arma contexto, lo pasa al scorer; **suprime SMS** de las
   advertencias de continuación (solo dashboard — decisión Oscar).
5. **`metrics_store.py`** — persiste/expone los campos nuevos (raw_json cap 2000→3000).
6. **`dashboard.html`** — card muestra titular simple, análisis, 📊 contexto, ⏳ ventana, badge ⚠ ADVERTENCIA.

**Gate1 vs momentum (la contradicción que Oscar detectó):** el brazo de noticias bloqueaba como "ya
priceado" justo los movimientos que Marea/PED quieren montar (DELL +34% post-earnings + 6 upgrades). Ahora
esos pasan como **ADVERTENCIA de continuación** (ventana 1-2 días, dashboard-only) en vez de morir en seco.

**⚠️ TRAP del modelo (documentado):** `ai_client._CLAUDE_DEFAULT = claude-haiku-4-5`. El `model` que
`main.py` lee de `config.json` se pasa a `score_with_claude` pero **NUNCA llega a `call_ai`** (código
muerto); el modelo real lo decide la env var **`CLAUDE_MODEL`**. Para correr en Sonnet hacen falta DOS
vars: `AI_ENGINE=claude` **y** `CLAUDE_MODEL=claude-sonnet-4-6`. **Ambas confirmadas en el `.env` de la VM**
→ corre en Sonnet 4.6 de verdad. ✅ **ENDURECIDO (2026-06-01):** `model` ya se cablea por `call_ai`;
`config.json` es la fuente de verdad (env var = override opcional). Ya no cae a Haiku en silencio.

**Despliegue:** `deploy.sh` OK (fast-forward `8b01aed..5e2ef5b`, sintaxis OK, servicio activo, AlpacaNews
WS conectado, eToro pre-cargado). Validado `AI_ENGINE=claude` + `CLAUDE_MODEL=claude-sonnet-4-6` en la VM.
**Falta solo:** ver en una noticia real de mercado que Sonnet pueble bien `titular_simple`/`analisis_simple`/
`contexto_sector` (verificar en dashboard, no probado con tokens en vivo — se usó respuesta simulada).

**Piloto — contexto + horizonte (RESUELTO esta sesión, commit `d6f31ea`):** el Piloto NO lleva capa IA
que vetee la matemática (rompe el edge validado; ya se midió que vetar no es robusto). En su lugar le dimos
"los OJOS del brazo de noticias, no las MANOS": (1) **etiquetas de horizonte** (Noticias=corto plazo/evento
1-2 días; Piloto=tendencia semanas-meses) para matar la sensación de contradicción DELL; (2) **flag
informativo por compra** (estado del sector + noticia adversa reciente, leído de metrics.db) — NO veta;
(3) **log informativo** `data/pilot_news_flags.jsonl` para revisar a futuro.

**Piloto — rotación MEDIDA Y RECHAZADA (commit `d15e54b` + `research/backtest_marea_rotation.py`):** Oscar
propuso rotar a los líderes más fuertes. Backtest (2022-26, neto de fees): la rotación da **más retorno
bruto** (CAGR 77% vs 57%) pero **peor riesgo-ajustado** (Sharpe 1.31→1.22, MaxDD −25%→−36%, peor mes
−12%→−22%), **5× turnover** y **NO es robusta** (gana en años trending, pierde en 2025). Veredicto: NO
rotación automática. En su lugar → **panel Top-10 líderes** (ranking fuerza relativa) que **reemplaza
"Mañana al abrir"**: pila ranqueada #1..#10 + holdings fuera del top-10 (candidatos a rotar) + acción del
open (🎯/🛑) + stop 4×ATR + niveles desplegables + sector + noticia. Rotación = decisión DISCRECIONAL de Oscar.

**Piloto — sección "Breakouts nuevos" (commit `7ee7a49`):** disparada por el caso SNOW (+77% en 10d pero
fuerza 6m solo +12% → invisible en el top-10). `indicators()` ahora calcula `ret_10d`; `_fresh_breakouts`
surfacea rupturas frescas (sobre SMA200 + nuevo máx 50d, fuera del top-10) ordenadas por retorno reciente.
Es **RADAR, no lista de compra**: los 🎯 son compras validadas, los 🆕 son vigilancia.

**Caso SNOW analizado (NO era bug):** en Piloto, su momentum 6m bajo lo deja fuera del top-10 (correcto, por
diseño) + cartera 5/5 sin slot. En Noticias, llegó pero se filtró bien (2 artículos Finnhub de ~9h de viejo
+ 1 Alpaca con keyword score 0). El brazo de noticias dispara por CATALIZADOR (titular Tier-1), no por precio.
*(Aparte detectado: corre un cap global de 45 min que pisa la lógica source-aware de 90 min de Finnhub —
podría matar noticias legítimas que laguean 60-90 min. Revisar.)*

## 🔬 SESIÓN 2026-06-01 (CONTINUACIÓN) — ESTUDIO A: selección/rotación de Marea — VEREDICTO: NO CAMBIAR

> **Origen:** Oscar quería saber EXACTAMENTE qué comprar/vender/mantener cada día; le hacía ruido
> que Marea no tenga horizonte (PED sí) y no estaba convencido de que rotar al top fuera malo.
> Pidió un plan exhaustivo "sin dejar cabos sueltos". Objetivo elegido: riesgo-ajustado (Sharpe/MaxDD).

**Construido:** `research/backtest_marea_selection.py` — motor flexible sobre el arnés validado
(universo top-80, fee $1/lado, open+1, macro QQQ, vol-sizing). 4 ejes: señal de ranking × regla de
venta × cadencia × relleno. Modos `grid` / `neighborhood` / `stress`.

**VEREDICTO: NO TOCAR MAREA. BASE (gate de breakout + chandelier 4×ATR) es el ganador riesgo-ajustado.**
CAGR +35.8% / Sharpe 1.35 / MaxDD −23.8% — el mejor drawdown de todos.

**La trampa que lo decidió (metodológica):** con datos desde 2022, los indicadores recién calientan
a fin de año → **el bear 2022 nunca se opera** → sample sesgado al bull que FLATTEREA lo agresivo
(quitar gate parecía Sharpe 1.48; horizonte 1.92; rank-band 1.70). El `--mode stress` (datos desde
2020, 2022 = bear real operable) lo DA VUELTA:
- NoGate (quitar gate): Sharpe 1.27 / MaxDD **−34%** → el gate de breakout es PROTECCIÓN de régimen.
- BASE+horizonte 84: Sharpe 1.33 / MaxDD −33% (capar por tiempo empeora DD); horizonte 126 ≈ no-op.
- NoGate+horiz sin stop: Sharpe 1.44 / MaxDD **−43%** → peligroso sin stop.
- Rank-band 2K (= rotación): Sharpe **0.97** / MaxDD **−48%** → COLAPSA en el bear. Confirma+explica
  el rechazo previo (`backtest_marea_rotation.py`): el DD se descontrola justo cuando importa.

**Para la ansiedad "Marea sin horizonte":** el chandelier YA es la regla de venta definida (vende si
el cierre diario rompe máx-desde-entrada − 4×ATR). No le falta deadline; dejar correr SIN fecha es el
edge del trend-following. Único lever real = K (subir a 8-10 mejora Sharpe/DD pero choca con ~5
manejables en eToro). Detalle en memoria [[study_marea_selection_rotation]].

## 🔬 SESIÓN 2026-06-01 (CONT.) — ESTUDIO B: breakouts frescos/SNOW + LEVER K — VEREDICTOS CERRADOS

> `research/backtest_fresh_breakouts.py` + `backtest_marea_selection.py --mode ksweep`, ambos
> bear-inclusive (datos desde 2020, lección del Estudio A). Detalle en [[study_fresh_breakouts_and_k]].

**B — Breakouts frescos: RADAR, NO COMPRA (hipótesis confirmada).**
- Event study (exceso vs QQQ tras la ruptura, primer toque): jerarquía **STRONG >> SNOW > FRESH**.
  +20d: STRONG +6.8% / SNOW +2.5% / FRESH +1.7%. El pop no se invierte de golpe pero rinde 1/4–1/2
  de los LÍDERES que Marea ya compra. SNOW **negativo en 2022 (−1.2%) y 2023 (−0.5%)** → revierte en
  el bear. No robusto.
- Sleeve operable: peor riesgo-ajustado que Marea (Sharpe 1.15 vs 1.35, MaxDD −38% vs −24%). Su mejor
  caso (stop 3×ATR) solo IGUALA con menos retorno. → el panel 🆕 sigue siendo RADAR/vigilancia. Si se
  tradea uno discrecional: stop angosto 3×ATR + tamaño chico + unvalidated.

**LEVER K (nº posiciones, sobre BASE bear-inclusive): K=5 BIEN ELEGIDO.**
K=3-4 Sharpe ~1.22/DD −31% (concentrar empeora) · **K=5 Sharpe 1.35/DD −23.8% (live)** · K=8 Sharpe
1.39/DD −22.1% (sweet spot, pero +0.04 Sharpe no compensa +60% trades/fees + 8 nombres manuales) ·
K=10-12 se aplana/decae. NO bajar de 5; mild upgrade a 6-8 solo si se automatiza la ejecución.

**CIERRE DEL BLOQUE (A+B+K):** Marea NO se toca. Es robusta tal cual (gate breakout + chandelier, K=5).
Todas las decisiones medidas con datos bear-inclusive. Commits en main (sin push, los pushea Oscar).

## 🔧 SESIÓN 2026-06-01 (CONT.) — pulidos: plumbing modelo Claude + cap de noticias — HECHO (falta deploy)

**1. Plumbing del modelo Claude — ENDURECIDO.** Antes el `model`/`claude_model` de config.json era
código muerto: nunca llegaba a `call_ai`, el modelo real lo decidía SOLO la env var `CLAUDE_MODEL`
→ si se perdía, caía a Haiku EN SILENCIO. Fix: `call_ai`/`_call_claude` aceptan `model`;
`score_with_claude` lo pasa; precedencia **config.json (vía main) > env CLAUDE_MODEL > default Haiku**.
Ahora config.json es la fuente de verdad (la env var queda como override opcional). Validado sin red.

**2. Cap de edad de noticias — source-aware restaurado.** `config.json` tenía
`max_article_age_minutes: 45` que PISABA la lógica source-aware (mataba earnings de Finnhub que
laguean 60-90 min y hasta 8-Ks tardíos). Puesto a `null` → EDGAR=240min, Finnhub/Yahoo=90, Alpaca=120,
default=90. **Dedup ensanchado a 120 min** (`cross_source_dedup_minutes`) para cubrir el lag de 90 de
Finnhub + jitter: si Alpaca/Benzinga ya trajo Y procesó el evento (`sent_to_claude=1`), la copia tardía
se descarta; si nunca se procesó, la 2da copia pasa (decisión de Oscar: estar al tanto no daña, pero sin
repetir). Validado: Finnhub 70min ya NO muere por edad; EDGAR 200min pasa, 260 no.

✅ **DESPLEGADO en la VM (2026-06-01).** Los 3 fixes en producción. Commits en main; Oscar pushea.

## 🕐 FIX 2026-06-01 — zona horaria Lima en el frontend (commit `ee0b826`)
> Oscar descargó "el log de hoy" y le bajó el del **02-jun**. Causa: el **backend ya estaba 100% en
> Lima** (`datetime.now(LIMA)`=UTC-5 en app.py + metrics_store), pero el **frontend** calculaba "hoy" con
> `new Date().toISOString()` = **UTC** → de noche en Lima ya es el día siguiente en UTC. Fix en
> `dashboard.html`: `today()` usa `America/Lima`; reloj "Actualizado" forzado a Lima; `minsAgo()` interpreta
> el `ts` (Lima naive) como Lima absoluto (`+'-05:00'`) → "hace X min" correcto en cualquier navegador.
> Verificado (00:45 UTC = 19:45 Lima → fecha 2026-06-01, no -02). **Aplica al desplegar a la VM** (la VM corre
> en UTC pero el código usa el offset Lima explícito, así que los timestamps salen en Lima sin tocar el SO).

## ✅ VALIDACIÓN DEL LOG DEL DÍA — `data/noticias_log_2026-06-01.csv` (cierre 2026-06-01)
> Revisado a pedido de Oscar. **Veredicto: el sistema va bien, funcionando como se diseñó.**
- **Zona horaria CORRECTA (Lima):** el log abarca 00:03 → 19:35 del 2026-06-01 (día completo Lima), todos los ts en Lima.
- **Embudo sano:** 1171 eventos → 1141 filtrados (97.4%: **590 por edad**, 551 por keywords/score) → 6 Gate1-bloqueado (priceado) → 22 a IA (15 baja + 7 media) → **2 SMS**.
- **2 SMS legítimos, ambos del feed RT Alpaca-Benzinga (age 0 min):** **AMD** (Barclays PT→$665, ai=7 ALTA) y **TLN** (clearances regulatorias de adquisición, ai=7 ALTA).
- **Cap de edad source-aware CONFIRMADO en vivo:** las razones de edad muestran `max=90` (antes 45) y **0 eventos con age>90 se colaron** → el deploy del fix tomó. Lo que llegó a IA era fresco (27/30 con 0-5 min, máx 34).
- **Gate1 (compute-first) correcto:** bloqueó 6 priceados — p.ej. **4 alzas de PT de DELL** (ya +34% post-earnings) sin spamear un SMS por analista. Caso de "continuación priceada" funcionando.
- **IA discrimina bien:** notas "Maintains/Raises PT" de bajo valor → ai_baja (1-3); sustantivas → ai_media (4-6); solo 2 catalizadores reales → ai=7 → SMS.
- **Fuentes activas y diversas:** Finnhub/Yahoo 487, EDGAR 315, Alpaca-Benzinga 160 (RT), Finnhub-Benzinga 135, + SeekingAlpha/CNBC/Chartmill/Fintel.
- Observación (no problema): mucho ruido de "Maintains/Raises PT" de analistas, bien filtrado por Gate1+IA (cero SMS falsos). Si algún día molesta, endurecer keyword filter para PT-raises — hoy no hace falta.

## 🚀 PENDIENTES PRÓXIMA SESIÓN (acordado 2026-06-01)
0. ✅ **HECHO — Estudios A (selección/rotación), B (breakouts frescos) y lever K** → Marea NO se toca.
1. ✅ **HECHO — Backtest de "Breakouts frescos"** → RADAR confirmado (ver bloque B arriba). El panel 🆕
   se queda como vigilancia, no lista de compra.
2. ✅ **Rediseño del frontend — HECHO Y DESPLEGADO EN LA VM (2026-06-01, Oscar confirmó "todo bien").** Commits `4c4332f` reskin, `a1c7d9c` responsive, `6d5f567` Piloto brief6, `ee0b826` tz Lima. Piloto rehecho al brief 6 de Stitch (header + stats + equity degradado + grid Top-10 izq/Breakouts der + tablas card), `_plBuyCard` muerto eliminado, verificado con datos reales locales. Detalle en [[project_frontend_redesign]].
   Reskin completo de `api/dashboard.html` al diseño de Stitch (fintech claro, sidebar, Geist+Inter, acento
   índigo) PRESERVANDO toda la lógica JS e IDs (cero cambio de comportamiento — se voltearon los tokens
   `:root` oscuro→claro; como el JS usa `var(--*)` casi todo se reskineó solo + bloque de overrides + shell
   a sidebar). **Responsive 100% verificado** con Edge headless: `scrollWidth==clientWidth` (cero desborde)
   en vw 470/870/1270, las 4 pestañas cambian sin errores JS, content-top ahora envuelve (era `height:64px`
   fijo → `min-height`+`flex-wrap`), nav 2-col y stats 2-col en móvil ≤560. Detalle de diseño en
   [[project_frontend_redesign]]. FALTA: `deploy.sh` a la VM para verla con datos reales + pulir vistas
   pobladas (alertas/Top-10/posiciones/equity usan los mismos componentes) + limpiar `_plBuyCard` muerto.
3. ✅ **HECHO — plumbing del modelo Claude + cap de noticias** (2026-06-01, ver bloque abajo).

## ⚡ SESIÓN 2026-05-31 (NOCHE) — tarjetas Piloto + stop vivo + filtro invalidación medido — COMMITEADO+PUSH; Oscar despliega

> **Contexto:** Oscar vio "Niveles no disponibles (correr el pilot)" en las cards → era dashboard JSON viejo
> (la feature de niveles es de esta misma sesión). Se resolvió corriendo el pilot en la VM. OJO trampa: en la
> VM `python -m pilot.run_pilot` con el python del sistema falla (`ModuleNotFoundError: dotenv`); usar el venv:
> **`venv/bin/python -m pilot.run_pilot`**.

**1. Salida de Marea — CLARIFICADA (era confusión de Oscar).** El stop 4×ATR es un **SL TRAILING (chandelier)**,
   NO un TP: `máx_desde_entrada − 4×ATR`, solo sube nunca baja; sales si el **cierre diario** rompe el nivel.
   Marea NO tiene target (trend-following: deja correr). PED sale por **tiempo ≈ Day+7**. Sin TP en ninguna.

**2. Tarjetas "Mañana al abrir" rediseñadas** (`api/dashboard.html` `_plBuyCard`): más grandes (protagonistas),
   jerarquía clara — 🎯 comprar al OPEN · 🛑 STOP 4×ATR destacado (la salida) · horizonte por estrategia
   (Marea sin target / PED ≈Day+7). Se QUITARON los chips de pullback como "stops" (confundían) y se
   reintrodujeron como **DOS referencias DISCRECIONALES** (no reglas, etiquetadas como tal):
   - 🟢 **Pullback opcional** (orden límite, **SIN SL** · si no llena, al open) = EMA20 + Retest breakout.
   - 🔎 **Invalidación** (soporte 10d) = "si cae por debajo, evaluar posible tesis rota (criterio, no SL)".

**3. Filtro de entrada por invalidación — MEDIDO y RECHAZADO como regla** (`research/backtest_marea_invalidation.py`).
   Veto de entrada si el open abre bajo breakout/soporte/gap. El "veto breakout" parecía ganar (+103pp total,
   Sharpe 1.25→1.43) pero NO es real: n=7 vetos en 4 años, mejora por **path-dependence** (reshuffle de cartera),
   **se revierte en 2025** (−4.2pp), y el **DD EMPEORA** (−27.0 vs −25.2). Las vetadas eran mixtas (1 runner /
   1 perdedora). Veredicto: **NO veto automático**; el breakout/soporte perdido NO predice fracaso de forma
   fiable. Se rescata solo como **referencia discrecional** en la tarjeta (punto 2). Refuerza el "sin SL":
   el chandelier 4×ATR ya da espacio; un SL fijo en soporte churnnea (ya rechazado en sección 2 del test previo).

**4. Posiciones "En cartera" — dos columnas nuevas:**
   - **Días** (D+x): PED muestra `D{x}/6` (rojo cuando toca salir ~Day+7); Marea solo días en cartera.
   - **Stop 4×ATR** vivo (solo Marea): nivel chandelier + **colchón %** (cuánto cae antes de tocarlo).
     Backend `run_pilot._live_chandelier()`; payload `positions[]` ahora trae `days_held`, `entry_date`,
     `ped_hold_days`, `stop_4atr`. Se actualiza en cada corrida diaria del pilot (no intradía — es cierre diario).

**PENDIENTES:**
- 🚀 **Oscar despliega esta versión** (`deploy.sh` + `venv/bin/python -m pilot.run_pilot` para repoblar
  `entry_levels` + `days_held` + `stop_4atr` en el JSON).
- 🔔 **TEMA DE MAÑANA (lo trae Oscar): uso de la IA en el sistema.** Continuación pactada.
- ✅ **MU ~$971 NO es bug** — Oscar confirmó que MU superó los $1000 hoy (los $100-130 eran de hace meses;
  conocimiento del asistente desactualizado). El dato de eToro es correcto; la tarjeta escala bien.

**DESPLEGADO Y CONFIRMADO (2026-05-31 23:08 Lima, commit `8b01aed`):** `deploy.sh` OK (fast-forward,
servicio activo, AlpacaNews WS conectado, WhatsApp de arranque enviado). `venv/bin/python -m pilot.run_pilot`
→ "ya corrido para 2026-05-29 — nada que hacer" + reescribió `pilot_dashboard.json` con los campos nuevos.
Oscar ve el frontend al día. Sesión cerrada.

## ⚡ SESIÓN 2026-05-31 (TARDE) — análisis de tácticas + Fase 3 + dashboard — COMMITEADO EN main, SIN PUSH NI DEPLOY

> **Modo de trabajo:** se trabaja SIEMPRE directo en `main`, sin ramas, salvo que Oscar lo pida (preferencia
> nueva). Commits solo cuando él lo aprueba; no pushear salvo indicación. El sistema queda EN VIGILANCIA:
> Oscar reportará cualquier error que vea en vivo.

**1. Tácticas de ENTRADA — investigadas y CERRADAS con datos** (3 backtests en `research/`, detalle en
   memoria `project_entry_tactics_phases`). Veredictos:
   - **Marea pullback vs open** (`backtest_marea_entry.py`): pullback profundo = trampa (cae bajo QQQ);
     pullback suave parecía ganar pero NO es robusto (pierde −29pts en 2024, inestable por ventana).
     → **La señal entra al OPEN.** Stop "rompe-tesis" también rechazado (no baja el drawdown).
   - **PED qué día entrar** (`backtest_ped_entry_timing.py`): la acción NO sube de noche tras la reacción;
     entrar antes (martes-cierre) es peor; entrar al gap del martes mata el edge. → **Mantener Day+2 open.**
   - **PED qué hora del D+2** (`backtest_ped_intraday_d2.py`, velas 1-min): no hay "pop" que haga comprar
     la cima; el timing intradía es un empate (±0.2%, dentro del ruido). → No meter orden de mercado en el
     primer minuto (micro-pop + spread ancho); entrar en la apertura o primeros ~30 min, en cualquier micro-dip.

**2. Fase 3 — SALIDA event-driven por noticia adversa (IMPLEMENTADA + testeada, falta deploy).**
   En `main.process_article`: si la IA marca `direccion=SHORT` sobre un ticker EN CARTERA real
   (`account_cache` vía `_held_tickers()`, fail-safe si cache >60min) y es alta convicción (`score_ia>=7`
   o `event_mode`) → alerta URGENTE `_send_adverse_news_alert` "considerá salir", INMEDIATA 24/5 (no se
   encola al digest), dedup 1/ticker/día, suprime el SMS de oportunidad normal. Decisiones de Oscar:
   alta-convicción + inmediata 24/5. Tests `python test_adverse_news.py` (12/12). Depende de que el news
   arm entregue catalizadores (se valida ~2-jun).

**3. Dashboard — protagonismo del Piloto.** La sección "Mañana al abrir" pasó de una línea apretada a
   CARDS por ticker: ⭐ TOP + badge Marea/PED + **stop 4×ATR (chandelier) destacado** + niveles de
   pullback opcionales (EMA20/retest/soporte) como chips. Usa el `entry_levels` del JSON que antes NO se
   mostraba (era el único hueco real de la auditoría). Las cards se poblarán el lunes cuando el cron del
   pilot genere compras (ahora `buys` vacío = último día viernes).

**4. Auditoría de la app — SANA.** Compila todo el runtime; tests pasan (`test_position_strategy`,
   `test_adverse_news`); sin trap py3.9 PEP 604 (solo docstrings; `etoro_market`/`alpaca_news` ya tienen
   el future import); conviction/event gate OK; eToro read-only OK ($5,505, 0 pos); pilot end-to-end OK;
   endpoints `/` (dashboard) y `/api/pilot` 200. NO se hallaron bugs que corregir.

**Commiteado en main (4 commits, SIN push):** `ceb9255` Fase 1 niveles · `2934d6f` research backtests ·
`1a27996` Fase 3 · `b584504` dashboard. **PENDIENTE: deploy a la VM** (Fase 3 + dashboard + Fase 1) con
`deploy.sh` cuando Oscar lo decida (sugerido junto con la validación del news arm el martes 2-jun).

## ⚡ SESIÓN 2026-05-31 — ✅ PUSHEADA Y DESPLEGADA EN LA VM (10:55 Lima)
Servicio `opportunity-alert` activo con el código nuevo. Logs de arranque confirman:
`Fuentes activas: EDGAR, AlpacaNews (Benzinga/WS), Finnhub, PositionTracker` +
`[AlpacaNews] WebSocket conectado y suscrito (fuente: Benzinga)` + WhatsApp de arranque OK.

✅ **eToro precio CONFIRMADO EN VIVO (2026-05-31 16:20 Lima, commit `be51716`):** el pre-warm
`[Startup] Mapa de instrumentos eToro pre-cargado` ya sale en logs y `fetch_price('NVDA')` da
$211.69 con velas 1m reales (instrumentId NVDA=**1137**, no 1005 como decía la memoria vieja).
🐛 **Bug que estaba tapando todo esto:** `utils/etoro_market.py` usaba `-> int | None` (PEP 604)
SIN `from __future__ import annotations` → en la VM (py3.9) el módulo NO importaba (TypeError en
def-time), así que eToro caía SILENCIOSAMENTE a Alpaca y el pre-warm moría en su `except`+debug
(invisible). Fix = agregar el future import (mismo que ya tenía alpaca_news.py). El `py_compile`
del deploy NO detecta esto porque no evalúa anotaciones. Lección: todo módulo nuevo que use
`X | None` necesita el future import en py3.9. Si el token eToro muere → llega SMS de aviso.
Hecho esta sesión (todo verificado en vivo, imports + tests OK):
- **Brazo de noticias en tiempo real**: Alpaca News = feed Benzinga GRATIS por WebSocket
  (descartado Benzinga pago). `sources/alpaca_news.py`. Ya desplegado antes (commit d63f757).
- **PED arreglado**: `fetch_earnings_map` usa Finnhub (no yfinance roto en py3.9). Ya desplegado.
- **eToro = fuente PRIMARIA de precio actual + velas 1m** (broker real → fidelidad máxima),
  Alpaca fallback. `utils/etoro_market.py`. prev_close vía snapshot IEX (free no permite SIP
  reciente). Resuelve deficiencias IEX #1 (Gate1) y #4 (P&L de cierres fiel).
- **Arquitectura de datos** (ver memoria project_data_sources_arch): eToro = precio intradía +
  portfolio; Alpaca = barras diarias batch (Marea/PED/universo — eToro es 1 id/llamada, sin batch).
- **Robustez**: (#1) circuit breaker + token-expiry compartido en etoro_market; (#2) pre-warm del
  mapa instrumentos (evita 4.5s en la 1ª noticia); (#3) weekend_queue persistida a disco; (#4 PED)
  log de cobertura earnings; (#6) event-mode usa dirección adivinada (no LONG fijo);
  (#7 dedup) ventana cross-source 20→60min; (Gate5) lee account_cache, no llama eToro síncrono;
  (#2 stream) watchlist viva refrescada cada 60s.
- **Equity real eToro** (curva en dashboard) + limpieza cosmética (premarket/win-rates Watcher).
- NO tocados por criterio: EDGAR map manual (#5, ticker_in_text ya cubre) y huecos de equity (#7).

⚠️ Validar en deploy: la VM necesita `etoro_config.json` con keys vivas (ya lo tiene del portfolio
read-only). Confirmar en logs: `[Startup] Mapa de instrumentos eToro pre-cargado` y
`[AlpacaNews] WebSocket conectado`. Si el token eToro está muerto, ahora llega SMS de aviso.


> Este es el documento de continuidad. Para retomar: leé esto + las memorias en
> `.claude/.../memory/` (índice en MEMORY.md) + `REBUILD_PLAN.md` para el detalle histórico.

---

## 1. QUÉ ES EL SISTEMA HOY (en una frase)

Un sistema de **momentum multi-día** con DOS fuentes de señal validadas con datos, más un
sistema de alertas de noticias. Se eliminó todo lo que NO tenía edge. Es **100% MANUAL**
(no ejecuta órdenes; Oscar opera en eToro tras la recomendación).

**Las 2 estrategias validadas (el núcleo):**
- **🟢 Marea** = momentum por PRECIO (líderes por fuerza relativa, breakout 50d, chandelier 4×ATR,
  filtro macro QQQ>SMA200, top-5 posiciones). Universo: top-80 liquidez (auto-refresh semanal).
- **🟣 PED** = momentum por EARNINGS (mega-cap ≥$100B + reacción Day+1 ≥+5% → entra Day+2, sale ~Day+7).

Ambas viven en `pilot/`, comparten un paper portfolio (`pilot/run_pilot.py`), un track record y el
dashboard. Más el **badge ⭐ TOP** (tercil superior validado) y **fuerza por sector**.

---

## 2. ESTADO DE CADA COMPONENTE

| Componente | Estado |
|---|---|
| **Marea** | ✅ VALIDADO out-of-sample (2022-26): CAGR ~37% (universo ancho honesto) vs QQQ 16%, bate 4/5 años, protege en bear, diversificado (27 ganadores, 8 sectores). |
| **PED** | ✅ VALIDADO + out-of-sample: mega-cap+≥5%+D7 = +1.0-1.2%/trade net, WR 64-67%, robusto en ambas mitades. |
| **Score ⭐ TOP** | ✅ VALIDADO: el tercil TOP predice retorno forward (3-4x el resto). BINARIO (solo TOP; 2vs1 es ruido). |
| **Módulo unificado** | ✅ Construido e integrado (`run_pilot.py` + `paper_portfolio.py` + `ped_signals.py` + `star_score.py`). Corre end-to-end. |
| **Dashboard Piloto** | ✅ Honesto: banner backfill, vs QQQ, realizado/no-realizado, badges Marea/PED, ⭐, sectores. |
| **Noticias** (catalizador) | ✅ Funciona, manual. Filtro earnings rescatado (`utils/earnings_calendar.py`). |
| **Login dashboard** | ✅ Arreglado (auto_error=False; auto-entra sin clave en local). |
| **Posiciones** (eToro real) | ✅ CONECTADO read-only (2026-05-30). Endpoint: `public-api.etoro.com/api/v1/trading/info/portfolio`. Cuenta real: $5,505 cash, 0 posiciones abiertas hoy. Test: `python test_etoro_connection.py`. |
| **Deploy VM** | ⏳ PENDIENTE — hoy corre manual. |

**ELIMINADO (sin edge, con datos):** Watcher (scalping 1m, murió por fees), auto-trading
(estaba cableado en app.py — desconectado), premarket scanner intradía (sin edge), PED naive.
Todo en `_deprecated/` o `_quarantine/`.

---

## 3. ESTRUCTURA

```
opportunity_alert/
├── main.py, config.json          # runtime noticias 24/7
├── api/ (app.py, dashboard.html) # frontend — 4 tabs: Noticias/Posiciones/Watchlist/Piloto
├── pilot/                        # 🌊 MAREA + PED (el núcleo)
│   ├── run_pilot.py              # orquestador diario
│   ├── momentum_signals.py       # motor Marea
│   ├── ped_signals.py            # motor PED
│   ├── star_score.py             # ⭐ TOP + sectores
│   ├── paper_portfolio.py        # portfolio papel (source: marea/ped)
│   ├── universe.py               # top-80 liquidez (auto semanal)
│   └── REVALIDACION_MENSUAL.md   # 🔔 recordatorio
├── utils/ (earnings_calendar, universe, metrics_store, etoro_client...)
├── research/                     # backtests validados (NO runtime)
├── _quarantine/                  # auto-trader (NO activar)
└── _deprecated/                  # Watcher, premarket scanner, watchlist CLI
```

---

## 4. CÓMO OPERAR Y CORRER

**Flujo diario (manual hoy, automático tras deploy VM):**
1. Después del cierre USA (~3pm Lima) corre `python -m pilot.run_pilot` → genera órdenes + alerta.
2. Oscar revisa el dashboard (tab Piloto) o la alerta: "Mañana al abrir: 🟢 COMPRAR X⭐, 🔴 VENDER Y".
3. Oscar ejecuta en eToro a la apertura (8:30am Lima). Manual.
4. (Cuando eToro esté conectado) Posiciones refleja su portafolio real.

**Comandos:**
```bash
python _run_api.py                 # levantar dashboard (localhost:8081, sin login en local)
python -m pilot.run_pilot          # correr el día (alerta + dashboard)
python -m pilot.run_pilot --no-alert
```
Dashboard: el server en background se recicla; correrlo en terminal propia para estabilidad.

---

## 5. HALLAZGOS VALIDADOS CLAVE (no re-litigar)

- Marea real ≈ **~25-37%/año** (NO el +228% del backfill ni el +73% del universo cherry-picked).
  El +228% del estado live es un backfill in-sample dominado por 1 ticker (WDC +750%).
- Premarket intradía y PED naive: **sin edge** (el pop ya priceó, revierte). Earnings-day = perdedor.
- El edge de earnings está en el **drift multi-día (PED)**, no en el pop.
- Cap de tamaño/trim en Marea: **no mejora** el riesgo-ajustado (el chandelier ya gestiona). CAP 30% casi gratis como seguro contra blowup de 1 nombre — opcional, decisión de Oscar (lean: BASE o CAP 30%).
- Diversificación cross-sector confirmada (Marea no es solo IA).

---

## 6. PENDIENTES (priorizados — POR AcÁ SEGUIMOS)

1. ✅ **HECHO — Conectar eToro read-only** (2026-05-30). `data/etoro_config.json` configurado con
   cuenta real solo-lectura. Endpoint corregido a `public-api.etoro.com/api/v1/trading/info/portfolio`
   (la nota vieja de CLAUDE.md estaba mal: ni `api.etoro.com` ni sin `/api/v1/`). Verificado en vivo:
   $5,505 cash, 0 posiciones. El PositionTracker y `/api/positions` ya pueblan solos.
2. ✅ **HECHO — Salida por estrategia en Posiciones** (2026-05-30). Reemplazadas las heurísticas
   viejas (T1 +8% / retroceso 4% / `portfolio.md`) por salida-por-estrategia en `utils/position_strategy.py`:
   atribución HÍBRIDA (override dashboard > auto-match piloto > manual), salida Marea=chandelier 4×ATR
   sobre cierre diario, PED=tiempo (Day+6≈Day+7), manual=solo contexto de noticias. El PositionTracker
   ahora aconseja "VENDER al open" en vez de cortar ganadores. Posiciones abiertas se auto-añaden al
   universo de noticias. Override por posición en el dashboard (dropdown Marea/PED/Manual/Auto).
3. ✅ **HECHO — Pulidos arquitectura A–E** (2026-05-30):
   - **A.** `/api/health` capital ahora = cash REAL de eToro (vía `account_cache.json` que escribe el
     tracker cada 10 min; cae a env si el cache >60 min). Cero llamadas a eToro por request.
   - **B.** Dashboard muestra estado de salida en vivo por posición (stop chandelier + margen, días PED,
     señal VENDER) — leído del cache, sin fetches de Alpaca en el frontend.
   - **C.** Heartbeat consolidado a 1 llamada eToro (antes 2).
   - **D.** Tickers auto-añadidos por posición van en grupo aparte "📊 En posición (auto)" en la
     Watchlist, sin botón de borrar (los gestiona el tracker).
   - **E.** Tests: `python test_position_strategy.py` (13 tests, sin red). + bug corregido:
     `_explain_large_moves` nunca disparaba (snapshot mal-ordenado + faltaba `current_rate`).
4. ✅ **HECHO — Deploy a VM (2026-05-30).** Servicio `opportunity-alert` corriendo el código nuevo
   (commits `18c341b`→`a5c429a`+). main.py = noticias + advisor posiciones + dashboard (:8081).
   eToro READ-ONLY confirmado en vivo en la VM ($5,505 cash). Marea genera recomendaciones OK
   (ej. SNDK/MU/WDC/DELL/ARM). Cron del pilot agendado: `0 22 * * 1-5` (VM en UTC; ~1-2h post-cierre).
   Mecanismo SIN cambios: `bash /home/opc/oportunity-alert/deploy.sh`. `data/etoro_config.json` se crea
   manual en la VM (gitignored). VM = **Python 3.9** → yfinance pineado a `<0.2.52` (las nuevas exigen py≥3.10).

   ⚠️ **DOS hallazgos del deploy:**
   - **(a) PED roto en la VM:** yfinance <0.2.52 NO puede leer earnings de Yahoo (todos los mega-cap dan
     "possibly delisted") → PED nunca detecta candidatos. Marea NO se afecta. **A resolver próxima sesión**
     (junto con Benzinga/fuentes de datos): opciones = earnings vía Finnhub (validar cobertura) o subir
     la VM a Python ≥3.10 para yfinance moderno. Ver [[project_ped_earnings_vm]].
   - **(b) Alerta del pilot — ARREGLADO Y DESPLEGADO** (commit `5733f37`, `deploy.sh` corrido 2026-05-30 21:08 Lima):
     `run_pilot` leía `TWILIO_TO` de env (vacío); el número vive en `config.json`. Ahora lo lee de ahí.
     Falta solo CONFIRMAR que sale el WhatsApp (item 3 abajo).

---

## 6bis. 🚀 PRÓXIMA SESIÓN — PLAN DE ARRANQUE (los 3 items, en orden)

**1. ✅ RESUELTO (2026-05-31) — brazo de NOTICIAS en tiempo real.** Ver [[project_benzinga_news_arm]].
   - Benzinga directo se **descartó** (precio no público / caro para el capital). Solución: la **Alpaca News
     API entrega el MISMO feed de Benzinga, GRATIS** con la cuenta Alpaca que ya usamos para precios.
     WebSocket push en tiempo real → mata el lag de 30-60min de Finnhub (el cuello de botella).
   - Validado: cobertura 32/32 watchlist, `source=benzinga`, handshake auth+subscribe OK en vivo.
   - Implementado: `sources/alpaca_news.py` (WS streamer), `alpaca_news_loop()`+thread "AlpacaNews" en
     `main.py`, `"ALPACA":120` en keyword_filter, `websocket-client` en requirements.
   - ✅ **DESPLEGADO en la VM (2026-05-31, commit `d63f757`):** servicio activo, log confirma
     `[AlpacaNews] WebSocket conectado y suscrito (fuente: Benzinga)`. Fix py3.9: `from __future__
     import annotations` (la VM no soporta `dict | None` de PEP 604; el py_compile del deploy no lo
     detecta porque no evalúa anotaciones).
   - **Falta solo:** validar una mañana de mercado que lleguen catalizadores frescos por el stream.

**2. ✅ RESUELTO (2026-05-31) — PED arreglado (opción A).** Ver [[project_ped_earnings_vm]].
   - Problema: yfinance <0.2.52 (py3.9 de la VM) no lee earnings de Yahoo → PED sin candidatos. Marea OK.
   - Solución: `pilot/ped_signals.fetch_earnings_map` reescrito para usar Finnhub `/calendar/earnings`
     (free tier, validado HTTP 200). Misma firma + mismo formato `{tk:[(date,'amc'/'bmo')]}` → lógica PED
     intacta. Filtra a earnings ya reportados (epsActual no nulo) para descartar fechas futuras agendadas.
   - Validado end-to-end (`research/ped_finnhub_e2e.py`): cobertura mega-cap correcta + AMD disparó
     ENTRADA PED (+18.6% reacción). yfinance ya NO es dependencia de PED.
   - ✅ **CONFIRMADO en la VM (2026-05-31):** `ped_finnhub_e2e` corrió en la VM py3.9 con earnings reales
     (NVDA/CRM/COST/WMT/HD/AMD con fechas), CERO `possibly delisted`, AMD disparó ENTRADA PED. Cerrado.

**3. 🔧 Confirmar alerta del pilot por WhatsApp.** Fix ya desplegado. Confirmar de dos formas:
   - Inmediata: en la VM `cd /home/opc/oportunity-alert && venv/bin/python -m pilot.run_pilot` → debe imprimir
     "alerta WhatsApp: enviada" y llegar el mensaje; o
   - Esperar al cron del lunes 22:00 UTC.

## 6ter. PENDIENTES MENORES
- **📈 Historial de equity real eToro:** ✅ HECHO Y DESPLEGADO (2026-05-31) — `utils/equity_history.py`
  (snapshot diario idempotente), cableado en `position_tracker_loop`, endpoint `/api/equity-history`,
  curva en la pestaña Posiciones. Se construye sola (1 punto/día) → tendrá forma tras varios días vivo.
- **🧹 Limpieza cosmética dashboard:** ✅ HECHO Y DESPLEGADO (2026-05-31) — quitado `_next_premarket()`
  + campo `next_premarket` de `/api/health`; `SESSION_INFO` ya no muestra los win-rates del Watcher
  eliminado (engañosos); span `swr` quitado de la topbar.

## 6quater. 🔔 RECORDATORIOS CON FECHA (decir a Oscar el día/hora)
| Cuándo (Lima) | Tarea | Cómo |
|---|---|---|
| **Lun 1-jun ~17:30** | Confirmar que llegó la alerta del pilot por WhatsApp | El cron corre lun 22:00 UTC (17:00 Lima). Revisar que llegó el mensaje |
| **Mar 2-jun 08:45** | Validar stream de noticias + correr latencia | En la VM: `venv/bin/python -m research.latency_alpaca_vs_finnhub --minutes 90` (mañana de mercado activa) |
| **Mar 2-jun (con lo anterior)** | Desplegar Fase 1 + Fase 3 + dashboard a la VM | `bash deploy.sh`. Commits ya en main (`ceb9255`, `1a27996`, `b584504`). Confirmar en logs `[AdverseNews]` si dispara y ver las cards del Piloto en el dashboard tras el cron |
| **Mar 30-jun 09:00** | Revalidación mensual del edge | Correr los 3 backtests de `research/` (ver `pilot/REVALIDACION_MENSUAL.md`) |

---

## 7. 🔔 RECORDATORIO REVALIDACIÓN MENSUAL

Última: **2026-05-30**. Próxima: **~2026-06-30**.
Correr (o decirle a Claude "corré la revalidación mensual"):
```bash
python research/backtest_marea_broad.py
python research/backtest_ped.py --fresh
python research/backtest_stars.py
```
Detalle: `pilot/REVALIDACION_MENSUAL.md`. (El universo se refresca solo semanal; esto es solo el edge.)

---

## 8. DECISIONES TOMADAS (contexto)

- Sistema **100% MANUAL** hasta validar track record real (Oscar lo decidió).
- Marea NO se separa — vive en `opportunity_alert/` unificada con PED y Noticias.
- Watcher, premarket scalp, PED naive: archivados con evidencia de datos (no eran rentables).
- "Estrellas" BINARIO (solo ⭐ TOP), no 3 niveles que mientan.
- Todo se valida con backtest antes de confiar. Sin falsas esperanzas.

---

**Para continuar:** decime "leé ESTADO_ACTUAL.md". eToro + salida-por-estrategia + pulidos A–E + **deploy a VM HECHOS**
(2026-05-30). Sistema vivo 24/7 en la VM. **Arrancá por la sección "6bis — PLAN DE ARRANQUE": (1) Benzinga,
(2) arreglar PED, (3) confirmar alerta pilot.**
```
```
