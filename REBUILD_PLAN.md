# REBUILD PLAN — OportunityAlert + Marea unificados
# Estado: 2026-05-30 | Fase: DOCUMENTACIÓN (aún sin tocar código de runtime)
# Dueño: Oscar Navarro

Este documento es la **fuente de verdad** de todo lo pendiente. Nace de la auditoría
del 30-may-2026 y del backtest de validación del pre-market. Mientras no se implemente,
nada de esto está hecho — son decisiones tomadas + tareas pendientes.

---

## 0. PRINCIPIOS DE LA RECONSTRUCCIÓN

1. **Un solo proyecto.** Marea + Noticias + Pre-market + Posiciones viven en
   `opportunity_alert/` y se complementan. (Revierte la decisión de separar Marea.)
2. **Manual primero.** Nada ejecuta órdenes reales hasta pasar todas las pruebas de
   validación. El auto-trader queda en cuarentena.
3. **Código antes que IA.** Se mantiene la regla de costos.
4. **Validar antes de construir.** Cada brazo necesita backtest neto-de-fees antes de
   confiarle capital. (Watcher murió así; pre-market se está validando así.)
5. **El edge del madrugón está en el CATALIZADOR y el UNIVERSO, no en la sofisticación
   técnica de la señal.** (Demostrado por backtest — ver §2.)

---

## 1. ARQUITECTURA Y ESTRUCTURA DE CARPETAS

### Estado actual (problema)
Raíz plana con ~12 `backtest_*.py`, scripts de viz, auditorías y módulos de
auto-ejecución mezclados con el runtime. CLAUDE.md describe una versión vieja.

### Estructura objetivo
```
opportunity_alert/
├── main.py, config.json              # runtime
├── sources/  filters/  alerts/       # núcleo noticias (se queda)
├── utils/                            # infra compartida + lógica viva
├── pilot/                            # MAREA (se queda, se integra al frontend)
├── api/                             # frontend unificado
├── research/        <- NUEVO        # todos los backtest_*, super_backtest, audit_fees,
│                                      viz_*, show_history, test_alert, backtest_premarket
└── _quarantine/     <- NUEVO        # auto-ejecución NO cableada (ver §6)
    ├── etoro_trader.py
    └── position_lock.py
```

### Tareas
- [x] Crear `research/` y mover 16 artefactos de investigación (+ bootstrap sys.path, validado).
- [~] Crear `_quarantine/` (hecho) — **archivos NO movidos**: `etoro_trader`/`position_lock`
      los usa `api/app.py`. Mover requiere antes desconectar la ejecución en app.py (ver §9).
- [~] `CLAUDE.md`: banner "ACTUALIZACIÓN 2026-05-30" añadido (manual, watcher, premarket,
      watchlist, estructura). Falta reescritura completa del cuerpo v2.1 a los 4 brazos.
- [ ] `metrics.db` como log único de los 4 brazos → genera el track record manual
      que habilita (o no) la automatización futura.
- [ ] Confirmar: sistema real corre en VM Oracle 24/7; local = solo frontend.

---

## 2. PRE-MARKET (brazo madrugada) — ❌ ARCHIVADO 2026-05-30 (sin edge validado)

**VEREDICTO FINAL (Fase B, backtest_premarket_catalyst):** el madrugón intradía NO tiene edge
replicable — solo MU lo sostenía (1 trade excepcional). ARCHIVADO como estrategia de trading:
`premarket_scanner.py` → `_deprecated/`, thread fuera de main.py, endpoints + pestaña frontend
removidos. RESCATADO: filtro de earnings → `utils/earnings_calendar.py` (avisa en Noticias).
El valor de earnings está en PED multi-día (ver §nueva PED), no en el pop intradía.

### (histórico — lo que se probó antes de archivar)

### Hallazgos del backtest (12-16 sem, neto fees) — qué quedó demostrado
- La señal técnica cruda NO tiene edge (gross ~0%, net-negativo).
- **PUMP/SMALL (QBTS, IONQ, RGTI, QUBT, RDDT, APP, BBAI) = net-negativo limpio. NO tradear.**
- **LARGE_CAP = net-positivo SOLO si excluyes earnings.** −0.29%/trade → +1.35%/trade gross
  al quitar los días de earnings (SHOP −9%/−19%, AMD +19%-gap).
- **Los gates técnicos (RVOL, code_score≥6, early-fade) NO funcionan — EMPEORAN el agregado.**
  SHOP tenía el RVOL/code más alto del grupo y fue el peor desastre. La premisa
  "mucho volumen + convicción = buena señal" queda INVALIDADA para large-caps.
- Salida same-day > next-day. SHORT NO VIABLE (re-confirma watcher audit).

