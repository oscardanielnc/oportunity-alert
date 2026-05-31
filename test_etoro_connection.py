"""
Prueba aislada de la conexion eToro READ-ONLY.

Uso:
    1. Copia etoro_config.example.json -> data/etoro_config.json
    2. Rellena public_key y user_key con tus llaves reales de eToro.
    3. python test_etoro_connection.py

NO ejecuta ordenes. Solo lee portfolio + cash. Output 100% ASCII (cp1252 Windows).
"""
import json
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "data" / "etoro_config.json"
PLACEHOLDERS = {"PEGA_AQUI_TU_API_KEY", "PEGA_AQUI_TU_USER_KEY", "", None}


def _line(c="-", n=60):
    print(c * n)


def main() -> int:
    _line("=")
    print("  PRUEBA DE CONEXION eToro (READ-ONLY)")
    _line("=")

    # 1. Existe el config?
    if not CONFIG_PATH.exists():
        print(f"[FALLO] No existe {CONFIG_PATH}")
        print("        Copia etoro_config.example.json a data/etoro_config.json")
        print("        y rellena public_key / user_key.")
        return 1

    # 2. Llaves rellenadas?
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[FALLO] etoro_config.json no es JSON valido: {e}")
        return 1

    for field in ("public_key", "user_key", "api_base"):
        if cfg.get(field) in PLACEHOLDERS:
            print(f"[FALLO] Falta el campo '{field}' (sigue siendo placeholder).")
            return 1
    print(f"[OK]   Config cargado. api_base = {cfg['api_base']}")
    print(f"[OK]   public_key  = {cfg['public_key'][:6]}...{cfg['public_key'][-4:]}")
    print(f"[OK]   user_key    = {len(cfg['user_key'])} chars (oculto)")
    _line()

    # 3. Llamada real al portfolio
    from utils.etoro_client import get_portfolio

    print("Llamando GET /api/v1/trading/info/portfolio ...")
    data = get_portfolio()

    if data.get("error"):
        print(f"[FALLO] eToro respondio con error: {data['error']}")
        if "auth_error" in str(data["error"]):
            print("        -> 401/403: el user_key (token de sesion) expiro o es invalido.")
            print("           Regenera el User Key en etoro.com/api y vuelve a pegarlo.")
        return 1

    positions = data["positions"]
    print(f"[OK]   Conexion exitosa.")
    print(f"       Cash disponible : ${data['available_cash']:,.2f}")
    print(f"       Valor total     : ${data['total_value']:,.2f}")
    print(f"       Posiciones      : {len(positions)}")
    _line()

    if positions:
        print(f"  {'TICKER':<8}{'DIR':<5}{'INVERTIDO':>12}{'P&L %':>9}")
        _line()
        for p in positions:
            print(
                f"  {p['ticker']:<8}{p['direction']:<5}"
                f"{p['invested_amount']:>11,.0f}${p['net_profit_pct']:>8.2f}"
            )
    else:
        print("  (Sin posiciones abiertas en este momento.)")

    _line("=")
    print("  RESULTADO: conexion READ-ONLY operativa.")
    _line("=")
    return 0


if __name__ == "__main__":
    sys.exit(main())
