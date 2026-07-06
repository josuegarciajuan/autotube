# Spec: Video Generation Pipeline

**Última actualización:** 2026-06-27
**Versión:** 2.0

---

## Propósito

Este documento define el algoritmo completo de generación de videos de Autotube. Todo cambio que afecte a este algoritmo **debe** incluir o modificar tests y pasar la suite completa antes de darse por bueno.

---

## Cómo usar los tests (TDD)

```bash
# Ejecutar todos los tests
python3 -m pytest tests/ -v

# Ejecutar un archivo específico
python3 -m pytest tests/test_voice_timing.py -v

# Ejecutar tests que coincidan con un patrón
python3 -m pytest tests/ -v -k "voice_speed"
```

### Reglas TDD

1. **Todo cambio que modifique el algoritmo de generación debe**:
   - Añadir nuevos tests o modificar los existentes en `tests/`
   - Ejecutar `python3 -m pytest tests/ -v` y pasar el 100%
   - Si el cambio toca `voice_timing.py`, actualizar `test_voice_timing.py`
   - Si el cambio toca `generate_v2()`, actualizar `test_generate_v2.py` y `test_block_batch.py`
   - Si el cambio añade una fase nueva, crear su archivo de tests correspondiente

2. **Un cambio se considera "que afecta al algoritmo" cuando**:
   - Modifica cómo se calcula el target de palabras
   - Modifica el bucle de generación de bloques
   - Modifica la lógica de enriquecimiento o la duración estimada
   - Modifica la fase de scraping o los tipos de error
   - Modifica las defensas (zombie guard, orphan detector)
   - Modifica la aceptación/rechazo de scripts en el orchestrator
   - Modifica los prompts de generación o enrichment

3. **Para ejecutar los tests antes de un commit:**
   ```bash
   python3 -m pytest tests/ -v --tb=short
   ```

---

## Arquitectura

```
API POST /videos/generate
    │
    ▼
generation_service.start_generation_job()
    │
    ├─ Fase 0: Scrape  ──→ phase_scrape()       [sin timeout global]
    ├─ Fase 1: Script  ──→ phase_generate_script() [3600s]
    │   │
    │   ├─ get_unused_content(strategy="best_first")
    │   ├─ words_for_duration(canal, duration) → palabras_objetivo
    │   └─ generate_v2(content_item, palabras_objetivo)
    │       │
    │       ├─ Bucle de batches (max 50 iter, 10 empty strikes)
    │       │   └─ _generate_blocks_batch()  [prompt ligero, 2-4 bloques]
    │       │       └─ build_content_only_prompt()  [300 tokens]
    │       │
    │       └─ _enrich_blocks()  [prompt completo, solo metadatos]
    │           └─ duracion_estimada ← duration_for_words(canal, palabras_reales)
    │
    ├─ Fase 2: TTS     ──→ phase_tts()          [7200s, per-block progress]
    ├─ Fase 3: Media   ──→ phase_media()        [900s, multi-provider fallback]
    ├─ Fase 4: Video   ──→ phase_video()        [∞, MoviePy assembly]
    ├─ Fase 5: Metadata──→ phase_metadata()     [300s, SEO, non-fatal]
    └─ Fase 6: Upload  ──→ phase_upload()       [1800s, YouTube API]
```

---

## Fuente única de verdad: duración → palabras

```
voice_timing.py
    │
    ├─ voice_speed_factor(config)
    │   ├─ edge-tts: "-10%" → 1.10, "+5%" → 0.95
    │   └─ kokoro: 0.85 → 1.176, 1.0 → 1.0
    │
    ├─ words_per_minute_real(config) = 150 * factor
    │
    ├─ words_for_duration(config, minutes)  → palabras con colchón 20%
    │   └─ canal2, 14 min → 14 × 165 × 1.20 = 2772
    │
    └─ duration_for_words(config, word_count)  → minutos reales
        └─ canal2, 2772 palabras → 2772 / (165 × 1.20) = 14.0
```

**Regla:** toda la pipeline usa `voice_timing.py`. Nunca se usa el hardcodeo `150 palabras/minuto` directamente.

---

## Defensas

| Defensa | Mecanismo | Disparador |
|---------|-----------|------------|
| Zombie guard | `_broadcast_progress(status="running")` ignora si job ya `failed`/`completed` | Cada callback de progreso |
| Cooperative stop | `threading.Event` chequeado entre batches y fases | Timeout o cancelación del usuario |
| Orphan detector Type 1 | Job `running` > 60 min sin `finished_at` → `failed` | Cada 5 min |
| Orphan detector Type 2 | Video `generating` sin job activo → `error` | Cada 5 min |
| Orphan detector Type 3 | Video `error` + job `running` → job `failed` | Cada 5 min |
| PullPush circuit breaker | 3 fallos consecutivos → `degraded`, skip restantes | Durante scrape |
| Failure counter | 10 fallos consecutivos → aviso `SOURCE DEGRADED` | Cada fallo HTTP |
| Cancel endpoint | `POST /api/jobs/{id}/cancel` → `request_stop()` + `update_job(failed)` | Acción del usuario |

---

## Tabla de mapeo tests → código

| Archivo de test | Cubre |
|-----------------|-------|
| `test_voice_timing.py` | `config/voice_timing.py` — todas las funciones |
| `test_word_target.py` | `script_generator.py` — `_compute_word_target`, `_get_word_target` |
| `test_content_prompt.py` | `prompts/canal*_prompts.py` — `build_content_only_prompt` |
| `test_block_batch.py` | `script_generator.py` — `_generate_blocks_batch`, `_build_minimal_prompt` |
| `test_generate_v2.py` | `script_generator.py` — `generate_v2`, `generate` |
| `test_enrich_blocks.py` | `script_generator.py` — `_enrich_blocks` |
| `test_orchestrator.py` | `orchestrator.py` — `phase_generate_script` |
| `test_scrape_errors.py` | `scrapers/base.py`, `scrapers/reddit.py` — `_request`, circuit breaker |
| `test_scene_ranges.py` | `pipeline/video_editor.py` — `_enforce_scene_durations` |
| `test_content_ordering.py` | `database/db.py` — `get_unused_content` |
| `test_defenses.py` | `generation_service.py` — `_broadcast_progress` zombie guard |
| `test_pipeline_e2e.py` | Integración completa: voice_timing → target → generate_v2 → enrich |
