# Visión local (josue.ink/ia) para el análisis de thumbnails

`pipeline/viral_cloner.py::_analyze_thumbnail_vision` analiza la miniatura de un
vídeo viral para extraer su composición. Usa **cualquier endpoint
compatible-OpenAI**; ya **no fuerza** `api.openai.com` y **ya no intenta
DeepSeek** como *fallback* multimodal (DeepSeek no es multimodal).

La selección del proveedor es 100 % por entorno y **reversible**: si no se
define nada, se mantiene el comportamiento histórico (OpenAI `gpt-4o-mini`).

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `VISION_BASE_URL` | `https://api.openai.com/v1` | Base URL compatible-OpenAI. |
| `VISION_MODEL` | `gpt-4o-mini` | Modelo/alias de visión. |
| `VISION_API_KEY` | `OPENAI_API_KEY` | Clave del proyecto/endpoint. |
| `VISION_MAX_IMAGE_MB` | `8` | Tamaño máximo de la imagen antes de enviarla; por encima se degrada a `None` con aviso. |
| `VISION_JSON_MODE` | `true` | Si es `true`, envía `response_format={"type":"json_object"}`. Ponlo a `false` si el backend lo rechaza. |

## Activar la API central de IA local

Añade al `.env` real (NO está en git, nunca se commitea):

```env
VISION_BASE_URL=http://127.0.0.1:8140/v1
VISION_MODEL=local-vision
VISION_API_KEY=<clave del proyecto "autotube" de la API de IA>
# opcionales
# VISION_MAX_IMAGE_MB=8
# VISION_JSON_MODE=true
```

La clave de proyecto se entrega al dar de alta el proyecto en `josue.ink/ia`
(API máquina `GET/POST /v1/...`, `Authorization: Bearer <clave>`). No la
incluyas en `.env.example` ni en commits.

> **Nota:** `docs/API.md` de `/root/ia` documenta el puerto `8120` como ejemplo,
> pero la instancia real (`/root/ia/config.local.json`) escucha en el **8140**.
> Confirma el puerto con `curl -s localhost:8140/v1/models -H "Authorization: Bearer $IA_KEY"`.

## Volver a OpenAI

Elimina (o comenta) las tres variables `VISION_BASE_URL`, `VISION_MODEL` y
`VISION_API_KEY`: los defaults de `config/settings.py` restauran OpenAI.

## Robustez (degradación a `None`)

`_analyze_thumbnail_vision` devuelve `dict | None`. Devuelve `None` —sin romper
la generación; el *caller* usa un prompt genérico— cuando:

- no hay `VISION_API_KEY`;
- el fichero no se puede leer;
- el formato no es jpeg/png/webp (se detecta por *magic bytes*, no se asume jpeg);
- la imagen supera `VISION_MAX_IMAGE_MB`;
- el proveedor falla;
- la respuesta no es un objeto JSON válido;
- el JSON no respeta el esquema (tipos de `description`, `objects`, `colors`,
  `composition`, `text_overlay`, `emotion`, `style`).

Nunca se registran en el log la imagen en base64 ni credenciales.

## Tests

```sh
python3 -m pytest tests/test_viral_vision_local.py -q
```