### Filtros que SÍ necesitamos (en orden de impacto)
1. **Exclusión por calendario de earnings** (#1, no es técnico). No entrar si el ticker
   reportó esa madrugada o la tarde anterior. — VALIDANDO sobre todo el universo.
2. **Clasificación de catalizador (IA/noticias)**: upgrade / contrato / guidance-raise /
   sector-momentum = sí; earnings-gap = no.
3. **DROPEAR los gates técnicos como pass-filters.** Volumen solo como piso de liquidez.
4. **Cap de overextensión**: saltar si premarket ya > ~10% (move ya hecho).

### Tareas
- [x] Restringir universo del scanner a LARGE_CAP 24/5 (`LARGE_CAP_24X5`, RESTRICT_TO_LARGE_CAP).
- [x] Integrar earnings-calendar (Finnhub) como exclusión dura (`_has_earnings_blackout`).
- [x] Quitar `code_score>=6` como gate de paso (se loguea, ya no descarta).
- [ ] Quitar/relajar early-fade y RVOL como componentes que aún pesan en code_score
      (ya no gatean, pero siguen en el cálculo informativo — limpiar cuando se rehaga el SMS).
- [ ] Reorientar el prompt IA a CLASIFICAR catalizador (no a re-puntuar técnico).
- [ ] Cap de overextensión (saltar si premarket > ~10%).
- [ ] Forzar salida same-day en la lógica de seguimiento.
- [ ] Reemplazar Finnhub free por fuente de earnings fiable (free se saltó SHOP 11-feb).
- [ ] **PENDIENTE VALIDACIÓN:** earnings filter sistemático + muestra más grande (MU pesa mucho).

---

## 3. NOTICIAS (brazo catalizador — OpportunityAlert original)

### Se queda (funciona): conviction_gates, event_gate, sources EDGAR/Finnhub, delayed_alerts.

### Tareas
- [x] **Fix bug keyword filter:** upgrades concretos (raises price target, upgraded to buy…)
      promovidos a Tier-1 → 1 sola keyword pasa a IA. Validado con caso MU. Los vagos
      (strong buy, initiated coverage, outperform) quedan en Tier-2.
- [ ] **Fuente de noticias rápida** (Benzinga API / RSS de wires: Business Wire,
      GlobeNewswire, PR Newswire). Finnhub free lagea 30-60min → inservible para el madrugón.
      DECISIÓN/COSTO PENDIENTE.
- [ ] Etiqueta "ejecutable ahora (24/5)" en las alertas según universo.
- [ ] Mantener MANUAL (read-only).

---

## 4. MAREA (brazo continuación)

### Se queda: validado por backtest (~16%/año neto, DD mejor que B&H).

### Tareas
- [ ] Integrar como sección del frontend "📋 Acciones de hoy" (diario, automático):
      qué comprar / vender / mantener.
- [ ] Deploy a VM Oracle con cron post-cierre (`10 21 * * 1-5`). (Estaba pendiente.)
- [ ] Confirmar config en VM (PILOT_CAPITAL, TWILIO_TO).
- [ ] Reusar motor `pilot/` tal cual. NO extraer a repo propio (decisión revertida).

---

## 5. WATCHER (signal_engine) — reorientar, no matar

### Veredicto: como motor de trades intradía MUERE por fees (1m scalping net-negativo,
auditado). PERO sus indicadores se RESCATAN como advisor de salida de posiciones.

### Qué se reutiliza, dónde y cómo
| Pieza | Se reutiliza en | Cómo |
|-------|-----------------|------|
| `signal_engine.py` (RSI/EMA/MACD/patrones/HTF/time-stop) | §6 Posiciones | Correr el scoring sobre las barras de cada posición ABIERTA → emitir MANTENER/CERRAR. Sin generar entradas, sin ejecutar. Sin fee por-scan = el problema de comisiones desaparece. |
| `WATCHER_SPEC.md` | referencia | Documentación del motor. Se conserva. |
| Lógica de scalping intradía | nada | Descartar la premisa. |

### Tareas — DECISIÓN HONESTA (2026-05-30): eliminar TODO el Watcher, sin advisor técnico
El motor de señales no está validado para salidas; Posiciones usará las reglas de riesgo
YA existentes del PositionTracker (stop/T1/retroceso). NO se construye advisor con indicadores.
- [x] Pestaña Watcher + panel removidos del dashboard.
- [x] BACKEND removido de app.py: endpoints `/api/watcher/*`, `/api/ticker-params/*`,
      `/api/calibrated-tickers`, funciones del thread, imports, tablas de calibración. 16 rutas.
- [x] Módulos `signal_engine.py` + `ticker_params_store.py` + `segment_config.py` + `WATCHER_SPEC.md`
      → `_deprecated/`. Research backtests del watcher guardados con try/except (no rompen premarket).
- [x] UI de calibración (config-form, stat Calibrados, badge cal, fetch calibrated-tickers) removida.
- [x] Purgadas ~1700 líneas de JS del watcher en dashboard.html (76KB) — JS válido (node --check),
      sin refs colgantes. Limpiados: routing/polling, globals, calibración JS.
- [x] CSS: 86 reglas del watcher eliminadas (28KB→19KB, balanceado/válido). Conservadas ~50 clases
      ambiguas/dinámicas (ej. `.ec.apertura` del premarket) para no romper estilos vivos.
