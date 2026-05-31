# 📍 ESTADO ACTUAL DEL SISTEMA — léeme para continuar
# Última actualización: 2026-05-31 (tarde) — sesión: tácticas de entrada (cerradas con datos) + Fase 3 salida event-driven + protagonismo Piloto
# Dueño: Oscar Navarro | Asistente: Claude

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
