# Plan — Nuevo Sistema de Planning con Random Diario Ponderado

**Fecha:** 2026-07-25  
**Ramificación:** del plan de reajuste anterior

---

## 1. Especificación del Algoritmo de Planning

### 1.1 Visión general

El sistema de planning genera sltos planificados (`planned_slots` para vídeos largos, `shorts_planned_slots` para shorts) con horizonte de 7 días. Cada 5 minutos, el `schedule_checker_loop` despacha el siguiente slot pendiente según prioridad. Cada ~30 min, `smart_replan` reconcilia desviaciones entre lo planeado y lo real.

### 1.2 Vídeos largos — Algoritmo por canal por día

Para cada canal y fecha `D`:

1. **Número de vídeos del día** (`n`):
   - `base = videos_per_day` (config mínimo, default 2)
   - `boost = videos_day_boost_weight` (probabilidad de +1, default 0.7)
   - `seed = hash(date_str + channel_id)` → generador determinista por día
   - Si `random() < boost` → `n = base + 1`, sino `n = base`
   - El resultado es determinista: mismo `date + channel` → mismo `n` siempre

2. **Modo viral** (`viral_count` entre los `n`):
   - `viral_min = viral_per_day_min` (default 1)
   - `viral_boost = viral_day_boost_weight` (probabilidad de +1, default 0.2)
   - `seed = hash(date_str + channel_id + 999)` → generador determinista offset distinto
   - Si `random() < viral_boost` → `viral_count = min(viral_min + 1, n)`, sino `viral_count = min(viral_min, n)`

3. **Secuencia de modos** (`source_mode` para cada slot):
   - `_build_source_mode_sequence(n, viral_count)` → alterna `[viral, original, viral, original, ...]` empezando por viral
   - Ejemplo: `n=3, viral=1` → `[viral, original, original]`
   - Ejemplo: `n=3, viral=2` → `[viral, original, viral]`

4. **Distribución horaria** (`target_upload_at`):
   - `_distribute_slots(n, day_seed, channel_id, upload_windows, publish_mode, timezone)`
   - Distribuye los `n` slots dentro de las ventanas de subida (`upload_windows`)
   - Scheduled channels: `target_public_at` = peak hour + jitter
   - Non-scheduled: `target_upload_at` dentro de la ventana, `scheduled_at = target_upload_at - lead_hours - avg_duration`

5. **Encadenamiento de generación** (`scheduled_at`):
   - Algoritmo backwards: desde el último slot del horizonte hacia el primero
   - Cada slot debe terminar (`scheduled_at + duration`) antes del `target_upload_at`
   - GAP mínimo entre slots: `GLOBAL_GAP_MINUTES` (30 min)
   - Si hay overcapacity: se comprime hasta `GLOBAL_GAP_MINUTES` como mínimo
   - Si capacity sobra: se programa lo más temprano posible (respetando `lead_hours`)

### 1.3 Shorts — Algoritmo por canal por día

Para cada canal y fecha `D`:

1. **Nativos** (`native_count`):
   - Fijo: `shorts_native_per_day` (default 4, todos los canales)

2. **Clips** (`clip_count`):
   - `clips_per_long = shorts_clips_per_long` (default 2)
   - `yesterday_published = count_videos_published_yesterday(channel_id, D)`
   - `clip_count = clips_per_long × yesterday_published`
   - Si no hay vídeos publicados ayer → 0 clips

3. **Distribución horaria**:
   - `_build_shorts_slots_for_channel()` distribuye nativos + clips en franjas a lo largo del día
   - `scheduled_at` calculado hacia atrás desde `target_upload_at`
   - Nativos primero, clips después

4. **Dependencia de clips**:
   - Los clips necesitan `source_video_id` — el vídeo largo del día anterior del que se extraen
   - Si el vídeo fuente no se completó, el clip se cancela al intentar despachar

### 1.4 Mecanismo de reconciliación (`smart_replan`)

Ejecutado cada ~30 min (10:00-23:00 CEST):

