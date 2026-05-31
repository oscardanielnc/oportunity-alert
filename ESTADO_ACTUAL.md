# 📍 ESTADO ACTUAL DEL SISTEMA — léeme para continuar
# Última actualización: 2026-05-30 (sesión larga de reconstrucción)
# Dueño: Oscar Navarro | Asistente: Claude

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

## 6ter. PENDIENTES MENORES (no bloquean)
- **📈 Historial de equity real eToro:** snapshot diario de equity real → curva P&L histórica (plata real).
- **🧹** dead CSS/tablas premarket residuales (cosmético).

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
