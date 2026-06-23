# Contratos — Autotube

## API / DB Contracts (Fase A)

### Flujo de creación de video (nuevo)

**Antes (Fase A):** cada generación creaba 3-4 registros `videos` (API + phase_video + phase_upload + uploader._log_to_db). Solo uno tenía `yt_video_id`.

**Después (Fase A):** una sola generación = 1 registro `videos`, trackeado desde la API.

```
POST /api/videos/generate {channel_id, action}
  → INSERT videos (status='generating', video_path='pending') → video_id
  → PipelineOrchestrator(db_video_id=video_id)
     → phase_video: UPDATE videos SET ... WHERE id=video_id  (no INSERT nuevo)
     → phase_upload: UPDATE videos SET ... WHERE id=video_id  (no INSERT nuevo)
  → mark_video_uploaded(video_id, yt_video_id, yt_url)        (guarda yt en el registro trackeado)
```

**Modo CLI (sin cambios):** `PipelineOrchestrator(db_video_id=None)` → `insert_video` en cada fase (compatibilidad preservada).

### Fase B: Derivación de duración desde config del canal

**Campo canónico:** `VIDEO_OPTIMAL_DURATION_MINUTES` (int, minutos). Ambos canales = 10.

**Algoritmo (rama producción, TEST_MODE=False):**
- `duration_target = VIDEO_OPTIMAL_DURATION_MINUTES`
- `words_min = duration_target × 150 × 0.85`
- `words_max = duration_target × 150 × 1.15`
- `blocks_min = max(5, duration_target × 1.5)`
- `blocks_max = max(8, duration_target × 2.1)`
- `duration_max = duration_target × 1.4`

**TEST_MODE preservado** para pruebas rápidas (200-400 palabras, ~1 min).
- Canal 3: registros huérfanos 27 y 29 eliminados; yt_video_id movido de 29 → 26.
- Canal 3 queda con ids 15, 21, 26 (todos con yt_video_id y status='uploaded').