1. **Paso 0 — Pipeline seco**: Si `total_pending < 3` → replan completo del horizonte
2. **Paso 0b — Días vacíos**: Si mañana o pasado no tienen slots → replan
3. **Paso 1 — Canales deshabilitados**: Cancela todos los pending de canales con `planning_enabled=false`
4. **Paso 2a — Cuenta generados hoy**: `count_videos_generated_today(ch_id)` (incluye generating, awaiting_upload, uploaded, etc.)
5. **Paso 2b — Exceso de slots**: Si `generated_today >= resolved_vpd(today)` → cancela pending sobrantes de hoy
6. **Paso 2c — Config mismatch**: Si `tomorrow_pending != resolved_vpd(tomorrow)` → replan horizonte
7. **Paso 2d — Slots stale**: Cancela pending con `scheduled_at > 6h` en el pasado (solo hoy/fechas pasadas)
8. **Paso 3 — Overcapacity warning**: Si `total_pending > daily_capacity × 2` → solo warning

**IMPORTANTE:** Todos los checks que usan `videos_per_day` deben usar `_resolve_videos_per_day(cfg, date_str)` en vez de `cfg.get("videos_per_day")` directamente. Esto es crítico porque cada día puede tener un `n` distinto (2 o 3 según random).

### 1.5 Recovery planner

Ejecutado cada 60 min. Detecta déficit de vídeos publicados vs objetivo del día. Mismo principio: usar `_resolve_videos_per_day()` para el target diario, no `videos_per_day` directo.

---

## 2. Cambios en el código

### 2.1 `config/settings.py` — OK (ya ajustado con `override=True`)

### 2.2 `database/db_extended.py`

#### `get_channel_planning_config()` (~L3413)
Añadir al return dict:
```python
"videos_day_boost_weight": config.get("videos_day_boost_weight", 0.7),
"viral_day_boost_weight": config.get("viral_day_boost_weight", 0.2),
```

#### `update_channel_planning_config()` (~L3456)
Añadir parámetros:
```python
videos_day_boost_weight: float = None,
viral_day_boost_weight: float = None,
```
Y lógica de clamping:
```python
if videos_day_boost_weight is not None:
    config["videos_day_boost_weight"] = round(max(0.0, min(1.0, videos_day_boost_weight)), 2)
if viral_day_boost_weight is not None:
    config["viral_day_boost_weight"] = round(max(0.0, min(1.0, viral_day_boost_weight)), 2)
```

#### `get_shorts_planning_config()` (~L4194)
Cambiar defaults:
```python
"shorts_native_per_day": sc.get("shorts_native_per_day", 4),   # antes 3
```
(El `shorts_clips_per_long` ya existe y se cambia vía migración SQL)

### 2.3 `api/services/planning_service.py`

#### `_resolve_videos_per_day()` (~L54)
Reescribir para soportar random ponderado:
```python
def _resolve_videos_per_day(ch: dict, date_str: str) -> int:
    base = int(ch.get("videos_per_day", 2) or 2)
    boost_weight = float(ch.get("videos_day_boost_weight", 0.7))
    day_seed = int(date_str.replace("-", ""))
    ch_id = int(ch.get("channel_id", 0) or 0)
    rng = random.Random(day_seed + ch_id)
    # Si base es 0 → no generar (canal deshabilitado en planificación)
    if base <= 0:
        return 0
    if rng.random() < boost_weight:
        return base + 1
    return base
```

#### `_build_source_mode_sequence()` (~L71) — renombrar a versión con viral boost
```python
def _build_source_mode_sequence(total: int, ch: dict, date_str: str) -> list[str]:
    viral_min = int(ch.get("viral_per_day", 1) or 1)
    viral_boost = float(ch.get("viral_day_boost_weight", 0.2))
    day_seed = int(date_str.replace("-", ""))
    ch_id = int(ch.get("channel_id", 0) or 0)
    rng = random.Random(day_seed + ch_id + 999)  # offset distinto
    viral_count = viral_min
    if total > viral_min and rng.random() < viral_boost:
        viral_count = min(viral_min + 1, total)
    viral_count = max(0, min(viral_count, total))
    if viral_count <= 0:
        return ["original"] * total
    if viral_count >= total:
        return ["viral"] * total
    result = []
    remaining_viral = viral_count
    remaining_orig = total - viral_count
    first = "viral" if remaining_viral >= remaining_orig else "original"
    for i in range(total):
        if first == "viral":
            if remaining_viral > 0 and (i % 2 == 0 or remaining_orig == 0):
                result.append("viral")
                remaining_viral -= 1
            else:
                result.append("original")
                remaining_orig -= 1
        else:
            if remaining_orig > 0 and (i % 2 == 0 or remaining_viral == 0):
                result.append("original")
                remaining_orig -= 1
            else:
                result.append("viral")
                remaining_viral -= 1
    return result
```

