# NEWS REACTION PLAN — medición del impacto real de las noticias
# Estado: 2026-06-20 | Fase: 0 (calibración histórica) — esqueleto listo, esperando datos de la VM
# Dueño: Oscar Navarro | Asistente: Claude

Este documento es la **fuente de verdad** del subsistema de *medición de reacción a noticias*.
Nace de la sesión del 20-jun-2026. Objetivo: convertir cada predicción de la IA sobre una
noticia en una hipótesis falsable y medir, con datos reales, **cuánto y qué tan rápido se
mueve la acción POR la noticia** (no por ruido del mercado), para después definir SL/TP
realistas y decidir qué noticias tradear en Binance perps.

---

## 0. PRINCIPIOS

1. **Es un ESTUDIO DE EVENTO, no un simulador de trades.** Lo central es el *retorno anormal*
   (movimiento atribuible a la noticia, neto del mercado y del sector), no el P&L de un trade.
2. **Grabar el camino, derivar las salidas después.** Registramos la trayectoria de precio
   completa de la ventana → cualquier combinación de SL/TP se calcula offline, sin comprometer
   una sola. Un dataset sirve para probar infinitos esquemas de salida.
3. **No se puede backtestear "la reacción": se mide desde ahora** — PERO sí podemos reconstruir
   retroactivamente la reacción de las noticias YA capturadas (tenemos t0 + ticker en metrics.db
   y barras históricas en Alpaca). Eso da dataset inmediato (Fase 0).
4. **Aditivo y read-only.** Nada toca el path de alertas ni la IA. Tabla nueva, thread/job nuevo,
   pestaña nueva. Si la medición explota, las alertas siguen intactas.
5. **Binance perp = venue futuro y ground truth de ejecución** (24/7, apalancado, fees bajos).
   Donde el ticker exista en perp, esa es la fuente preferida; donde no, Alpaca.

---

## 1. METODOLOGÍA — retorno anormal (cómo aislamos la noticia del ruido)

```
retorno_anormal(t) = retorno_accion(t) − β · retorno_mercado(t)
```
- `mercado` = QQQ (o SPY). MVP: β=1 → anormal = r_accion − r_QQQ ("market-adjusted").
- Refinado: restar también el **ETF de sector** (`pilot.star_score.SECTOR_ETF`) → deja el
  movimiento **idiosincrático**, lo único que la noticia pudo causar.
- β estimable con ~60 días previos (futuro; MVP usa β=1).

**Una noticia que "subió +4%" con QQQ +3.5% → +0.5% anormal = casi irrelevante.** Justo lo que
queremos detectar para aprender a ignorar tipos de noticia que no mueven.

---

## 2. MÉTRICAS POR EVENTO

Checkpoints (tiempo desde t0 = momento de la alerta): `+15m, +30m, +1h, +2h, +4h, cierre_día0,
próximo_open, +24h`. Tope duro **48h**. Zona caliente para trades rápidos = **primeras 1–6h**.

Por evento se registra:
| Métrica | Para qué |
|---|---|
| retorno crudo por checkpoint | movimiento bruto |
| **retorno anormal vs QQQ y vs sector** | movimiento atribuible / idiosincrático |
| **MFE / MAE** (crudo y anormal) | TP / SL realistas |
| tiempo al pico de reacción | horizonte realista |
| **run-up pre-t0** (retorno anormal −2h y −30m → t0) | cuán TARDE llegó nuestra alerta |
| `age_minutes` (edad de la noticia al capturar) | lag del feed |
| dirección correcta (signo IA vs realidad) | acierto direccional de la IA |
| volumen vs normal | ¿participación real o tick aislado? |
| precio cubierto / hueco de datos | honestidad (mercado cerrado overnight) |

**Agregaciones (el "oro"):**
- **score 1–10 → reacción anormal media + hit-rate direccional** (curva de calibración del score).
- **tipo_catalizador → reacción** (qué mueve, qué se ignora).
- **run-up pre-t0 por tipo** (qué tipos llegan ya priceados = ignorar / solo tendencia).

---

## 3. FUENTES DE DATOS

| Fuente | Cobertura | Historia | Uso |
|---|---|---|---|
| **Binance perp** (`ccxt.binanceusdm`) | **24/7 real** | ~40 días, pocos tickers | Forward + backfill <40d donde el ticker exista. Ground truth de ejecución. |
| **Alpaca SIP** | 4am–8pm ET (pre+after) | meses de 1m | Caballo de batalla del backfill. Gratis. |
| **yfinance** | regular + algo extended | 1m ~30d, rate-limited | Solo respaldo. |

