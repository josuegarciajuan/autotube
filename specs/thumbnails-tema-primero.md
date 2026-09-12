# Spec: generación de miniaturas tema-primero con diversidad real

Estado: aprobado (2026-09-11). Implementación por fases en worktree + merge.

## Problema

Las miniaturas de un mismo canal salen repetidas y poco correlacionadas con el
título: casi siempre un rostro con expresión de sorpresa exagerada como sujeto
principal y un fondo genérico. La identidad visual del canal se apoya en una
paleta de color fija (dorados en canal3, azules fríos en canal4, etc.) y en un
único layout (`dark_reveal`), de modo que todos los vídeos parecen variantes de
la misma imagen.

Causas raíz:

1. `pipeline/thumbnail_style_engine.py` cachea un único estilo por canal y su
   prompt pide "el MISMO estilo" para todas las miniaturas.
2. `THUMBNAIL_MANUAL_STYLE.base_composition` es `dark_reveal` en canal2/4/5 y el
   `ruins_reveal` de canal3 no existe en `LAYOUT_COMPOSITION`, así que cae al
   fallback `dark_reveal`.
3. `DEFAULT_FACE_DIRECTIVE` (MrBeast) y los `THUMBNAIL_CONCEPT_DIRECTIVE` de
   canal4/5 fuerzan rostro humano con sorpresa extrema como ancla; el tema real
   queda de fondo.
4. `_compose_final` colorea gradiente/borde/texto/badge con la paleta fija del
   canal: no hay color por vídeo.
5. El texto de miniatura usa fórmulas clichés ("SIN SANGRE | Nadie lo explicó").
6. No existe guardia anti-repetición entre miniaturas del mismo canal.

## Objetivo y criterio de éxito

- El sujeto principal de la imagen representa el contenido del título.
- Miniaturas consecutivas del mismo canal: layout distinto (sin repetir en las
  últimas `THUMBNAIL_LAYOUT_HISTORY_DEPTH`), tono de acento distinto
  (`thumbnail_color_key` guarda el hue del acento elegido) y overlay no
  duplicado.
- El color nace del contenido, no de una paleta de canal.
- La identidad corporativa se mantiene por reglas de composición, marco/borde,
  tipografía fija y sello/badge (no por logo ni overlays temáticos).
- Texto de alta atención sin clickbait (sin flechas ni círculos rojos).

## Decisiones de diseño

- **Cara:** protagonista solo si el tema es una persona (rostro real de stock,
  sin menores). En el resto, el tema manda; la cara es chip secundario opcional.
- **Color:** `THUMBNAIL_COLOR_MODE="image_content"`. Paleta dominante derivada
  de la imagen final; acento elegido para contrastar y evitar el tono dominante
  de los 2-3 vídeos previos.
- **Proveedores:** el LLM decide por sujeto. Persona/caso real → stock; escena
  conceptual → IA (Pollo); documento/objeto/archivo → stock. Tabla de decisión
  + ejecución determinista en el pipeline.
- **Texto:** palabra clave resaltada (caja/subrayado de acento), contorno grueso,
  tipografía del pool del canal. Sin señuelos tipo flecha/círculo.

## Arquitectura

### `pipeline/thumbnail_color.py` (nuevo)

Funciones puras y testeables:

- `dominant_palette(image, k=5) -> list[(r,g,b)]` — cuantización simple de la
  imagen (submuestreo + median-cut) sin dependencias nuevas.
- `hue_bucket(rgb) -> int` — 0..11 (30° por bucket).
- `hue_distance(a, b) -> int` — distancia circular en grados.
- `choose_accent(dominant, recent_hues, min_distance=40, prefer_palette=False)`
  — color de acento con contraste y distancia mínima a los tonos recientes.
- `contrast_text_color(bg_rgb) -> "#FFFFFF" | "#111111"` — legibilidad.
- `color_key(dominant) -> str` — etiqueta estable (p. ej. `hue_210`) para
  persistir y comparar.

### `pipeline/thumbnail_art_director.py` (nuevo)

Produce la dirección de arte por vídeo, integrada en `ThumbnailBrief`:

- `subject_type`: `person | place | object_artifact | concept | medical | event`.
- `face_role`: `protagonist` (solo si `subject_type == person`) | `secondary` |
  `none`.
- `primary_subject`: sintagma nominal concreto extraído de título+guion.
- `provider_plan`: `{"main": "ai"|"stock", "face": "stock"|"none",
  "inset": "stock"|"scene_image"|"none"}`.
- `layout`: elegido del pool del canal, evitando los últimos `depth` mediante
  memoria (rotación ponderada + desempate determinista por hash de vídeo).
- `color_intent`: intención emocional de color (no un hex fijo de canal).
- `emphasis_word`: palabra del overlay que se resalta.

El director usa el LLM si está disponible y cae a heurística determinista si no.
Recibe `recent_context` (layouts/tonos/sujetos previos) e instruye explícitamente
a no repetirlos.

### `pipeline/thumbnail_maker.py` (refactor)

