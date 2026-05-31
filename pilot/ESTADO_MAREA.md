# 🌊 MAREA — Estado del proyecto (2026-05-29)

> **Marea**: sistema de trading sistemático de momentum swing diario.
> La metáfora: sigues la *marea* del mercado (la tendencia), no la predices.
> Te subes a las olas ya en marcha y las sueltas cuando rompen.

Decisión (2026-05-29): **Marea será un programa SEPARADO de OpportunityAlert.**
Hoy vive dentro de `opportunity_alert/pilot/` reusando infra; mañana se extrae a su
propio repo/deploy.

---

## Por qué nace (el camino honesto)

OpportunityAlert se basaba en **predecir catalizadores de noticias** para comprar antes
del salto. Lo auditamos y concluimos, con datos:

| Hallazgo | Veredicto |
|---|---|
| Alertas de noticias predecían el movimiento | No funciona (llegas tarde al gap, sesgo de supervivencia) |
| Watcher scalping intradía 1m (eToro) | **Net-NEGATIVO** tras comisiones (−34%/2sem) |
| Shorts en eToro (0.30%/trade) | No viables |
| **Momentum swing diario LONG** | **Sobrevive comisiones** → base de Marea |

El edge real: el momentum/trend-following es la anomalía más robusta y documentada.
No es secreta — funciona porque es psicológicamente difícil de seguir. El edge de Oscar
es la **disciplina sistemática** que elimina la emoción.

---

## Qué hace Marea (las 5 reglas)

1. **Universo por reglas:** top ~80 acciones más líquidas NASDAQ/NYSE (dollar-volume),
   refresco semanal. Sin elegir a mano (mata el sesgo de supervivencia).
2. **Régimen:** solo LONG si `precio > SMA200` **y** `QQQ > SMA200`.
3. **Entrada:** breakout = nuevo máximo de cierre de 50 días, rankeado por momentum 126d.
4. **Concentración:** máx 5 posiciones, tamaño = equity/5 ajustado por volatilidad.
5. **Salida:** trailing chandelier `máx_high − 4×ATR`. Sin target fijo.

Señal al **cierre** del día D → orden al **open** del día D+1 (paper; Oscar ejecuta en eToro).

---

## Resultados de validación (backtest + backfill, con comisiones)

| Test | Retorno | Riesgo | Nota |
|---|---|---|---|
| Backtest portafolio (3.6 años) | CAGR ~+44-57% | MaxDD −19% a −32% | vs QQQ +15.6% CAGR |
| Backfill ~14 meses (motor del piloto) | +229% | MaxDD −15% | WR 64%, mensual prom +11% |

**Lectura honesta:** inflado por bull histórico + universo con ganadores actuales + sin
slippage. **Realista forward: ~1.5-3%/mes promedio, DD −15/−25%.** NO es 10%/mes sostenido.
La validación verdadera = el tramo **forward en vivo** (paper), que no tiene sesgo.

---

## Lo construido (hoy vive en pilot/)

| Archivo | Rol |
|---|---|
| `universe.py` | Universo por liquidez → `data/pilot_universe.json` |
| `momentum_signals.py` | Indicadores + breakout + ranking + chandelier + macro |
| `paper_portfolio.py` | Estado paper → `data/pilot_state.json` |
| `run_pilot.py` | Runner diario + alertas WhatsApp + dashboard |
| `backfill.py` | Replica el motor sobre historia (valida + siembra) |
| `explain.py` / `viz_funnel.py` | Diagnóstico y gráficos explicativos |
| `PILOT_SPEC.md` | Especificación técnica + comandos |
| API `/api/pilot` + pestaña 🚀 dashboard | Visualización |

Análisis y backtests previos (en raíz del repo): `audit_fees.py`,
`backtest_momentum_daily.py`, `backtest_portfolio_momentum.py`, `viz_pilot.py`.

---

## Plan para mañana

1. **Extraer Marea a programa separado:** nuevo repo/carpeta `marea/`, con su propia
   infra (data fetch, alertas, dashboard mínimo, estado). Decidir qué se copia y qué se
   comparte. Renombrar `pilot/` → estructura propia.
2. **Correr en local unos días** con `python -m pilot.run_pilot --no-alert` para verlo
   evolucionar (Oscar comprará en la noche por debajo del cierre, no market-on-open).
3. Pendiente de fondo: validar el tramo forward vs la expectativa del backfill.
4. Más adelante: deploy con cron, configurar `TWILIO_TO`.

## Comandos rápidos

```bash
python -m pilot.run_pilot --no-alert   # corre el día (consola + estado + dashboard)
python -m pilot.explain                # ver qué evalúa hoy, con números
python -m pilot.viz_funnel             # gráfico del embudo de selección
python -m pilot.backfill               # re-validar / re-sembrar
```
