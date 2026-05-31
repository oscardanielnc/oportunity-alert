# Piloto Momentum Swing — Especificación

Paper trading de momentum swing diario LONG. Genera señales, las ejecuta en un
portafolio simulado, alerta por WhatsApp y muestra todo en el dashboard.
**No ejecuta órdenes reales** — Oscar las ejecuta manualmente en eToro.

## Por qué existe

El scalping intradía resultó net-negativo tras comisiones eToro (ver auditoría).
El momentum swing diario sí sobrevive comisiones. El piloto valida el edge **en vivo
y hacia adelante**, lo que elimina el sesgo de supervivencia del backtest.

## Las 5 reglas

1. **Universo por reglas:** stocks líquidos NASDAQ/NYSE (top ~80 por dollar-volume),
   reconstruido semanalmente. NO se eligen tickers a mano.
2. **Régimen:** solo LONG si `precio > SMA200` **y** `QQQ > su SMA200` (no operar en bear).
3. **Entrada:** breakout = nuevo máximo de cierre de 50 días. Prioriza por fuerza
   relativa (momentum 126 días).
4. **Concentración:** máximo 5 posiciones (K=5), tamaño = equity/5 ajustado por
   volatilidad (menos $ a los de ATR% alto).
5. **Salida:** trailing chandelier = `máx_high_desde_entrada − 4×ATR`. Sin target fijo.

**Ejecución:** señal al cierre del día D → orden al OPEN del día D+1 (sin lookahead,
igual que operaría Oscar tras recibir la alerta).

## Componentes

| Archivo | Rol |
|---------|-----|
| `pilot/universe.py` | Construye/cachea universo por liquidez → `data/pilot_universe.json` |
| `pilot/momentum_signals.py` | Fetch barras diarias + indicadores + breakout + ranking + chandelier |
| `pilot/paper_portfolio.py` | Estado persistente del portafolio paper → `data/pilot_state.json` |
| `pilot/run_pilot.py` | Runner diario: ejecuta pendientes, decide salidas/entradas, alerta, dashboard |
| `pilot/backfill.py` | Replica el motor sobre historia: valida + siembra track record |
| `api/app.py` `/api/pilot` | Endpoint que sirve `data/pilot_dashboard.json` |
| dashboard pestaña 🚀 Piloto | Equity, retorno, órdenes de mañana, posiciones, trades cerrados |

## Comandos

```bash
python -m pilot.universe              # (re)construir universo
python -m pilot.backfill              # validar + sembrar track record (~14 meses)
python -m pilot.run_pilot             # correr el día (alerta WhatsApp + dashboard)
python -m pilot.run_pilot --no-alert  # sin WhatsApp (consola + estado)
python -m pilot.run_pilot --rebuild-universe
```

## Operación diaria

Correr `run_pilot` **una vez al día tras el cierre de USA** (16:00 ET / 15:00 Lima).
Recibes alerta: "mañana al abrir comprar X, vender Y". Ejecutas en eToro al open.

Cron en la VM Oracle (post-cierre, ~16:10 ET = 21:10 UTC, lun-vie):
```cron
10 21 * * 1-5  cd /ruta/opportunity_alert && /usr/bin/python3 -m pilot.run_pilot >> data/pilot.log 2>&1
```

## Configuración

- `PILOT_CAPITAL` (env, default 10000) — capital inicial del paper.
- `TWILIO_TO` (env) — número destino WhatsApp. **No está en local**; configurar en la VM.
- Parámetros K/breakout/mult/vol_target en `paper_portfolio.py` y `momentum_signals.py`.

## Estado del backfill inicial (2026-05-29)

~14 meses simulados, 80 tickers: equity $10k→$32.9k (+229%), WR 64%, mensual prom
+11% / mediana +8%, MaxDD −15%. **Inflado por bull + universo con ganadores actuales;
la validación real es el tramo forward en vivo.** Lectura realista forward: ~1.5-3%/mes
promedio, DD −15/−25%.

## Pendiente / mejoras

- Deploy a VM Oracle con cron (el sistema real corre ahí, no en local).
- Tras 1-2 meses en vivo: comparar equity real-forward vs expectativa del backfill.
- Opcional: refrescar universo automáticamente y registrar cambios de membresía.
