# Integridad de narración (shorts + long-form)

Fecha: nov 2026. Origen: reporte de audio "raro" en shorts (frases saltadas /
narración incoherente). La auditoría encontró cuatro causas independientes que
podían combinarse.

## Causas y fixes

| # | Causa | Dónde | Fix |
|---|-------|-------|-----|
| 1 | El guion se recortaba por palabras desde el final de cada bloque (LLM devolvía 95-245 palabras vs tope 92). Quedaban fragmentos ("No eran simples ofrendas: los"). | `api/services/shorts_scheduler.py`, `pipeline/shorts_tts.py` | `trim_blocks_sentences`: solo elimina **oraciones completas**; si no cabe, `needs_regeneration` y se regenera el guion. Presupuesto real (prosodia) inyectado en el prompt. Un único pase (se eliminaron los 3 recortes duplicados). |
| 2 | `edge-tts` añade ~1 s de padding por bloque; el offset acumulado usaba el último `WordBoundary` → SRT/escenas ~6 s por detrás del audio. | `pipeline/shorts_tts.py` | Se carga el MP3 del bloque, se recorta el silencio final (`audio_utils.trim_trailing_silence`) y se avanza con la **duración real**. Aplica también a `tts_engine.py` (long-form edge). |
| 3 | Clips de stock más cortos que su escena no se repetían; la cadena `xfade` colapsaba y `-shortest` cortaba la voz (39% de shorts; casos de 21 s con 45 s de narración). | `pipeline/shorts_media.py` | Vídeo: `-stream_loop -1 -t {visual_dur}`. Validación post-render: `validate_short_render(expected_duration_sec=mp3)`; si falta voz → error (el slot reintenta, nunca sube truncado). |
| 4 | Long-form: `_compute_block_ranges` calculaba cada escena con la duración de sus palabras y **descartaba las pausas entre bloques**; el body quedaba 8-26 s más corto y `with_duration(body_vid_dur)` + `-shortest` cortaban el cierre. `_assert_body_timeline_sync` lo trataba como "silencio final benigno". | `pipeline/video_editor.py`, `orchestrator.py` | Escenas **contiguas** (fin de bloque = inicio del siguiente) y sub-escenas contiguas. `_finalize_scene_ranges` valida contra la duración real. `_assert_body_timeline_sync` falla si el gap > `LONGFORM_MAX_TRAILING_AUDIO_SEC` (2 s). |
| 5 | `segmentos` del LLM omitían oraciones; `_sanitize_segmentos` aceptaba ≥85% del bloque y TTS narraba solo los segmentos. | `pipeline/script_generator.py`, `config/voice_resolver.py` | Cobertura ~100% (ordenada). Si falla, se narra el `texto` completo; guarda defensiva en `split_block_segments`. |
| 6 | Un bloque TTS fallido se omitía (`continue`) con solo un warning. | `pipeline/shorts_tts.py`, `tts_engine.py`, `kokoro_tts.py` | Aborta/conmuta (`TTS_FAIL_ON_BLOCK_ERROR=true`). |

## Invariantes

- `audio_MP4 ≥ duración_MP3 − 1.5 s` (shorts).
- Guion: cada bloque narrado termina en frontera de oración; nunca fragmentos.
- `|último timestamp − duración MP3| ≤ 1.5 s` (solo silencio final).
- Long-form: `sum(escenas) ≈ span de narración`; gap de cola ≤ 2 s.
- `segmentos` cubren el 100% del bloque o se ignoran.
- TTS: 0 bloques omitidos.

## Diagnóstico y recuperación

```bash
# Auditoría read-only (shorts + long-form)
python3 scripts/audit_narration_integrity.py --days 30 --json-out reports/narracion.json

# Recuperar shorts en cola (NO publicados) con MP4 truncado
python3 scripts/refix_truncated_shorts.py            # dry-run
python3 scripts/refix_truncated_shorts.py --apply    # re-render + backup

# Largos no publicados: el audit los lista; decidir hold/re-render (costoso)
```

- `refix_truncated_shorts.py` usa `short_asset_history` (orden `id ASC`) y split
  uniforme sobre la duración real del MP3; copia el original a
  `output/videos/shorts/_truncated_backup/`. No re-sube ni cambia el estado.
- Los standalone no se pueden recuperar (su audio era temporal y se borró).

## Kill-switches

`SHORTS_LOOP_SHORT_VIDEO_CLIPS`, `SHORTS_SENTENCE_TRIM`,
`LONGFORM_CONTIGUOUS_SCENES`, `TTS_TRIM_TRAILING_SILENCE`,
`TTS_FAIL_ON_BLOCK_ERROR` (default `true`), `LONGFORM_MAX_TRAILING_AUDIO_SEC`
(default 2.0), `SHORTS_RENDER_DURATION_SLACK_SEC` (default 1.5).

## Tests

`tests/test_narration_integrity.py` cubre recorte por oraciones, cobertura de
`segmentos`, escenas contiguas/sub-escenas, validación de render truncado,
recorte de silencio y aborto de TTS. Ejecutar junto con
`tests/test_scene_timing.py`, `tests/test_short_render_validation.py`,
`tests/test_shorts_renderer_regression.py`, `tests/test_standalone_shorts_fixes.py`.
