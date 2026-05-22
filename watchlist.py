"""
Gestor de watchlist — OportunityAlert
Uso:
  python watchlist.py              → muestra la lista actual
  python watchlist.py add AAPL: Apple Inc   → agrega ticker
  python watchlist.py remove AAPL           → elimina ticker
"""
import os
import sys
import re

# Forzar UTF-8 en stdout para Windows (evita UnicodeEncodeError con caracteres especiales)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.txt")


def load_watchlist() -> list[tuple[str, str]]:
    """Retorna lista de (TICKER, Nombre) ignorando comentarios."""
    entries = []
    if not os.path.exists(WATCHLIST_FILE):
        return entries
    with open(WATCHLIST_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                ticker, _, name = line.partition(":")
                entries.append((ticker.strip().upper(), name.strip()))
    return entries


def load_raw_lines() -> list[str]:
    if not os.path.exists(WATCHLIST_FILE):
        return []
    with open(WATCHLIST_FILE, encoding="utf-8") as f:
        return f.readlines()


def save_raw_lines(lines: list[str]):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)


def show():
    entries = load_watchlist()
    if not entries:
        print("Watchlist vacia. Agrega tickers con: python watchlist.py add TICKER: Nombre")
        return

    # Agrupar por sección (las líneas de comentario con ──)
    lines = load_raw_lines()
    print()
    print("=" * 52)
    print("  OPPORTUNITY ALERT — Watchlist activa")
    print(f"  {len(entries)} tickers monitoreados")
    print("=" * 52)

    current_section = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# ──") or stripped.startswith("# --"):
            section = stripped.lstrip("# ─-").strip()
            print(f"\n  {section}")
            print(f"  {'-' * 46}")
            current_section = section
        elif stripped.startswith("#"):
            continue  # comentario de instrucciones, skip
        elif ":" in stripped:
            ticker, _, name = stripped.partition(":")
            print(f"  {ticker.strip():<8}  {name.strip()}")

    print()
    print("=" * 52)
    print("  Editar: notepad watchlist.txt  (Windows)")
    print("  Editar: nano watchlist.txt     (Linux VM)")
    print("  Agregar: python watchlist.py add TICKER: Nombre")
    print("  Borrar:  python watchlist.py remove TICKER")
    print("=" * 52)
    print()


def add(args: list[str]):
    line_to_add = " ".join(args).strip()
    if ":" not in line_to_add:
        print("Formato incorrecto. Usa: python watchlist.py add TICKER: Nombre")
        print("Ejemplo: python watchlist.py add AAPL: Apple Inc")
        return

    ticker, _, name = line_to_add.partition(":")
    ticker = ticker.strip().upper()
    name = name.strip()

    # Verificar si ya existe
    existing = load_watchlist()
    if any(t == ticker for t, _ in existing):
        print(f"  {ticker} ya esta en la watchlist.")
        return

    # Agregar al final del archivo
    lines = load_raw_lines()
    # Quitar newline final si existe, agregar el ticker
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(f"{ticker}: {name}\n")
    save_raw_lines(lines)
    print(f"  Agregado: {ticker}: {name}")


def remove(args: list[str]):
    if not args:
        print("Indica el ticker a borrar. Ejemplo: python watchlist.py remove AAPL")
        return

    ticker_to_remove = args[0].strip().upper()
    lines = load_raw_lines()
    new_lines = []
    found = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            new_lines.append(line)
            continue
        if ":" in stripped:
            ticker = stripped.split(":")[0].strip().upper()
            if ticker == ticker_to_remove:
                found = True
                continue  # saltar esta línea = eliminar
        new_lines.append(line)

    if found:
        save_raw_lines(new_lines)
        print(f"  Eliminado: {ticker_to_remove}")
    else:
        print(f"  {ticker_to_remove} no encontrado en la watchlist.")


def get_tickers() -> list[str]:
    """Retorna solo los tickers — para usar desde main.py."""
    return [ticker for ticker, _ in load_watchlist()]


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        show()
    elif args[0].lower() == "add":
        add(args[1:])
        show()
    elif args[0].lower() == "remove":
        remove(args[1:])
        show()
    else:
        print("Uso: python watchlist.py [add TICKER: Nombre | remove TICKER]")
