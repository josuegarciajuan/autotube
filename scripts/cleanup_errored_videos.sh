#!/bin/bash
set -euo pipefail

DB="/root/autotube/autotube.db"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# PHP_EOL IDs
BAD_IDS=$(sqlite3 "$DB" "SELECT GROUP_CONCAT(id) FROM videos WHERE status IN ('error','interrupted','blocked','orphaned','worker_died','deleted_on_yt');")
SCRIPT_IDS=$(sqlite3 "$DB" "SELECT GROUP_CONCAT(DISTINCT script_id) FROM videos WHERE status IN ('error','interrupted','blocked','orphaned','worker_died','deleted_on_yt') AND script_id IS NOT NULL;")

if [[ -z "$BAD_IDS" ]]; then
    echo "No hay videos problemáticos. Nada que limpiar."
    exit 0
fi

echo "Videos a eliminar: $BAD_IDS"
echo "Scripts asociados: ${SCRIPT_IDS:-ninguno}"
echo ""

echo "ESTADO ACTUAL:"
sqlite3 -header -column "$DB" "SELECT v.canal, COUNT(*) as bad_count FROM videos v WHERE v.status IN ('error','interrupted','blocked','orphaned','worker_died','deleted_on_yt') GROUP BY v.canal;"
echo ""

if $DRY_RUN; then
    echo "DRY-RUN — se eliminarian los siguientes registros:"
    echo ""
    for tbl in video_scenes video_stats_history video_asset_history video_playlists video_lifecycle_actions generation_jobs; do
        count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM $tbl WHERE video_id IN ($BAD_IDS);" 2>/dev/null || echo "0")
        echo "  $tbl: $count registros"
    done
    echo "  planned_slots: $(sqlite3 "$DB" "SELECT COUNT(*) FROM planned_slots WHERE video_id IN ($BAD_IDS);") (SET NULL)"
    echo "  videos: 18 registros"
    if [[ -n "$SCRIPT_IDS" ]]; then
        echo "  scripts orphans: $(sqlite3 "$DB" "SELECT COUNT(*) FROM scripts WHERE id IN ($SCRIPT_IDS);")"
    fi
    echo ""
    echo "Ejecuta sin --dry-run para aplicar los cambios."
    exit 0
fi

echo "Ejecutando limpieza real..."
echo ""

# Backup
BACKUP_PATH="${DB}.backup-$(date +%Y%m%d-%H%M%S)"
cp "$DB" "$BACKUP_PATH"
echo "Backup guardado: $BACKUP_PATH"
echo ""

# Guardar file paths antes de borrar
FILE_LIST="/tmp/cleanup_files_$$.txt"
sqlite3 "$DB" "SELECT COALESCE(video_path,'') || '|' || COALESCE(audio_path,'') || '|' || COALESCE(thumbnail_path,'') FROM videos WHERE id IN ($BAD_IDS);" > "$FILE_LIST"

# Child tables
echo "Limpiando tablas hijas..."
for tbl in video_scenes video_stats_history video_asset_history video_playlists video_lifecycle_actions; do
    count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM $tbl WHERE video_id IN ($BAD_IDS);" 2>/dev/null || echo "0")
    if [[ "$count" -gt 0 ]]; then
        sqlite3 "$DB" "DELETE FROM $tbl WHERE video_id IN ($BAD_IDS);"
        echo "   $tbl: $count"
    fi
done

count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM generation_jobs WHERE video_id IN ($BAD_IDS);")
if [[ "$count" -gt 0 ]]; then
    sqlite3 "$DB" "DELETE FROM generation_jobs WHERE video_id IN ($BAD_IDS);"
    echo "   generation_jobs: $count"
fi

count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM planned_slots WHERE video_id IN ($BAD_IDS);")
if [[ "$count" -gt 0 ]]; then
    sqlite3 "$DB" "UPDATE planned_slots SET video_id=NULL, status='cancelled' WHERE video_id IN ($BAD_IDS);"
    echo "   planned_slots: $count cancelados"
fi

# Pipeline log
if [[ -n "$SCRIPT_IDS" ]]; then
    count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM pipeline_log WHERE content_id IN ($SCRIPT_IDS);" 2>/dev/null || echo "0")
    if [[ "$count" -gt 0 ]]; then
        sqlite3 "$DB" "DELETE FROM pipeline_log WHERE content_id IN ($SCRIPT_IDS);"
        echo "   pipeline_log: $count"
    fi
fi
echo ""

# Scripts and raw_content orphans
if [[ -n "$SCRIPT_IDS" ]]; then
    raw_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM raw_content WHERE id IN ($SCRIPT_IDS);" 2>/dev/null || echo "0")
    if [[ "$raw_count" -gt 0 ]]; then
        sqlite3 "$DB" "DELETE FROM raw_content WHERE id IN ($SCRIPT_IDS);"
        echo "raw_content huerfano: $raw_count"
    fi
    script_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM scripts WHERE id IN ($SCRIPT_IDS);")
    if [[ "$script_count" -gt 0 ]]; then
        sqlite3 "$DB" "DELETE FROM scripts WHERE id IN ($SCRIPT_IDS);"
        echo "scripts huerfanos: $script_count"
    fi
    echo ""
fi

# Main event
video_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM videos WHERE id IN ($BAD_IDS);")
sqlite3 "$DB" "DELETE FROM videos WHERE id IN ($BAD_IDS);"
echo "videos eliminados: $video_count"
echo ""

# Disk cleanup
echo "Limpiando archivos en disco..."
while IFS='|' read -r vp ap tp; do
    if [[ -n "$vp" && -f "$vp" ]]; then
        rm -f "$vp" && echo "   DEL: $vp"
    fi
    if [[ -n "$ap" && -f "$ap" ]]; then
        rm -f "$ap" && echo "   DEL: $ap"
    fi
    if [[ -n "$tp" && -f "$tp" ]]; then
        echo "   KEPT: $tp (thumbnail policy)"
    fi
done < "$FILE_LIST"
rm -f "$FILE_LIST"

echo ""
echo "Limpieza completada."
echo ""

echo "ESTADO FINAL:"
remaining=$(sqlite3 "$DB" "SELECT COUNT(*) FROM videos WHERE status IN ('error','interrupted','blocked','orphaned','worker_died','deleted_on_yt');")
total=$(sqlite3 "$DB" "SELECT COUNT(*) FROM videos;")
echo "   Problematicos restantes: $remaining"
echo "   Total videos en DB: $total"
