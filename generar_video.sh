#!/bin/bash
# ============================================================
# Autotube — Generar video completo
# Scrapea automáticamente si no hay contenido
# Ejecutar: bash /root/autotube/generar_video.sh
# ============================================================
set -e
cd /root/autotube

echo "=============================================="
echo "  AUTOTUBE — Generación de Video"
echo "  Canal 1: Historias Impactantes Reales"
echo "  $(date)"
echo "=============================================="

# ── Paso 1: Pipeline completo ──────────────────────────────────
# scrape (si es necesario) → guion → TTS → imágenes → video
echo ""
echo "[1/2] Pipeline completo (scrape + guion + voz + imágenes + montaje)..."
echo "       Esto tarda ~5-10 min en el montaje de video. Paciencia."
echo ""
python3 main.py run --canal canal1 --skip-upload

# ── Paso 2: Copiar a web ───────────────────────────────────────
echo ""
echo "[2/2] Publicando en web..."
python3 -c "
import shutil
from pathlib import Path
import sqlite3

conn = sqlite3.connect('autotube.db')
conn.row_factory = sqlite3.Row
row = conn.execute(
    '''SELECT video_path, thumbnail_path, titulo_final
       FROM videos
       WHERE canal = ? AND yt_video_id IS NULL
       ORDER BY created_at DESC LIMIT 1''',
    ('canal1',)
).fetchone()
conn.close()

if not row:
    print('  ERROR: No se encontró el video en la base de datos')
    exit(1)

vp = Path(row['video_path'])
if not vp.exists():
    print(f'  ERROR: Video no encontrado en disco: {vp}')
    exit(1)

tp = Path(row['thumbnail_path']) if row['thumbnail_path'] else None
web = Path('/var/www/html/atupuerta/autotube/videos')
web.mkdir(parents=True, exist_ok=True)

shutil.copy2(vp, web / vp.name)
print(f'  Video: https://lamami.online/autotube/videos/{vp.name}')

if tp and tp.exists():
    shutil.copy2(tp, web / tp.name)
    print(f'  Thumb: https://lamami.online/autotube/videos/{tp.name}')

print(f'  Título: {row[\"titulo_final\"]}')
print(f'  Tamaño: {vp.stat().st_size / 1024 / 1024:.1f} MB')
"

echo ""
echo "=============================================="
echo "  VIDEO GENERADO CON ÉXITO"
echo "  Dashboard: https://lamami.online/autotube/"
echo "=============================================="
