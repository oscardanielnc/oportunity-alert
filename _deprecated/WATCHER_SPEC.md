# Watcher Tecnico — Especificacion v3
# OportunityAlert — actualizado 2026-05-27

## Que hace

Monitorea tickers en tiempo real barra a barra (1m/5m/15m) y emite senales
tecnicas accionables: ENTRAR_LONG, ENTRAR_SHORT, CERRAR_LONG, CERRAR_SHORT,
MANTENER, PRECAUCION, ESPERAR.

El sistema NO ejecuta ordenes — solo informa. Las ordenes las ejecuta Oscar en eToro.

---

## Motor de senales v3 (utils/signal_engine.py)

### Scoring (max 9 pts)

| Criterio | Pts | Descripcion |
|----------|-----|-------------|
| L1/S1 | 1 | Bollinger Band extremo (precio en/fuera banda) |
| L2/S2 | 1 | RSI extremo (<35 bull / >65 bear) |
| L3/S3 | 1 | MACD histogram mejorando/deteriorando |
| L4/S4 | 1 | Precio en soporte/resistencia (ATR proximity) |
| L6/S6 | 1 | Volumen confirmando (>1.2x promedio) |
| Patrones | 0-4 | 16 patrones de vela con pesos (ver tabla) |

### 16 patrones de vela

| Patron | Peso | Tipo |
|--------|------|------|
| Bullish/Bearish Engulfing | 3 | Fuerte reversal |
| Morning/Evening Star | 3 | Fuerte reversal |
| Three White Soldiers/Black Crows | 2 | Continuacion fuerte |
| Hammer / Shooting Star | 2 | Reversal moderado |
| Piercing Line / Dark Cloud Cover | 2 | Reversal moderado |
| Dragonfly / Gravestone Doji | 2 | Indecision con direccion |
| Bullish/Bearish Harami | 1 | Debil, necesita confirmacion |
| Inverted Hammer / Hanging Man | 1 | Debil, necesita confirmacion |

Score de patrones cappado en 4 pts (los mas fuertes dominan).

### Momentum filter

Si el precio movio +0.5% en los ultimos 10 bars (uptrend fuerte), los
patrones bajistas pierden hasta 2 pts de su score (y viceversa en downtrend).
Evita senales falsas por patrones debiles contra la tendencia principal.

### HTF (Higher Timeframe) bias

| TF del Watcher | HTF usado |
|----------------|-----------|
| 1m | 5m |
| 5m | 15m |
| 15m | 1h |

HTF trend calculado via EMA20 posicion + MACD histogram (requiere >=20 barras HTF).

| HTF Trend | Umbral LONG | Umbral SHORT |
|-----------|-------------|--------------|
| BULLISH | 4/9 | 8/9 (muy dificil entrar contra tendencia) |
| NEUTRAL | 5/9 | 5/9 |
| BEARISH | 8/9 | 4/9 |

### Confirmacion y direction lock

- Confirmacion: 2 scans consecutivos con la misma senal
- Direction lock: tras ENTRAR_LONG, bloquea ENTRAR_SHORT por 5 scans (y viceversa)
- Se libera al confirmar CERRAR/ESPERAR o tras 5 scans

### Umbrales de salida (en posicion)

| Condicion | Accion |
|-----------|--------|
| Score opuesto >= 3 | CERRAR |
| RSI < 30 con posicion SHORT | CERRAR_SHORT inmediato (RSI override) |
| RSI > 70 con posicion LONG | CERRAR_LONG inmediato (RSI override) |
| Score opuesto >= 2 | PRECAUCION (avisa, no sale) |

### Time-stop

Si la posicion lleva demasiados scans CON movimiento adverso >= 0.5%:

| TF | Scans max | Tiempo aprox |
|----|-----------|-------------|
| 1m | 30 | 30 min |
| 5m | 12 | 60 min |
| 15m | 6 | 90 min |

El time-stop NO actua si el adverso es menor a 0.5% (permite pullbacks normales).
Cuando dispara: bypasa confirmacion de 2 scans, emite CERRAR inmediatamente.
Se resetea al cambiar position_state.

---

## Feed de datos

- IEX (real-time): solo en horario regular 9:30am-4pm ET
- SIP (15-min delay): pre-market, after-hours, y fallback cuando IEX esta desactualizado
- Dashboard muestra pill de sesion en el topbar con WR historico y modo de feed

---

## Umbrales de entrada por sesion (SESSION_THRESHOLDS en app.py)

El motor filtra senales ENTRAR segun el horario. Backtest mostro que el pre-market
temprano (03-08h Lima, datos SIP con 15min de delay) tiene WR significativamente menor.

