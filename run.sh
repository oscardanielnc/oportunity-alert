#!/bin/bash
# OportunityAlert — Deploy en Oracle VM
# Uso: bash run.sh [--service | --direct]
#
#   --service  instala y activa como servicio systemd (recomendado, auto-reinicio)
#   --direct   corre directamente con nohup (sin systemd)
#   sin arg    muestra este menú

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== OportunityAlert — Deploy ==="
echo "Directorio: $SCRIPT_DIR"

# ── Verificar .env ────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo "ERROR: .env no encontrado. Copia .env.template y completa las variables."
    exit 1
fi

# Cargar variables para verificación
set -a; source .env; set +a

for var in ANTHROPIC_API_KEY FINNHUB_API_KEY; do
    if [ -z "${!var}" ]; then
        echo "ERROR: $var no configurada en .env"
        exit 1
    fi
done
echo "Variables de entorno: OK"

# ── Instalar dependencias ─────────────────────────────────────────────────────
pip3 install -r requirements.txt --quiet
echo "Dependencias: OK"

mkdir -p data

# ── Modo systemd (recomendado) ────────────────────────────────────────────────
install_service() {
    SERVICE_FILE="/etc/systemd/system/opportunity_alert.service"
    USER_NAME=$(whoami)

    # Ajustar el usuario en el .service al usuario actual
    sed "s/User=ubuntu/User=$USER_NAME/" opportunity_alert.service | \
    sed "s|/home/ubuntu|$HOME|g" > /tmp/opportunity_alert.service

    sudo cp /tmp/opportunity_alert.service "$SERVICE_FILE"
    sudo systemctl daemon-reload
    sudo systemctl enable opportunity_alert
    sudo systemctl restart opportunity_alert

    echo ""
    echo "Servicio instalado y activo."
    echo ""
    echo "Comandos utiles:"
    echo "  sudo systemctl status opportunity_alert    ver estado"
    echo "  journalctl -u opportunity_alert -f         logs en vivo"
    echo "  sudo systemctl stop opportunity_alert      detener"
    echo "  sudo systemctl restart opportunity_alert   reiniciar"
    echo "  python3 main.py status                     resumen rapido"
}

# ── Modo directo con nohup ────────────────────────────────────────────────────
run_direct() {
    PID_FILE="opportunity_alert.pid"
    LOG_FILE="opportunity_alert.log"

    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "Deteniendo instancia anterior (PID $OLD_PID)..."
            kill "$OLD_PID"
            sleep 2
        fi
        rm -f "$PID_FILE"
    fi

    nohup python3 main.py >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Iniciado en background (PID: $(cat $PID_FILE))"
    echo "  Logs: tail -f $SCRIPT_DIR/$LOG_FILE"
    echo "  Stop: kill \$(cat $SCRIPT_DIR/$PID_FILE)"
    sleep 2
    tail -5 "$LOG_FILE" 2>/dev/null || true
}

# ── Menú ──────────────────────────────────────────────────────────────────────
case "${1:-}" in
    --service)
        install_service
        ;;
    --direct)
        run_direct
        ;;
    *)
        echo ""
        echo "Opciones:"
        echo "  bash run.sh --service   instala systemd (auto-reinicio en reboot y crashes)"
        echo "  bash run.sh --direct    corre con nohup (mas simple, no persiste en reboot)"
        echo ""
        echo "Recomendado para produccion: --service"
        ;;
esac
