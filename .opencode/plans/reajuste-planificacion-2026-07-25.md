# Plan de Reajuste — Planificación Diaria de Videos y Shorts

**Fecha:** 2026-07-25  
**Contexto:** Colapso masivo el 24-jul-2026 por deprecación del modelo DeepSeek `deepseek-chat`.

---

## Diagnóstico

### Lo que SE DEBERÍA estar generando (UI)
| Canal | Videos/día | Shorts/día |
|-------|-----------|------------|
| canal2 — Sincronias | 2 | 3 |
| canal3 — Civilizaciones Olvidadas | 2 | 3 |
| canal4 — Expediciones sin retorno | 3 | 3 |
| canal5 — Anomalias Medicas | 2 | 3 |
| **Total** | **9** | **12** |

### Lo que REALMENTE se publicó (videos completados)

| Fecha | canal2 | canal3 | canal4 | canal5 | Total | % plan |
|-------|--------|--------|--------|--------|-------|--------|
| Jul 21 | 0 | 0 | 1 | 0 | 1 | 11% |
| Jul 22 | 3 | 2 | 1 | 2 | 8 | 89% |
| Jul 23 | 4 | 2 | 6 | 4 | 16 | 178% |
| Jul 24 | 3 | 2 | 3 | 2 | 10 | 111% |
| Jul 25 | 1 | 1 | 0 | 0 | 2 | (parcial) |

### Lo que REALMENTE se publicó (shorts completados)

| Fecha | canal2 | canal3 | canal4 | canal5 | Total | % plan |
|-------|--------|--------|--------|--------|-------|--------|
| Jul 21 | 3 | 2 | 1 | 2 | 8 | 67% |
| Jul 22 | 3 | 3 | 0 | 2 | 8 | 67% |
| Jul 23 | 3 | 3 | 3 | 3 | 12 | 100% |
| Jul 24 | 2 | 2 | 3 | 3 | 10 | 83% |

### Causa raíz del colapso del 24-jul
DeepSeek deprecó `deepseek-chat` el 24-jul-2026. El `.env` fue actualizado a `deepseek-v4-flash` pero la API ya estaba corriendo con la variable stale en memoria. Workers heredaron el valor incorrecto → 54 videos fallaron con error engañoso "sin contenido disponible".

---

## Paso 1 — Blindar el entorno de workers (PREVENTIVO)

### Archivo: `config/settings.py` línea 13

**Cambio:**
```python
# ANTES:
load_dotenv(PROJECT_ROOT / ".env")

# DESPUÉS:
load_dotenv(PROJECT_ROOT / ".env", override=True)
```

**Razón:** Sin `override=True`, si un proceso hereda variables de entorno viejas (ej. `LLM_MODEL=deepseek-chat`), `load_dotenv()` no las sobreescribe con los valores del `.env`. Con `override=True`, cualquier proceso nuevo (workers, CLI) siempre lee los valores frescos del `.env`.

---

## Paso 2 — Limpiar duplicados de `planned_slots`

### SQL a ejecutar:
```sql
-- Ver duplicados primero
SELECT date_key, channel_id, slot_position, status, COUNT(*) as cnt
FROM planned_slots
WHERE date_key >= '2026-07-22'
GROUP BY date_key, channel_id, slot_position, status
HAVING cnt > 1
ORDER BY date_key, channel_id, slot_position;

-- Eliminar filas cancelled que son duplicadas (mantener la más reciente)
DELETE FROM planned_slots
WHERE id NOT IN (
    SELECT id FROM (
        SELECT MAX(id) as id
        FROM planned_slots
        WHERE status != 'cancelled'
        GROUP BY date_key, channel_id, slot_position
    )
)
AND status = 'cancelled'
AND date_key >= '2026-07-22';

-- Para slots donde TODAS las filas son cancelled, mantener solo la más reciente
DELETE FROM planned_slots
WHERE id NOT IN (
    SELECT MIN(id) FROM (
        SELECT id, date_key, channel_id, slot_position
        FROM planned_slots
        WHERE date_key >= '2026-07-22'
        GROUP BY date_key, channel_id, slot_position
    )
)
AND date_key >= '2026-07-22'
AND status = 'cancelled';
```

**Razón:** El colapso del 24-jul causó acumulación de filas `cancelled` duplicadas (hasta 7 por slot). Esto distorsiona métricas y la planificación futura. Hay ~200+ filas huérfanas.

---

## Paso 3 — Recuperar shorts de Jul 25

### SQL:
```sql
-- Eliminar shorts cancelados de Jul 25
DELETE FROM shorts_planned_slots
WHERE date_key = '2026-07-25' AND status = 'cancelled';
```

Luego la API regenerará automáticamente los slots vía `ensure_today_shorts_scheduled()` o el recovery planner al detectar el déficit.

**Razón:** Todos los shorts del 25-jul fueron cancelados en cascada porque los videos largos fallaron. Ahora que la generación larga funciona, los shorts deben regenerarse.

---

## Paso 4 — Verificar plan actual

Los valores en DB ya son correctos (2/2/3/2 para videos, 3/3/3/3 para shorts). No se requieren cambios de configuración.

### Verificación:
```sql
-- Videos por día (en channels.config_json)
SELECT c.slug, c.id FROM channels c WHERE c.active=1;

-- Shorts planning config
SELECT channel_id, shorts_native_per_day, shorts_enabled FROM shorts_planning_config;
```

---

## Ejecución

Para salir del modo Plan y ejecutar:

1. Aplicar el cambio en `config/settings.py` (línea 13): añadir `override=True`
2. Ejecutar los SQL de limpieza en `autotube.db`
3. Reiniciar la API para que tome el cambio de `load_dotenv`: `bash scripts/apply_changes.sh`
4. Verificar que la generación se reanuda normalmente

### Nota sobre el modelo DeepSeek
El fix del modelo (`deepseek-chat` → `deepseek-v4-flash`) ya fue aplicado en el `.env` y en commits `6a034fd` y `898175f`. No requiere acción adicional.
