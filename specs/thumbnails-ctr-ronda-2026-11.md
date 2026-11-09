# Ronda CTR de miniaturas (nov 2026)

Estado: implementado. Complementa `specs/thumbnails-tema-primero.md`.

## Problema medido

- CTR real (`video_reach_daily`, impr≥200, n=163): media ponderada **1,89%**,
  mediana 1,73%, rango 0,37%–8,72%.
- El mejor caso (`2063`, 8,72%) no lo producía el pipeline: era un original
  recuperado con rostro extremo, alto contraste y “NADIE” dorado.
- Fallos sistemáticos: textos cortados (“ERA UNA CIUDAD BAJO LA”,
  “3.000 METROS SIN”), L2 descartado en layouts de una línea, texto sobre el
  sujeto, imágenes aplastadas a negro (94/150 en la auditoría), tres acentos,
  badge de 26 px e imagen de otro país (Cachemira/Australia).
- El A/B compartía imagen base y subía siempre la variante 1: medía copy, no
  packaging, y nunca usaba el overlay curado en metadata.

## Decisiones

- **Una sola fuente de verdad del texto**: `packaging_brief._truncate_words`
  nunca corta palabras ni deja conectores colgantes; si L2 no aporta dato nuevo
  (cifra, unidad, lugar o consecuencia) se elimina.
- **Legibilidad de feed**: L1 ≥15 px y L2 ≥11 px a 100 px de alto; badge 40 px
  de lienzo (~5,5 px en feed); sin boosts ocultos de contraste (×1,3 eliminado)
  y con topes de gradiente/viñeta.
- **Composición**: L2 se pinta aunque el layout se diseñara para una línea; la
  zona de texto se elige por saliencia y se desplaza si pisa una cara; panel
  automático solo cuando el fondo es claro.
- **Imagen**: recorte 16:9 por energía de bordes; guardia de relevancia
  geográfica que reescribe el prompt si el título y el sujeto no comparten lugar.
- **Caras**: `THUMBNAIL_FACE_ROLE_POLICY="person_only"`. La cara solo protagoniza
  cuando la persona es la historia; en el resto no se usa cara secundaria.
- **Selección**: `make_variant_thumbnails` devuelve primero la variante con mejor
  QC; el worker sube esa, no la variante 1. La rotación A/B de miniaturas
  publicadas queda apagada (`THUMBNAIL_AB_ROTATION_ENABLED=false`).

## Implementación

- `pipeline/thumbnail_qc.py` (nuevo): score 0-100, issues (`text_dangling_*`,
  `text_overlaps_subject`, `too_dark`, `l1_too_small`, `low_text_contrast`,
  `too_many_accents`…). Funciones puras + Pillow/numpy.
- `scripts/thumbnail_qc_audit.py` (nuevo): auditoría read-only de miniaturas
  existentes, 0 cuota.
- Migración v74: `thumbnail_qc_score`, `thumbnail_qc_issues`,
  `thumbnail_pipeline_version`, `thumbnail_overlay_l1/l2`.
- `pipeline/thumbnail_relevance.py` (nuevo): entidades de lugar + reescritura
  del prompt con la ubicación correcta y prohibición de mapas/banderas ajenos.
- `pipeline/thumbnail_maker.py`: corte por saliencia, detección de cara para QC,
  zona de texto, panel, topes de oscurecimiento, badge legible, selección por QC.
- `pipeline/packaging_brief.py`: `_truncate_words` seguro, `_has_new_information`,
  validaciones de línea colgante y de L2 sin dato.
- `pipeline/thumbnail_art_director.py`: política `person_only`.
- `config/defaults.py`: nuevos topes y flags; canal3 pasa a `DejaVuSerif-Bold`
  y canal4 a `NimbusSansNarrow-Bold`.
- `api/services/ab_test_worker.py`: no rota miniaturas publicadas.

## Invariantes

1. Ninguna miniatura sube con una línea terminada en artículo/preposición.
2. Ninguna miniatura sube con L2 si ese L2 no aporta información nueva.
3. La imagen nunca muestra un lugar distinto al del título (fails open cuando
   el lugar es desconocido para el léxico).
4. La rotación A/B no toca miniaturas ya publicadas mientras el flag esté off.
5. El QC es advisory en la primera semana; después puede bloquear con fallback
   a plantilla canónica.

## Verificación

```bash
python3 -m pytest tests/test_thumbnail_qc.py tests/test_thumbnail_packaging.py \
  tests/test_thumbnail_tema_primero.py tests/test_packaging_brief.py -q
AUTOTUBE_MEDIA_ROOT=/root/autotube DATABASE_PATH=/root/autotube/autotube.db \
  python3 scripts/thumbnail_qc_audit.py --days 60
```
