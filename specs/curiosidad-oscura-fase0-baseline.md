# Fase 0 — Línea base y criterios del experimento «curiosidad oscura»

> Fecha de corte: **2026-10-23**. Estado: **aprobada como referencia del piloto**.
> Regenerable en cualquier momento con:
> `python3 scripts/baseline_curiosidad.py --db /root/autotube/autotube.db --days 90`
> (solo lectura, 0 cuota de YouTube API, sin red).

## 1. Hipótesis editorial (consensuada)

> **Una historia concreta, inquietante y con consecuencias humanas puede atraer
> más que una descripción general de un tema.**

Estructura por pieza: **tema propio del canal → anomalía concreta →
consecuencia humana → pregunta pendiente.**

Reglas duras:

- La coherencia con el nicho del canal es **requisito**, no factor intercambiable.
- Toda afirmación del título/miniatura debe estar **respaldada por el guion y las
  fuentes**. Se permite exageración expresiva, nunca cambios factuales
  (una posibilidad no se presenta como hecho; una muerte no confirmada no se afirma).
- El packaging (título + miniatura) **promete**; el arranque del vídeo **cumple**.
- No activar `STATS_AUTO_COLLECT`: el seguimiento usa datos ya recolectados y la
  recolección manual existente.

**Proporción del piloto:** ~50 % de los temas de cada canal con el giro
«curiosidad oscura»; el otro 50 % mantiene la orientación habitual **con la
misma mejora de packaging** (para no confundir «tema nuevo» con «miniatura
arreglada»).

## 2. Línea base — long-form (ventana 90 días, 2026-07-25 → 2026-10-23)

Medianas por canal (fuente: `video_reach_daily` + `video_stats_history`):

| Canal | Vídeos | Views 7d (med) | Views 28d (med) | CTR 28d (med) | Retención (med) | Duración media vista |
|---|---:|---:|---:|---:|---:|---:|
| canal2 (Sincronías) | 141 | 0 | **0** | 0,00 % | 12,1 % | 83 s |
| canal3 (Civilizaciones) | 130 | 1 | **5,5** | 1,78 % | 22,0 % | 177 s |
| canal4 (Expediciones) | 142 | 0 | **2** | 1,45 % | 23,2 % | 155 s |
| canal5 (Anomalías Med.) | 112 | 1 | **4** | 1,05 % | 16,6 % | 129 s |

**Lectura:** el vídeo long-form *típico* del sistema recibe entre **0 y 6
visitas en 28 días**. La mediana de CTR (1,0–1,8 %) está por debajo del rango
habitualmente considerado sano (2–10 %). El problema de atracción es
**sistémico, no puntual** — exactamente lo que el plan ataca.

### Referencias de rendimiento (top por views 28d en la ventana)

| Vídeo | Canal | Pub. | V 7d | V 28d | Impresiones | CTR | Ret. | AVD |
|---|---|---|---:|---:|---:|---:|---:|---:|
| El K2: la montaña que devora a quienes se atreven | canal4 | 10-08 | 171 | 278 | 2 075 | 2,99 % | 21,7 % | 317 s |
| Yosemite High Route: supervivencia | canal4 | 10-12 | 129 | 208 | 1 290 | 4,57 % | **46,3 %** | **701 s** |
| ¿Qué borraron de la historia humana? | canal3 | 10-19 | 193 | 193 | 2 710 | 1,99 % | 16,1 % | 220 s |
| El misterio de la civilización más antigua | canal3 | 10-11 | 165 | 165 | 2 472 | 1,94 % | 24,8 % | 292 s |
| ¿Y si la humanidad ya existió antes…? | canal3 | 10-17 | 109 | 109 | 2 983 | 3,08 % | 31,3 % | 310 s |

Referencia histórica fuera de ventana (la que motivó la hipótesis):
**«La posada donde NADIE salía con vida»** (canal3, ago-2026): 1 251 views
acumuladas, CTR ponderado ~10,8 % en los días con `reporting_reach` disponible,
retención 17,1 % → **modelo de atracción**. Yosemite → **modelo de permanencia**.

## 3. Línea base — shorts (ventana 90 días)

| Canal | Shorts | Views (mediana, último valor) |
|---|---:|---:|
| canal2 | 323 | 58 |
| canal3 | 344 | 134 |
| canal4 | 343 | 133 |
| canal5 | 343 | 168 |

Nota: para shorts no hay tabla diaria de alcance; la mediana usa el último
`views` de `short_stats` por short. En la Fase 6 se decide si basta o se
instrumenta ventana diaria.

## 4. Registro de fallos de packaging observados (muestra real)

### 4.1 Texto de miniatura mutilado / incoherente