| Sesion | Hora Lima | WR backtest | Umbral ENTRAR | Feed |
|--------|-----------|-------------|---------------|------|
| REGULAR | 08:30-15:00 | 62% | 5/9 | IEX real-time |
| PRE-MARKET temprano | 03:00-07:59 | 45% | **6/9** | SIP 15min delay |
| PRE-MARKET (apertura) | 08:00-08:29 | ~58% | 5/9 | SIP 15min delay |
| AFTER-HOURS | 15:00-20:00 | 56% | 5/9 | SIP 15min delay |

Nota: los WR por hora son aproximados (promedio de 1 semana, 10 tickers).
En el futuro se refinaran con WR por franja horaria especifica.

Para editar umbrales: modificar SESSION_THRESHOLDS en api/app.py.
El dashboard refleja automaticamente el umbral activo via /api/health.

### Badge de sesion en el topbar

Pill visible en todo momento con:
- Nombre de sesion + color (verde=REGULAR, amarillo=PM temprano, azul=AFTER-HOURS)
- WR historico del backtest para esa sesion
- Modo de feed (IEX / SIP 15min delay)
- Aviso amarillo "umbral 6/9" cuando aplica restriccion adicional

SESSION_INFO en dashboard.html es la tabla editable con los valores por sesion.

---

## Backtest resultados (2026-05-27, super_backtest.py)

10 tickers: NVDA, AMD, MU, PLTR, IONQ, QBTS, APP, RDDT, AVGO, MSFT

| TF | Historia | Trades | WR | P&L Total | Peor trade |
|----|----------|--------|----|-----------|----|
| 1m | 1 semana | 189 | 60% | +11.44% | -2.07% |
| 5m | 6 semanas | 189 | 57% | -1.13% | -3.51% |
| 15m | 12 semanas | 125 | 50% | +0.04% | -5.25% |

Recomendacion: usar 1m. El motor fue calibrado para esta granularidad.

### Mejores tickers en 1m (1 semana)

| Ticker | WR | P&L | Notas |
|--------|-----|-----|-------|
| IONQ | 75% | +5.28% | El mejor — alta volatilidad, senales claras |
| QBTS | 56% | +1.75% | Solido |
| PLTR | 47% | +1.48% | WR bajo pero ganancias grandes |
| RDDT | 64% | +1.28% | Fiable |
| APP | 80% | +0.92% | WR excelente |
| AVGO | 68% | +0.66% | Estable |
| MU | 64% | +0.73% | Bueno |
| MSFT | 50% | -0.25% | Demasiado lento para 1m scalping |
| AMD | 70% | -0.28% | WR alto pero ganancias pequenas |
| NVDA | 52% | -0.12% | Mucho ruido en 1m |

---

## API /api/watcher/status — campos clave

```
signal               ESPERAR / ENTRAR_LONG / ENTRAR_SHORT / CERRAR_LONG / CERRAR_SHORT / MANTENER / PRECAUCION
score_long           0-9
score_short          0-9
htf_trend            BULLISH / BEARISH / NEUTRAL
patterns_bull        lista de nombres de patrones alcistas detectados
patterns_bear        lista de nombres de patrones bajistas detectados
direction_lock_remaining  scans restantes de direction lock
direction_lock_dir        LONG / SHORT / null
position_scans_held  cuantos scans lleva la posicion abierta
position_adverse_pct % movimiento adverso acumulado (null si sin posicion)
timestop_max_scans   30 / 12 / 6 segun TF
timestop_adverse_pct 0.5
feed_mode            IEX / SIP
market_session       REGULAR / PRE_MARKET / AFTER_HOURS / CLOSED
bar_lag_min          minutos desde la ultima vela recibida
interval             1m / 5m / 15m
```

---

## Scripts de backtest

```bash
# Backtest MU hoy con todas las mejoras v3
python backtest_today.py

# Backtest multi-ticker: 7 tickers x 5 dias
python backtest_multi.py

# Super backtest: 10 tickers x 3 TFs
python super_backtest.py

# Backtest comparativo: todo el dia vs solo horario regular
python backtest_regular_vs_all.py
```

---

## Pendientes (2026-05-28+)

### Alta prioridad
1. Deploy a VM Oracle (bash deploy.sh) — Watcher v3 solo en local por ahora
2. Calibrar umbrales para 5m y 15m (actualmente optimizado para 1m)
3. Mostrar alertas de noticias del ticker en el Watcher card (datos disponibles en /api/alerts?ticker=X)

### Media prioridad
4. Analisis de sector — SPY/QQQ como contexto macro para reforzar/debilitar senales
5. Ajustar parametros time-stop con mas historia (probar 25 scans / 0.4% adverso)
6. Sugerir IONQ y QBTS como defaults en el dashboard (mejores segun backtest)
7. WR por franja horaria especifica — refinar SESSION_INFO con datos de mas semanas
   (actualmente usa promedios amplios: 03-08h=45%, REGULAR=62%, AH=56%)