#### `compute_daily_slots()` y `compute_horizon_slots()`
Actualizar las llamadas:
- `_resolve_videos_per_day(ch, date_str)` → ya devuelve el valor random determinista
- `_build_source_mode_sequence(n, ch, date_str)` → ahora acepta `ch` y `date_str` para calcular viral count con random

#### `smart_replan()` — CRÍTICO: arreglar todos los checks con vpd
```python
# Línea ~1319: reemplazar
vpd = int(cfg.get("videos_per_day", 1) or 1)
# por:
resolved_vpd_today = _resolve_videos_per_day(cfg, today)

# Línea ~1346: usar resolved_vpd_today en vez de vpd
if generated_today >= resolved_vpd_today and today_pending > 0:

# Línea ~1369: usar resolved para mañana
resolved_vpd_tomorrow = _resolve_videos_per_day(cfg, tomorrow_str)
if tcnt > 0 and tcnt != resolved_vpd_tomorrow:
```

#### `sync_midday()` — Mismo fix
```python
# Línea ~1034
target = _resolve_videos_per_day(cfg, today)
```

### 2.4 `api/services/shorts_scheduler.py`

#### `compute_daily_shorts_slots()` (~L335)
```python
# Cambiar: long_video_count, long_target_hours = _get_planned_long_video_count(ch_id, date_str)
# Por:
yesterday_count = _get_yesterday_published_count(ch_id, date_str, db)
clip_count = clips_per_long * yesterday_count
```

#### Nueva función `_get_yesterday_published_count()`
```python
def _get_yesterday_published_count(channel_id: int, date_str: str, db=None) -> int:
    """Cuenta vídeos largos publicados el día anterior (status uploaded/published)."""
    from datetime import datetime, timedelta
    yesterday = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    if db is None:
        from database.db import get_db
        db = get_db()
    count = db.count_completed_videos_for_date(channel_id, yesterday)
    return count
```

#### Añadir `count_completed_videos_for_date()` en `database/db.py` o `db_extended.py`
```python
def count_completed_videos_for_date(self, channel_id: int, date_key: str) -> int:
    """Cuenta vídeos con status uploaded/published para una fecha específica."""
    rows = self._fetch(
        """SELECT COUNT(*) as cnt FROM videos v
           JOIN planned_slots p ON p.video_id = v.id
           WHERE p.channel_id = ? AND p.date_key = ?
           AND v.status IN ('uploaded', 'published', 'uploaded_private')""",
        (channel_id, date_key)
    )
    return rows[0]["cnt"] if rows else 0
```

### 2.5 `api/services/recovery_planner.py`

#### `auto_recover_missing_publications()` (~L209)
```python
# Reemplazar:
target = cfg.get("videos_per_day", 0)
# Por:
target = _resolve_videos_per_day(cfg, today)
```
(Necesitará importar `_resolve_videos_per_day` de `planning_service`)

### 2.6 `api/routers/planning.py`

#### `PlanningConfigUpdate` model
Añadir:
```python
videos_day_boost_weight: Optional[float] = None   # 0.0 — 1.0
viral_day_boost_weight: Optional[float] = None     # 0.0 — 1.0
```

#### Pasar a `db.update_channel_planning_config()`:
```python
videos_day_boost_weight=data.videos_day_boost_weight,
viral_day_boost_weight=data.viral_day_boost_weight,
```

### 2.7 Frontend

#### `ChannelConfigCard.tsx`
- Añadir al interface `ChannelConfig`: `videos_day_boost_weight: number`, `viral_day_boost_weight: number`
- Añadir 2 sliders:
  - **"Prob. +1 vídeo/día"**: icono `Dice5`, valores 0-100% (mapeados a 0.0-1.0), default 70%, color azul `text-blue-400`
  - **"Prob. 2º viral/día"**: icono `Zap`, valores 0-100%, default 20%, color purple `text-purple-400`
