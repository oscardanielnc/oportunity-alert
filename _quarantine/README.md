# _quarantine/ — Auto-ejecución (NO activar)

Módulos de **ejecución de órdenes reales** en eToro. El proyecto opera en modo
**MANUAL** hasta validar el track record. No activar hasta cumplir las pruebas de §9
de `REBUILD_PLAN.md`.

## ⚠️ HALLAZGO DE SEGURIDAD (2026-05-30) — PENDIENTE DE RESOLVER

`utils/etoro_trader.py` (open/close orders, soporta `environment: "real"`) y
`utils/position_lock.py` **NO se movieron aún a esta carpeta** porque **`api/app.py`
los importa y los usa** — la auto-ejecución está cableada al frontend del Watcher:

- `api/app.py:29-34` → importa `open_position`, `close_position`, `get_available_capital`.
- `api/app.py:~626` → `if signal in ("ENTRAR_LONG","ENTRAR_SHORT")` ejecuta la orden.
- Endpoints `/api/position-lock` y `/api/position-lock/release`.

Esto **contradice** el mandato "eToro READ-ONLY, NUNCA ejecuta órdenes" de CLAUDE.md.

**Mitigación actual:** `data/etoro_config.json` NO existe → `etoro_trader` no puede
cargar credenciales → una orden fallaría. No hay peligro inmediato, pero el camino
de ejecución está vivo en el código.

## Pasos para cuarentena correcta (antes de mover los archivos)
1. En `api/app.py`: desconectar la rama `ENTRAR_LONG/ENTRAR_SHORT` → en lugar de
   `etoro_open(...)`, que solo registre/alerte (modo manual). El Watcher pasa a
   advisor-only (MANTENER/CERRAR sobre posiciones abiertas), §5 de REBUILD_PLAN.
2. Mantener `position_lock` si se sigue usando para estado read-only, o moverlo
   junto con el trader si se elimina la rama de ejecución.
3. Una vez sin referencias de ejecución, mover `etoro_trader.py` (+ `position_lock.py`)
   aquí y validar que `api/app.py` arranca.
