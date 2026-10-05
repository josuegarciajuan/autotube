# Calidad y coherencia visual — Plan de implementación

> Estado: **en ejecución (Fases 0–4)**. Worktree `work/20261005-calidad-coherencia`.
> Objetivo: que lo que se ve corresponda a lo que se narra, dentro del mundo del
> vídeo (época/lugar), con más clips (objetivo 80 % del tiempo visual, fallback a
> imagen) y mejor nitidez, **sin romper** el pipeline funcional.

## Principios

1. Coherencia antes que cantidad o resolución.
2. Extender lo existente (`ThemeContext`, `VisualBible`, `SceneVisualContext`,
   `era_terms`, `cinematic_staging`, `ScriptValidator`).
3. Todo comportamiento nuevo nace **apagado** en `config/defaults.py` y se activa
   por `channels.config_json` (contrato multi-canal; nada hardcodeado por slug).
4. Fail-open y acotado (máx. llamadas/candidatos). Nunca bloquear el pipeline.
5. Invariantes intactas: un solo long-form concurrente, dedup/unicidad de escenas,
   guards de RAM; sin tocar planificación, publicación, OAuth, egress, anti-strike.
6. Entrega por worktree, commits atómicos, merge a la rama de producción y
   `scripts/apply_changes.sh`.

## Fases

### Fase 0 — Baseline y auditoría (medir, no cambiar)
- `scripts/audit_video_media_quality.py` (read-only): por vídeo/últimos N vuelca
  escena → narración, `search_query_en`, query elegida, proveedor, tipo, ruta,
  resolución original, motivo de fallback y % de vídeo (por tiempo y por escena).
- `media_fetcher._asset_decision_record()`: log estructurado por escena.
- Flag: `ASSET_DECISION_LOG_ENABLED` (default `True`).

### Fase 1 — Defectos claros e información perdida
- `image_fetcher.py`: propagar `width`, `height`, `description`.
- `media_fetcher._search_image_provider_page`: conservar esas dimensiones en el
  candidato (hoy se descartan).
- `_download_candidate` / `_download_image`: registrar dimensiones reales;
  `quality_flag` si se usó `webformatURL` (~640 px).
- Preferir descarga grande; si `width < min_stock_image_width`, intentar el
  siguiente candidato/proveedor dentro de presupuesto; aceptar con aviso al agotar.
- `scripts/check_asset_logos.py`: detectar overlay de logo/marca en esquinas para
  identificar el asset y el proveedor exactos.
- Config: `prefer_large_download`, `min_stock_image_width`, `min_stock_image_height`.

### Fase 2 — Guiones: promesa, progresión y honestidad
- `prompts/base_prompts.py`: reforzar gancho concreto, progresión (cada bloque
  aporta información nueva), separación hecho/hipótesis/recreación y acción.
- `script_validator.py`: nuevos checks como *warnings con score* (sin failover):
  promesa del gancho, novedad entre bloques, preguntas retóricas, hedge vs claim.
- `script_generator.py`: reparación editorial **máx. 1 llamada**, conservar el
  original si no mejora.
- Flags: `SCRIPT_EDITORIAL_REVIEW_ENABLED` (default `False`),
  `SCRIPT_EDITORIAL_REVIEW_MAX_CALLS` (default `1`).

### Fase 3 — Contexto global y por escena
- `scene_context.py`: poblar `prev_snippet`/`next_snippet`; campos `subject`,
  `action`, `object`, `setting`, `must_show`, `must_avoid`, `depiction_mode`.
- `visual_bible.py` + `build_visual_bible_prompt`: campos por escena y alineación
  estricta (si faltan entradas, derivar del `fragment_text` de la subescena).
- `theme_extractor.py`: `temporal_segments` (saltos de época deliberados).
- `era_terms.py`: excepción de anacronismo por escena/segmento.
- Flag: `THEME_TEMPORAL_OVERRIDES_ENABLED` (default `True`).

### Fase 4 — Búsqueda y selección coherente
- `cinematic_staging.py`: `_simplify_query` conserva el verbo/acción; `rank_candidates`
  integra acción + contexto + época.
- `media_fetcher.py`: pool con la query de **acción exacta** primero y ancla de
  época obligatoria; unificar el orden (hoy `rank_candidates` y `_relevance_score`
  se pisan); `must_avoid` penaliza; escalera de fallback explícita con
  `max_generic_fallback_pct`.
- `_classify_scenes`: preferir vídeo en todo el runtime y en escenas con acción;
  tope progresivo hacia 80 % (piloto) con fallback a imagen.
- Verificación visual opcional `pipeline/visual_verifier.py` en modo observación
  (`VISUAL_VERIFY_MODE=off|observe|enforce`, default `off`), acotada y fail-open.
- Config: `action_scene_boost`, `require_action_match`, `max_generic_fallback_pct`,
  `target_video_time_pct`.

## Claves de configuración nuevas (defaults)

`ASSET_DECISION_LOG_ENABLED`, `SCRIPT_EDITORIAL_REVIEW_ENABLED`,
`SCRIPT_EDITORIAL_REVIEW_MAX_CALLS`, `THEME_TEMPORAL_OVERRIDES_ENABLED`,
`MIN_IMAGE_USABLE_WIDTH`, `LOW_RES_ZOOM_CLAMP`, `VISUAL_VERIFY_MODE`,
y en `MEDIA_STRATEGY`: `prefer_large_download`, `min_stock_image_width`,
`min_stock_image_height`, `action_scene_boost`, `require_action_match`,
`max_generic_fallback_pct`, `target_video_time_pct`.

## Criterios de aceptación

- Relevancia narración↔escena mejor en la muestra; sin anacronismos evidentes.
- Fallback a imagen correcto cuando no hay clip adecuado.
- Menos borrosidad y marcas visibles.
- Sin fallos nuevos en tests + render smoke; tiempo/coste dentro de presupuesto.
- Cada fase con kill-switch y rollback sin borrar assets ni detener trabajos.
