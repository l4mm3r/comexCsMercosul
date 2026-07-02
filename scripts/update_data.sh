#!/usr/bin/env bash
# =====================================================================
# Wrapper para cron: actualización mensual de datos Comex Stat.
#
# Comex Stat publica los datos consolidados de cada mes durante la
# primera semana del mes siguiente (días 3-6 aprox.).
# Cron sugerido (ejecuta los días 3-7 de cada mes a las 17:00):
#
#   0 17 3-7 * *  /home/eduardo-vazzoler/Documentos/Proyectos/comexstat/scripts/update_data.sh >> /var/log/comexstat_update.log 2>&1
#
# El script es idempotente: si no hay datos nuevos, no hace nada.
# =====================================================================
set -euo pipefail

PROJECT_DIR="/home/eduardo-vazzoler/Documentos/Proyectos/comexstat"
cd "$PROJECT_DIR"

# Python del entorno virtual del proyecto
PY="$PROJECT_DIR/.venv/bin/python"

echo "========================================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando actualización Comex Stat"
echo "========================================================"

# Actualización completa: descarga (solo cambios) -> transformación -> validación
"$PY" -m src.update

RC=$?
echo "--------------------------------------------------------"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Finalizado con código: $RC"
echo "========================================================"
exit $RC