- `LAYOUT_COMPOSITION` ampliado: `topic_hero`, `subject_closeup`,
  `artifact_document`, `split_diagonal`, `negative_space_top`, `center_burst`,
  `portrait_hero` (además de los actuales). Cada layout define zonas, insets,
  badge, peso de borde, gradiente y líneas de texto.
- `_compose_final` deja de tintar con `style["color_palette"]` cuando el modo es
  `image_content`: usa `thumbnail_color` sobre la imagen base + acento elegido.
- `_draw_channel_frame`: marco corporativo config-driven (esquinas, doble línea).
- `_draw_face_chip`: retrato circular pequeño (~15-20% de ancho) con anillo de
  acento, solo cuando `face_role == "secondary"` y hay rostro de stock.
- `_draw_emphasis`: caja/subrayado de acento tras `emphasis_word`.
- `_generate_subject_image`: respeta `provider_plan` (Pollo o stock). El rostro
  se obtiene de stock real; nunca IA para caras humanas.
- `make_viral_thumbnail` / `make_variant_thumbnails` aceptan `art_direction` y lo
  devuelven para que el recompose de `phase_metadata` sea estable (no re-sortea
  layout/color).

### Prompts y psicología

- `pipeline/thumbnail_brainstorm.py`: `DEFAULT_FACE_DIRECTIVE` →
  `DEFAULT_TOPIC_DIRECTIVE` (tema primero; cara solo si es persona real; emoción
  intensa no caricaturesca). Se inyecta `recent_context`.
- `pipeline/thumbnail_psychology.py`: reglas tema-primero, color-desde-contenido,
  un solo foco, anti-patrón (cara genérica / fondo sin relación), texto ≤3
  palabras con sustantivo concreto.
- `pipeline/metadata_generator.py`: prompt de `thumbnail_text` con formatos
  concretos (pregunta, cifra, contraste) y prohibición de clichés.
- `api/services/packaging_policy.py`: lista de claims prohibidos ampliada +
  `validate_thumbnail_diversity`.

## Datos y diversidad

- Migración idempotente **v55**: `videos.thumbnail_color_key`,
  `thumbnail_face_role`, `thumbnail_subject` (TEXT DEFAULT '').
- `ExtendedDatabase.update_video_thumbnail_style(video_id, style, layout, *,
  color_key="", face_role="", subject="")` (retrocompatible).
- `ExtendedDatabase.get_recent_thumbnail_context(channel_id, limit=3)`.
- `validate_thumbnail_diversity(new, recent, config)`: layout no repetido en las
  últimas `depth`, `hue_distance` de acento ≥ `min_distance`, overlay no
  casi-duplicado. Advisorio con un reintento; nunca bloquea la subida.

## Configuración

`config/defaults.py` (valores por defecto sensatos):

- `THUMBNAIL_COLOR_MODE = "image_content"`
- `THUMBNAIL_FACE_ROLE = "auto"`
- `THUMBNAIL_LAYOUT_POOL = [...]`
- `THUMBNAIL_LAYOUT_HISTORY_DEPTH = 3`
- `THUMBNAIL_ACCENT_HUE_DISTANCE_MIN = 40`
- `THUMBNAIL_EMPHASIS_ENABLED = True`
- `THUMBNAIL_TYPOGRAPHY_POOL = ["DejaVuSans-Bold", ...]`
- `THUMBNAIL_FRAME_STYLE = "corner_marks"`
- `THUMBNAIL_THEMATIC_OVERLAYS_ENABLED = False`

`config/canal{2,3,4,5}_config.py`: marco, tipografía, sello, subconjunto de
layouts y política de cara. Se neutraliza la parte "cara exagerada" de
`THUMBNAIL_CONCEPT_DIRECTIVE`; `color_palette` queda como referencia legacy.

## Integración

- `orchestrator.phase_video`: brief + provider plan + recent_context.
- `orchestrator.phase_metadata`: recompose con `art_direction` persistido.
- `api/services/full_pipeline_worker.py`: variantes adaptan la nueva firma sin
  activar A/B.
- `api/services/thumbnail_service.py` y `scripts/reupload_missing_thumbnails.py`.
- `pipeline/frame_thumb.py`: intacto.

## Tests

- Unit: `thumbnail_color` (buckets, distancia, acento, contraste), rotación de
  layout sin repetir, mapeo `subject_type → face_role`, `validate_thumbnail_diversity`,
  `_draw_emphasis`/`_draw_face_chip` no rompen composición.
- Integración: N miniaturas por canal con LLM/Pollo mockeados → N layouts
  distintos, hue buckets distintos, overlays no duplicados, marco presente.
- Actualizar `tests/test_thumbnail_packaging.py` (guardias `secondary_scene`
  siguen vigentes; test de config nueva).
- Manual: `python3 test_video.py --canal canal2 --skip-scrape --quick` + contacto.

## Riesgos

- Compatibilidad: `THUMBNAIL_COLOR_MODE="channel_palette"` mantiene el
  comportamiento legacy; pools vacíos caen a defaults.
- Coste: sin A/B, 1 generación + recompose (reutiliza imagen base).
- Anti-strike: solo miniaturas; no se toca subida, descripción ni `content_safety`.