- Deshabilitar cuando `!config.planning_enabled`
- Formato: mostrar porcentaje (ej. "70%") junto al stepper

#### `Scheduling.tsx`
- Añadir los campos al interface `PlanningConfig`
- Pasar en `onUpdate`

#### `api.ts`
- Añadir campos opcionales a `updatePlanningConfig`

### 2.8 Frontend — ShortsCard (Scheduling.tsx)

Cambiar defaults visuales:
- Nativos/día: label = "Nativos/día", default visual = 4 (ya se lee del backend)
- Clips: label = "Clips × vídeo ayer", default visual = 2

(Los defaults reales vienen del backend vía `GET /api/planning/shorts-config`)

---

## 3. Migración de base de datos

```sql
-- 1. Ajustar canal4 a base 2 (era 3)
UPDATE channels SET config_json = json_set(config_json, '$.videos_per_day', 2) 
WHERE slug = 'canal4' AND json_extract(config_json, '$.videos_per_day') = 3;

-- 2. Añadir boost weights a todos los canales activos
UPDATE channels SET config_json = json_set(
    json_set(config_json, '$.videos_day_boost_weight', 0.7),
    '$.viral_day_boost_weight', 0.2
) WHERE active = 1;

-- 3. Asegurar viral_per_day = 1 en todos (mínimo base)
UPDATE channels SET config_json = json_set(config_json, '$.viral_per_day', 1)
WHERE active = 1 AND json_extract(config_json, '$.viral_per_day') IS NULL;

-- 4. Shorts: 4 nativos + 2 clips por largo
UPDATE shorts_planning_config SET shorts_native_per_day = 4, shorts_clips_per_long = 2;

-- 5. Borrar toda la planificación actual (se regenera al reiniciar API)
DELETE FROM planned_slots WHERE date_key >= '2026-07-25';
DELETE FROM shorts_planned_slots WHERE date_key >= '2026-07-25';
```

---

## 4. Orden de ejecución

| Paso | Acción | Herramienta |
|------|--------|-------------|
| 1 | Modificar `database/db_extended.py` — nuevos campos | Edit |
| 2 | Modificar `api/services/planning_service.py` — random + fix vpd | Edit |
| 3 | Modificar `api/services/shorts_scheduler.py` — clips de ayer | Edit |
| 4 | Añadir `count_completed_videos_for_date()` en DB | Edit |
| 5 | Modificar `api/services/recovery_planner.py` — fix vpd | Edit |
| 6 | Modificar `api/routers/planning.py` — nuevos campos | Edit |
| 7 | Modificar `frontend/.../ChannelConfigCard.tsx` — sliders | Edit |
| 8 | Modificar `frontend/.../Scheduling.tsx` — interfaces | Edit |
| 9 | Modificar `frontend/.../api.ts` — tipos | Edit |
| 10 | Ejecutar migración SQL | Bash |
| 11 | `npm run build` en frontend | Bash |
| 12 | Commit + restart API (`apply_changes.sh`) | Bash |
| 13 | Verificar planificación generada | Bash/DB query |

---

## 5. Verificación post-deploy

```sql
-- Verificar que los slots tienen source_mode variado
SELECT date_key, c.slug, source_mode, COUNT(*) 
FROM planned_slots p JOIN channels c ON c.id=p.channel_id
WHERE date_key BETWEEN '2026-07-26' AND '2026-07-30'
GROUP BY date_key, c.slug, source_mode
ORDER BY date_key, c.slug;

-- Verificar que hay mezcla de 2 y 3 slots por canal/día
SELECT date_key, c.slug, COUNT(*) as n
FROM planned_slots p JOIN channels c ON c.id=p.channel_id  
WHERE date_key BETWEEN '2026-07-26' AND '2026-07-30' AND p.status='pending'
GROUP BY date_key, c.slug
ORDER BY date_key, c.slug;

-- Verificar shorts config
SELECT c.slug, sc.shorts_native_per_day, sc.shorts_clips_per_long
FROM shorts_planning_config sc JOIN channels c ON c.id=sc.channel_id;
```
