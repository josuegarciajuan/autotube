# Narración expresiva (prosodia por tono)

## Problema

La voz narraba de forma uniforme: la prosodia TTS solo dependía del `tipo` de
bloque (hook/desarrollo/climax/reflexion/cierre). El LLM ya etiquetaba una
`emocion` por bloque, pero **ningún motor TTS la leía**. Además el botón de
escuchar cada voz del panel no funcionaba porque los MP3 de preview eran
ficheros estáticos que no existían (`output/` es gitignored).

## Solución

Capa de narración expresiva con un **tono canónico** por bloque y, opcionalmente,
por sub-frase (`segmentos`). El tono modula `rate`, `pitch`, `volume` y una
`pause_after_ms` insertada entre segmentos.

```
Guion LLM ──> bloque{texto, tipo, emocion, tono?, segmentos?}
                    │
        PROSODY_PROFILES (defaults + override por canal en config_json)
                    │
        voice_resolver ──> per-segmento {rate,pitch,volume,pause_after_ms}
                    │
        tts_engine (edge) / kokoro_tts ──> MP3 + word timestamps
                    │
        Panel "Voz y Narración" + Voice Lab (preview runtime cacheado)
```

## Vocabulario de tonos

`neutro, suspense, misterio, tension, revelacion, asombro, tristeza,
esperanza, reflexion, enfasis, cierre` (definido en `config/defaults.py:TONO_CATALOG`).

Resolución del tono (prioridad): `tono` canónico explícito → keywords de
`emocion` → mapa por `tipo` → `TONO_DEFAULT`.

## Adaptación automática del tono (sin elección manual)

El usuario **solo elige la voz** (Santa, Jorge, ...). El tono de cada
párrafo/bloque se decide en el algoritmo con esta prioridad:

1. `tono` canónico emitido por el LLM al enriquecer el guion (contexto real).
2. Keywords de `emocion` (meta ya existente en guiones antiguos).
3. **Inferencia por contenido del texto** (`infer_tono_from_text`): señales
   léxicas y de puntuación (revelación, tensión, misterio, tristeza...).
4. Mapa por `tipo` de bloque (hook/climax/cierre...).
5. `TONO_DEFAULT`.

Esto hace que la narración varíe por párrafo incluso en guiones generados antes
de esta feature (sin `tono`), o cuando `emocion` viene vacía. Los botones de
tono del panel son **solo prueba de audio**; no seleccionan nada. El editor de
prosodia calibra cómo suena cada tono cuando el algoritmo lo detecta.

## Config (heredable, override por canal)

En `config/defaults.py`:

- `EXPRESSIVE_NARRATION` (bool, kill-switch)
- `TONO_CATALOG`, `TONO_DEFAULT`
- `TONO_MAX_SEGMENTS_PER_BLOCK`
- `PROSODY_PROFILES`: `{tono: {rate, pitch, volume, pause_after_ms}}`

Se serializa por `config_bridge` (sin cambios de schema DB). Cada canal puede
sobrescribir tonos concretos en su `config/{slug}_config.py` o desde el panel.

## Motores

- **edge-tts** (`pipeline/tts_engine.py`): un `Communicate` por segmento con
  `rate/pitch/volume` del tono; pausas con `AudioSegment.silent`; timestamps
  acumulados igual que antes.
- **Kokoro** (`pipeline/kokoro_tts.py`): `speed` por segmento derivado del
  `rate` del tono (`rate_to_speed`), pausa de tono + pausa de párrafo; se
  mantiene el batching/unload para RAM.
- **Shorts** (`pipeline/shorts_tts.py`): mismo resolver; pausa por tono en vez
  de la constante fija.

## Capa LLM

`script_generator._enrich_block_fields_batch` pide `tono` canónico y, opcional,
`segmentos` (`[{texto, tono}]`). `_sanitize_segmentos` descarta segmentos que no
reproduzcan el texto del bloque. Los prompts de shorts (`shorts_native.py`,
`shorts_scheduler.py`) también piden `tono`.

## Preview runtime (arregla el botón ▶)

`api/services/voice_preview_service.py` sintetiza con el mismo motor/prosodia
que producción y cachea en `output/voice_previews/cache/{hash}.mp3`. Endpoints
en `api/routers/voices.py`:

- `GET /api/voices` → catálogo (22 voces) + `tones`.
- `GET /api/voices/clip?engine&voice&tone&slug` → clip por tono.
- `GET /api/voices/relato?engine&voice&slug` → relato largo que recorre todos los tonos.
- `POST /api/voices/preview` → texto libre.

Catálogo ampliado: 3 Kokoro + 19 edge-tts en español (España, México, Argentina,
Colombia, Chile, Perú, Venezuela, EE.UU., Cuba).

## Panel

- `VoiceSelector.tsx`: usa `api.getVoices()`, reproduce vía `/voices/clip` y
  muestra errores reales.
- `VoiceLab.tsx`: botones por tono + "Relato largo".
- `ProsodyEditor.tsx`: tabla de edición de `PROSODY_PROFILES`.
- `ChannelDetail.tsx`: sección Voz con `EXPRESSIVE_NARRATION`, prosodia, tono por
  defecto y pausa Kokoro.

## Limitación

`edge-tts` gratuito no soporta estilos Azure (`mstts:express-as`, whisper, etc.).
El suspense/tristeza se aproximan con `rate/pitch/volume` + pausas + elección de
voz. Para emoción de estudio habría que integrar un proveedor con estilos.

## Verificación

- `python3 -m pytest tests/test_expressive_narration.py`
- `cd frontend && npx tsc --noEmit`
- `GET /api/voices/clip` y `/api/voices/relato` devuelven audio.
- Invariantes intactos: lock TTS por canal, una sola generación long-form,
  egress por cuenta y `content_safety` no se tocan.
