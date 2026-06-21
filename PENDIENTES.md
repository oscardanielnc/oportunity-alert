# 📌 PENDIENTES — seguimiento de tareas que esperan datos/condición

> Recordatorio AUTOMÁTICO: cron semanal en la VM corre `scoreboard_digest.py --notify` (domingo
> 23:30 UTC ≈ 18:30 Lima) y avisa por WhatsApp el estado del scoreboard + cuándo cada pendiente
> queda "listo" (readiness). Así no hay que acordarse a mano.

---

## #2 — Convicción data-driven (BASE LISTA, falta cablear)
**Estado:** `utils/scoreboard.expected_impact(db, arm, cat, min_n=8)` ya existe (commit 174b9ea) —
devuelve el impacto empírico (mediana retorno-al-exit/MFE, win, n) por categoría, o `None` si poca
muestra.

**Qué falta:** cablearlo en `earnings/strategies.py` (convicción del candidato) y
`utils/signal_score.py` (gate de SMS) para que usen el impacto real medido **con fallback** al proxy
actual (% de reacción para PED / `PRERUN_PROJ` para pre-run-up).

**Condición para activarlo (la avisa el cron):** que una (arm, categoría) acumule **n ≥ 8 señales
cerradas**. El digest semanal marca "🔧 #2 listo (n≥8): arm/cat …" cuando se cumple.

## #3 — Lectura del scoreboard (solo necesita datos, nada que construir)
**Qué hacer:** cuando haya señales cerradas, leer el detalle:
```
scp opc@213.35.121.9:/home/opc/oportunity-alert/data/metrics.db data/metrics_vm.db
python research/scoreboard_digest.py --db data/metrics_vm.db --days 7
```
**Cuándo:** las primeras señales (Market Movers/Noticias) cierran ~48h; PED/pre-run-up ~5-8d. El
cron semanal ya manda un resumen; este paso es para el detalle completo (tablas por categoría/fuente,
candidatos a suprimir, cestas de market movers).

---

## Refinamientos de Earnings (cuando haya validación/datos)
- Más estrategias solo si pasan el bar (Short-PED y Cañonazo ya DESCARTADOS por falta de edge/lookahead).
- POST-earnings "reacción en vivo" (beat→long / miss→short / inline→skip, condicionado a tamaño):
  idea de Oscar 2026-06-21 — **a backtestear** (calendario histórico + velas 1m del día del earning).
