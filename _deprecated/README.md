# _deprecated/ — Código retirado (no usar)

Archivos que ya no forman parte del sistema. Se conservan por referencia/historial.

## watchlist.py + watchlist.txt (deprecados 2026-05-30)
Gestor CLI de watchlist sobre un archivo de texto. **Reemplazado**: la watchlist
ahora vive en la tabla `watchlist` de `metrics.db` (fuente única canónica), gestionada
desde el dashboard web (tab Watchlist) y servida por `/api/watchlist`. `config.json`
queda solo como seed inicial. Ver `main.py:get_watchlist()` y `REBUILD_PLAN.md §7`.