| ID | Canal | Título | `thumbnail_text` guardado | Fallo |
|---|---|---|---|---|
| 2733 | canal2 | ¿Viajeros del tiempo reales? los 10 casos que la ciencia… | `10 CASOS \| LA CALLÓ` | L2 mutilada: la dedup de tokens borró «CIENCIA» (está en el título) y dejó «LA CALLÓ» sin sujeto |
| 2649 | canal3 | El secreto mejor guardado de la selva: la civilización | `BAJO LA SELVA \| LOS` | L2 reducida a «LOS» (residuo tras dedup) |
| 2726 | canal2 | Simulación o realidad: el trilema de Nick Bostrom… | `CAMBIARÍAN \| LA GRIETA QUE NO CIERRA` | L1 sin ancla clara al tema |
| 2735 | canal4 | Hms Erebus y Terror: nadie sobrevivió a la expedición | `HMS \| EL HIELO LOS BORRÓ` | L1 débil («HMS») |
| 2697 | canal3 | ¿Quién construyó las maravillas imposibles…? | `QUIÉN \| BAJO LA ARENA` | L1 = «QUIÉN», palabra vacía (saliente mal elegida) |
| 2737 | canal2 | Sincronicidad: Jung, Pauli y los milagros… | `JUNG Y PAULI \| FIRMARON LO IMPOSIBLE` | L2 semánticamente extraña |

**Causa raíz identificada (a corregir en Fase 2):**
`pipeline/packaging_brief.py::build_overlay_spec` elimina de L2 cualquier token
presente en el título o en L1 (anti-repetición), lo que rompe frases correctas;
además `thumbnail_maker.py` aplica `[:budget]` como red de seguridad y una ruta
de pintado usa tamaño fijo sin ajuste al ancho real.

### 4.2 Títulos almacenados cortados a mitad de frase (fallo nuevo detectado)

| ID | `titulo_final` (tal cual en DB) | len |
|---|---|---:|
| 2733 | «¿Viajeros del tiempo reales? los 10 casos que la ciencia» | 56 |
| 2594 | «Lo que las civilizaciones antiguas esconden: enigmas que aún» | 60 |
| 2480 | «El misterio del sistema solar exterior que la NASA aún no logra» | 63 |
| 2602 | «¿Qué esconden las ciudades perdidas del Amazonas y el verdadero?» | 64 |

Los títulos se truncan al `TITLE_MAX_CHARS` (~60-65) **sin completar la idea**.
Se registra como modo de fallo adicional a tratar en Fase 2/5 (el recorte debe
ser reformulación, no tijera).

### 4.3 Incoherencia imagen ↔ tema

- Vídeo 2733 (viajeros del tiempo): miniatura = camiseta «oatmilk» sobre fondo
  negro. Ninguna relación con el tema. A revisar en Fase 2 (coherencia visual).

## 5. Definición de la comparación (cómo juzgaremos el piloto)

- **Medianas por canal y formato**, nunca un ganador aislado.
- Ventanas comparables: views/CTR a **7 y 28 días** desde la publicación real
  (`COALESCE(actual_published_at, published_at)`).
- Métrica ausente ≠ 0: un vídeo sin datos de impresiones es «sin datos», no
  «CTR 0 %».
- Clasificación por vídeo (coherente con `editorial_reviews`):
  `no_impressions` → `datos_insuficientes` → `ctr_bajo` → `ctr_ok_retencion_baja`
  → `atraccion_y_permanencia_ok`.
- Largos y shorts **nunca** se agregan juntos.
- Grupo piloto vs control dentro del mismo canal y semanas (misma cadencia).

### Criterios de éxito del piloto (a evaluar en Fase 7)

1. **Atracción:** mediana de views 28d del grupo piloto ≥ 2× la de su control
   en el mismo canal, o CTR 28d mediano ≥ +1 pp con impresiones suficientes.
2. **Permanencia:** la retención mediana del piloto **no** cae más de 3 pp
   respecto a su control.
3. **Calidad de packaging:** 0 miniaturas nuevas con frase mutilada, texto
   desbordado o imagen ajena al tema en la muestra de revisión.
4. **Sin regresiones operativas:** el piloto no altera cadencia, cuota ni
   invariantes de seguridad (pacing, content-safety, concurrencia).

Si (1) se cumple y (2) no, la promesa o el arranque se revisan antes de ampliar.
Si solo mejora CTR pero no views absolutas, se revisa la demanda del tema
(Fase 4) antes de tocar más el packaging.

## 6. Limitaciones conocidas de esta línea base

- `video_reach_daily` cubre desde 2026-08-22; ventanas 28d de vídeos publicados
  antes están parcialmente vacías (no afecta a la ventana elegida).
- El CTR de «La posada» es histórico y parcial (fuera de ventana): se usa como
  inspiración cualitativa, no como dato del experimento.
- Las publicaciones de 2026-10-22/23 aún no tienen ventana: entran como 0 y se
  excluyen solas al medianearse en cortes posteriores con más madurez.
- `average_view_duration` de shorts no está disponible en `short_stats`:
  mediana `null` registrada a propósito.
