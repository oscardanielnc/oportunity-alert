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
4. **🚀 Deploy a VM — EN PROCESO (2026-05-30).** Mecanismo SIN cambios: `bash /home/opc/oportunity-alert/deploy.sh`
   (git pull main → pip install → syntax check → systemctl restart opportunity-alert). El servicio
   corre main.py = noticias + advisor de posiciones + dashboard (thread daemon, puerto 8081).
   **DOS pasos manuales que deploy.sh NO hace:**
   (a) crear `data/etoro_config.json` en la VM (gitignored → no viaja por git pull) — ONE-TIME;
   (b) cron post-cierre para el pilot: `10 21 * * 1-5  cd /home/opc/oportunity-alert && venv/bin/python -m pilot.run_pilot`
   (el pilot NO está en main.py; sin cron, Marea/PED no generan órdenes diarias solas).
   Se agregó `yfinance` + `matplotlib` a requirements.txt (yfinance era CRÍTICO y faltaba → sin él PED no detecta earnings).
5. **🔴 PRÓXIMA SESIÓN — PRIORIDAD: Benzinga / brazo de NOTICIAS.** La mayoría de trades vendrán de
   PED+Marea, pero los de la sección de NOTICIAS son los de mayor upside ($) si se dan. Requieren:
   muchas pruebas, posibles cambios, y quizás una API de pago (Benzinga) — Finnhub free lagea 30-60min
   (cuello de botella de detección). Medir latencia real Benzinga vs Finnhub una mañana + ROI de costo.
   Todo el análisis se hace la próxima sesión.
6. **📈 Historial de rendimiento real eToro:** snapshot diario de equity real → curva P&L histórica
   (como la de Piloto pero plata real). "Desde ahora" = empieza al conectar eToro. PENDIENTE.
7. **🧹 Menor:** dead CSS/tablas premarket residuales (cosmético, no molesta).

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

**Para continuar:** decime "leé ESTADO_ACTUAL.md". eToro + salida-por-estrategia + pulidos A–E HECHOS;
deploy a VM en proceso (ver pendiente #4). **PRÓXIMA SESIÓN = PRIORIDAD Benzinga / brazo de noticias (#5).**
```
```