**Matiz 24/7:** la acción US NO cotiza 24/7 (solo 4am–8pm ET). Noticia nocturna → primer precio
en el pre-market siguiente. No es límite de herramienta, es realidad del mercado; se marca el
hueco y se mide "desde el primer precio disponible tras t0". El perp de Binance es el único
instrumento realmente 24/7 → por eso es el venue.

**Cobertura Binance perp del universo (probado 2026-06-20, 15/31):**
- SÍ: `AMD ARM ASML AVGO BE COHR CRM CVX IBM MSFT MU NVDA PLTR TSM UBER`
- NO (→ Alpaca): `APP AVAV CCJ EME GOOG IONQ KTOS LMT NOC QBTS QUBT RDDT RGTI RTX SHOP TLN`
- La lista crece; el script chequea disponibilidad EN VIVO por ticker (no hardcodear).

---

## 4. HORIZONTE

Para medir **impacto** (no tendencia): la reacción se concentra en el **día 0–1**. Más allá de
2 días = ruido no relacionado. **Ventana = 24h de tiempo de mercado (≈1–2 días hábiles), tope
48h.** Por evento se bajan SOLO las barras de su ventana (t0−2h → t0+48h) + QQQ + sector ETF.
Nada de históricos largos.

---

## 5. FASES

### ▶ FASE 0 — Backfill histórico (EN CURSO)
`research/news_reaction_backfill.py` (read-only, sin runtime):
1. Lee alertas de `data/metrics_vm.db` (copia traída de la VM).
2. Por alerta: resuelve fuente (Binance perp si existe y <40d; si no, Alpaca SIP).
3. Baja ventana t0−2h → t0+48h + QQQ + sector ETF.
4. Calcula retorno crudo/anormal por checkpoint, MFE/MAE, tiempo al pico, run-up pre-t0, acierto.
5. Escupe tablas score→reacción, tipo→reacción, run-up→tipo + detalle a
   `data/news_reactions_backfill.csv`.
**Meta: validar en horas si la metodología tiene señal.**

### ▶ FASE 1 — Medición forward (motor + tabla)
- Tabla `news_reactions` en `metrics.db` (FK → `alerts.id`).
- Job **horario** idempotente desde barras: por cada ventana abierta actualiza trayectoria /
  MFE / MAE / anormal; cierra a +24h (tope 48h). Si se salta una corrida, la siguiente
  reconstruye desde barras. Thread propio con try/except — desacoplado de las alertas.

### ▶ FASE 2 — Analítica
Script en `research/` que produce las tablas de calibración (score→anormal, tipo→anormal,
hit-rate direccional, run-up por tipo). Insumo para definir SL/TP por tipo de catalizador.

### ▶ FASE 3 — Frontend: pestaña "📈 Reacciones / Trades"
Scoreboard (hit-rate direccional, anormal medio por score y por tipo) + fila por evento
(ticker, dirección, anormal%, MFE/MAE, tiempo). La noticia que lo disparó se expande **a un
click** (vía `alert_id`). Separada de "⚡ Catalizadores" para no abrumar.

### ▶ FUTURO — Ejecución Binance perps
Cuando los datos validen, cablear `ccxt.binanceusdm` (ya resuelto en `tvindicators`) solo para
los tickers con perp. Los SL/TP salen del dataset de calibración, no de la corazonada.

---

## 6. ESQUEMA `news_reactions` (Fase 1, borrador)

```
id, alert_id (FK→alerts.id), ticker, t0 (ts alerta), source (binance|alpaca),
direccion_ia, pct_estimado_ia, score_ia, tipo_catalizador, age_minutes,
price_t0, runup_pre_2h_abn, runup_pre_30m_abn,
ret_15m, ret_1h, ret_2h, ret_4h, ret_eod, ret_nextopen, ret_24h,        (crudos)
abn_15m, abn_1h, abn_2h, abn_4h, abn_eod, abn_nextopen, abn_24h,        (vs QQQ)
abn_sec_24h,                                                            (vs sector)
mfe_abn, mae_abn, mfe_raw, mae_raw, time_to_peak_min,
direccion_correcta (bool), data_gap (bool), status (open|closed), updated_at
```

---

## 7. RIESGOS / NOTAS

- **Tamaño de muestra:** se necesitan 50–100+ eventos por categoría antes de confiar. Paciencia.
- **First-touch honesto:** al *derivar* salidas, si una vela toca SL y TP, asumir el peor caso.
- **Tiempo de mercado vs reloj:** registrar ambos; no diluir la reacción con horas de mercado
  cerrado (irrelevante en perps 24/7).
- **Alpaca SIP free:** las barras recientes (<15 min) pueden dar 403/null; irrelevante para
  ventanas históricas de 24h.
