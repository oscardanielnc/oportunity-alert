# research/ — Backtests y auditorías (no runtime)

Artefactos de investigación y validación. NO forman parte del sistema en vivo
(main.py / api / pilot no los importan). Cada script lleva un bootstrap de `sys.path`
para resolver `from utils ...` y los imports entre hermanos desde esta subcarpeta.

Correr desde la raíz del repo, ej.:
```
python research/backtest_premarket.py
python research/audit_fees.py
```

## Contenido
- `backtest_premarket.py` — validación del scanner pre-market neto de fees (universo,
  earnings, gates). Resultado clave: ver `REBUILD_PLAN.md` §2 y §10.
- `audit_fees.py` — auditoría del Watcher neto de comisiones (mató el scalping 1m).
- `super_backtest.py`, `backtest_perticker_v2.py` — motor de simulación + modelo de
  comisiones eToro (LONG $2 fijo, SHORT 0.30%). Reutilizados por los anteriores.
- `backtest_momentum_daily.py`, `backtest_portfolio_momentum.py` — validación de Marea.
- Resto `backtest_*.py` — exploraciones del Watcher (1m/5m/15m, segmentos, time-stop).
