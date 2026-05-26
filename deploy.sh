#!/bin/bash
# deploy.sh — OportunityAlert deploy script
# Uso: bash /home/opc/oportunity-alert/deploy.sh

set -euo pipefail

PROJECT_DIR="/home/opc/oportunity-alert"
VENV_PIP="$PROJECT_DIR/venv/bin/pip"
SERVICE="opportunity-alert"
LOG_LINES=40

cd "$PROJECT_DIR"

echo "========================================"
echo " OportunityAlert — Deploy"
echo "========================================"

# 1. Git pull
echo ""
echo "[1/4] Git pull..."
git pull origin main
echo "OK"

# 2. Dependencias
echo ""
echo "[2/4] Actualizando dependencias..."
$VENV_PIP install -q -r requirements.txt
echo "OK"

# 3. Syntax check
echo ""
echo "[3/4] Verificando sintaxis Python..."
FAILED=0
while IFS= read -r -d '' pyfile; do
    if ! python3 -c "import py_compile; py_compile.compile('$pyfile', doraise=True)" 2>/dev/null; then
        echo "  ERROR de sintaxis: $pyfile"
        FAILED=1
    fi
done < <(find "$PROJECT_DIR" -name "*.py" \
    -not -path "*/venv/*" \
    -not -path "*/__pycache__/*" \
    -print0)

if [ $FAILED -eq 1 ]; then
    echo ""
    echo "Deploy ABORTADO — corrige los errores de sintaxis primero."
    exit 1
fi
echo "OK"

# 4. Restart + logs
echo ""
echo "[4/4] Reiniciando servicio..."
sudo systemctl restart "$SERVICE"
sleep 3

STATUS=$(systemctl is-active "$SERVICE" 2>/dev/null || true)
if [ "$STATUS" != "active" ]; then
    echo ""
    echo "FALLO al iniciar el servicio. Ultimos logs:"
    sudo journalctl -u "$SERVICE" -n $LOG_LINES --no-pager
    exit 1
fi

echo "OK — servicio activo"
echo ""
echo "========================================"
echo " Deploy exitoso"
echo " Dashboard: http://213.35.121.9:8081"
echo "========================================"
echo ""
echo "Ultimos logs:"
sudo journalctl -u "$SERVICE" -n $LOG_LINES --no-pager