- [x] Imports huérfanos quitados de app.py (threading, time). app.py 16 rutas, compila.
- [ ] Posiciones: surfacar reglas de riesgo existentes (PositionTracker) — NO advisor técnico. (futuro)

---

## 6. POSICIONES (Posiciones / PositionTracker)

### Se queda: eToro read-only, PositionTracker ya detecta cierres y stops.

### Tareas
- [ ] Veredicto diario hold/sell por posición usando indicadores del watcher rescatado (§5).
- [ ] Surfacear en frontend "💼 Tus posiciones".
- [ ] Mantener alertas existentes (stop en riesgo, T1, retroceso desde pico).

---

## 7. WATCHLIST

### ⚠️ Hallazgo: watchlist FRAGMENTADA en 3 fuentes (debe unificarse)
`config.json` (primary/extended/crypto) + `watchlist.txt` (vía `watchlist.py`) + tabla
en `metrics.db` (lo que sirve el API/frontend). Fuente de verdad para frontend = `metrics.db`.

### Tareas
- [x] Fuente única del universo 24/5 = `LARGE_CAP_24X5` en `utils/premarket_scanner.py`.
- [x] API `/api/watchlist` anota `tradeable_24x5` por ticker (validado: MU/NVDA sí, QBTS/BBAI no).
- [x] Frontend `dashboard.html`: badge "24/5" en la tabla de watchlist; quitada toda la UI
      de auto-trade (toggle, banner position-lock, funciones JS, CSS, badges 🤖 del log).
- [x] Unificar watchlist → `metrics.db` canónica (main.py podado), `config.json` seed,
      `watchlist.py/txt` → `_deprecated/`. Validado: sin importadores residuales.

---

## 8. FRONTEND UNIFICADO (api/)

### Objetivo: un dashboard, 3 secciones alimentadas por los brazos.
```
📋 ACCIONES DE HOY (Marea)   ⚡ CATALIZADORES (noticias+premarket)   💼 POSICIONES (advisor)
   diario/automático             tiempo real / manual                 diario/automático
```

### Tareas
- [ ] Sección "Acciones de hoy" (Marea, `/api/pilot` existente — extender).
- [ ] Feed live de catalizadores con etiqueta 24/5.
- [ ] Sección posiciones con veredicto hold/sell.

---

## 9. AUTO-EJECUCIÓN (futuro — cuarentena)

⚠️ **CORRECCIÓN (30-may):** `etoro_trader.py` + `position_lock.py` **SÍ están cableados —
en `api/app.py`** (líneas 29-34 importan open/close; ~626 ejecuta en `ENTRAR_LONG/SHORT`).
Esto contradice "eToro READ-ONLY" de CLAUDE.md. Solo está inerte porque falta
`etoro_config.json`. Ver `_quarantine/README.md`.

### Tarea de seguridad PRIORITARIA — ✅ HECHA (2026-05-30)
- [x] Desconectada la rama `ENTRAR_LONG/SHORT` → `etoro_open` en `api/app.py`: borrada
      `_execute_auto_trade`, los endpoints `/api/watcher/auto-trade` y `/api/position-lock(/release)`,
      el import de `etoro_trader`/`position_lock`, y los campos auto_trade/etoro_* de start/status.
- [x] `etoro_trader.py` + `position_lock.py` movidos a `_quarantine/`. Validado: app.py importa
      limpio (24 rutas), sin refs residuales, ningún módulo runtime los importa.

Sistema **100% manual a nivel de código**. NO reactivar hasta validar track record (§9 pruebas).

### Definir las "pruebas que debe pasar" antes de automatizar (pendiente):
- [ ] N de trades manual suficiente con net-positivo real (no backtest).
- [ ] Demostrar que la clasificación de catalizador filtra los earnings-traps en vivo.
- [ ] Criterios de tamaño de posición y máximo riesgo.

---

## 10. VALIDACIONES / DECISIONES ABIERTAS
- [x] Earnings filter sistemático (Finnhub) — RESULTADO 30-may: LARGE_CAP sin earnings
      = +0.68% net/trade (vs −0.39% con earnings); los earnings promedian −9.9%. CONFIRMA
      que el filtro es NECESARIO. PERO dos asteriscos: (a) **el edge está concentrado en MU**
      (quita los 3 trades de MU y large-cap-ex-earnings vuelve a ~break-even/negativo) →
      NO es suficiente para declarar rentable; (b) **Finnhub free es INCOMPLETO** — se
      saltó el earnings de SHOP 11-feb (−8.9% leaked). Producción necesita calendario fiable.
- [ ] Ampliar muestra del pre-market (18 trades es poco; MU domina el resultado).
- [ ] Validar que la clasificación de catalizador confirma el edge MÁS ALLÁ de MU.
- [ ] Elegir fuente fiable de earnings calendar (Finnhub free falla).
- [ ] ¿Pagar Benzinga? (depende del ROI de detección — medido parcialmente con MU).
- [ ] Diseño de la clasificación de catalizador (qué tipos entran, cuáles no).
- [ ] Velas 1m premarket reales (pendiente habilitar extended-hours en TradingView).
```
```
