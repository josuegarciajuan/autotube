# AI Image Providers — Registry

Registro completo de todos los proveedores de assets visuales del pipeline Autotube.
Incluye tanto los proveedores de stock (video/imagen) como los de generación IA.

---

## Proveedores actuales — Stock (no IA)

| # | Nombre | Tipo | Auth | API Key env | Rate limit | Detalles |
|---|--------|------|------|-------------|------------|----------|
| 1 | **Pexels Videos** | Video stock | API key | `PEXELS_API_KEY` | 200 req/h | `pipeline/providers/pexels.py` |
| 2 | **Pixabay Videos** | Video stock | API key | `PIXABAY_API_KEY` | 100 req/h | `pipeline/providers/pixabay.py` |
| 3 | **Mixkit** | Video stock | Ninguna | — | Web scrape | `pipeline/providers/mixkit.py` |
| 4 | **Coverr** | Video stock | Ninguna | — | Web scrape | `pipeline/providers/coverr.py` |
| 5 | **YouTube CC** | Video stock | Ninguna | — | yt-dlp | `pipeline/providers/youtube_cc.py` |
| 6 | **Pixabay Photos** | Imagen stock | API key | `PIXABAY_API_KEY` | 100 req/min | `pipeline/image_fetcher.py` |
| 7 | **Unsplash** | Imagen stock | API key | `UNSPLASH_ACCESS_KEY` | 50 req/h | `pipeline/image_fetcher.py` |

## Proveedores actuales — IA

| # | Nombre | Tipo | Auth | Rate limit | Coste | Detalles |
|---|--------|------|------|------------|-------|----------|
| 8 | **Pollo AI** | Imagen IA | Cookie sesión | Créditos limitados | Créditos | `pipeline/providers/pollo_image.py` + `pipeline/ai_image_generator.py` |

## Nuevos proveedores — IA (Fase 1: sin registro)

| # | Nombre | Modelo | Auth | Rate limit | ¿Ya funciona? | Detalles |
|---|--------|--------|------|------------|---------------|----------|
| 9 | **Pollinations.ai** | Flux | ❌ Ninguna | Ilimitado (generoso) | ⚠️ 402 (oct 2026) | `pipeline/providers/pollinations_provider.py` |
| 10 | **SD 1.5 Local (CPU)** | SD 1.5 | ❌ Ninguna | CPU: 2-3 paralelo | ✅ Implementado | `pipeline/providers/local_sd_provider.py` |

> **Nota (oct 2026):** la API anónima de Pollinations responde **HTTP 402 Payment
> Required**, así que SD 1.5 Local es el proveedor IA efectivo. Para descargar la
> casa, la generación SD se reparte por la flota con la definición
> **`autotube-ai-image`** de SuperServer (flag `AUTOTUBE_DIST_IMAGES=1`, fallback
> local). Ver `AGENTS.md` §Ejecución distribuida y el doc de SuperServer `projects/autotube.md`.
>
> **Equivalencia local ↔ flota.** La ruta distribuida es un espejo funcional de
> la local: mismos pesos (mismo snapshot SD 1.5), mismo código
> (`LocalSDProvider` + `AIImageUpscaler`), misma petición
> (`media_fetcher._build_ai_request`), mismo post-proceso/resolución e integración.
> No hay igualdad bit-a-bit (SD es estocástico; `torch` 2.14 en contenedor vs 2.13
> en la casa; `diffusers 0.39` coincide). Semilla determinista por escena según
> `spec/20 §4`. Verificación: `scripts/verify_dist_image_equivalence.py` y
> `tests/test_ai_image_request.py`.

## Stable Horde (gratis, crowdsourced) — implementado oct 2026

| Nombre | Modelo | Auth | Rate limit | Coste | Detalles |
|--------|--------|------|------------|-------|----------|
| **Stable Horde** | `stable_diffusion` (configurable) | API key anónima `0000000000` o cuenta gratis | Concurrencia (30), sin rate limit duro | $0 | `pipeline/providers/stable_horde_provider.py` |

`https://stablehorde.net` es un clúster GPU comunitario y gratuito. Flujo
**asíncrono** v2: `POST /generate/async` (responde **202** con `id`) →
`GET /generate/check/{id}` → `GET /generate/status/{id}` (imagen en base64
**WEBP**) → `DELETE /generate/status/{id}`. El provider convierte WEBP→JPEG y
upscalea con `AIImageUpscaler`.

**Cuenta:** no es obligatoria (key anónima `0000000000`, prioridad mínima). El
registro es **gratis** en <https://aihorde.net/register> (pseudónimo sin OAuth,
o con Google/GitHub/Discord) y da mejor prioridad. Key en `.env`:
`STABLE_HORDE_API_KEY=<key>` (si vacío → anónima).

**Límites medidos (cuenta 0 kudos, oct 2026):**
- `width`/`height` deben ser **múltiplos de 64**.
- Peticiones > **692×692** (o presupuesto de sampler) exigen **kudos por
  adelantado** → una cuenta 0-kudos usa 640×384 por defecto y reintenta a
  576×320 si el API lo rechaza.
- Latencia real medida: **~130 s/imagen** (cola + render). La imagen se upscalea
  a la resolución mínima del canal.

**Config (`config/defaults.py`):** `ai_stable_horde_model`,
`ai_stable_horde_width/height`, `ai_stable_horde_steps`, `ai_stable_horde_cfg_scale`,
`ai_stable_horde_timeout_sec`, `ai_stable_horde_poll_sec`.
Cadena por defecto: `["pollinations", "stable_horde", "local_sd"]`.

## Nuevos proveedores — IA (Fase 2: requieren cuenta gratuita)

| # | Nombre | Modelo | Auth | Rate limit | Estado | Detalles |
|---|--------|--------|------|------------|--------|----------|
| 11 | **Cloudflare Workers AI** | SDXL | Cuenta gratis Cloudflare | ~300-1000/día | 🔜 Pendiente | Necesita `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` |
| 12 | **HuggingFace Inference** | FLUX.1-dev | Token HF gratis | Rate-limited | 🔜 Pendiente | Necesita `HF_TOKEN` |

### Instrucciones para crear cuentas (Fase 2)

**Cloudflare Workers AI:**
1. Crear cuenta en https://dash.cloudflare.com/sign-up (gratis, sin tarjeta)
2. Ir a **AI** → **Workers AI** → **Use REST API**
3. Copiar **Account ID** y crear un **API Token** con permisos `Workers AI: Edit`
4. Guardar en `.env`: `CLOUDFLARE_ACCOUNT_ID=xxx`, `CLOUDFLARE_API_TOKEN=yyy`

**HuggingFace:**
1. Crear cuenta en https://huggingface.co/join (gratis, sin tarjeta)
2. Ir a https://huggingface.co/settings/tokens → New token → tipo `fine-grained`
3. Permiso: `Make calls to Inference Providers`
4. Guardar en `.env`: `HF_TOKEN=hf_xxx`

---

## Cadena de fallback (orden de prioridad planificado)

```
1. Pollinations.ai     (sin auth, rápido; oct 2026 casi siempre 402)
2. Stable Horde        (gratis crowdsourced, ~130 s/img, sin coste)   ← oct 2026
3. SD 1.5 Local CPU    (ilimitado, ~2-3 min/img, sin red)
4. Cloudflare Workers  (SDXL, alta calidad, limitado)   [pendiente]
5. HuggingFace         (FLUX, máxima calidad, rate-limited) [pendiente]
6. Pollo AI            (créditos agotados — descartado)
7. Stock images        (Pixabay/Unsplash, fallback final)
8. Stock videos        (Pexels/Pixabay/Mixkit/Coverr/YT)
```

---

## Metadatos comparativos (valores estimados — se refinarán con benchmarks)

| Provider | Calidad (1-10) | Latencia | RAM | CPU | Coste/img | Resolución real | Rate limit | Límite diario |
|----------|---------------|----------|-----|-----|-----------|-----------------|------------|---------------|
| Pollinations (anonymous) | 7.0 | 1.5s | 0 | 0 | $0 | 1024×576 (cap) | 4/min (1/15s) | ❌ Ninguno |
| Pollinations (Seed, gratis) | 7.0 | 1.5s | 0 | 0 | $0 | 1024×576 (cap) | 12/min (1/5s) | ❌ Ninguno |
| Cloudflare | 8.0 | 5-15s | 0 | 0 | $0 | 1024×1024 | ~300-1000/día | 300-1000/día |
| HuggingFace | 8.5 | 10-30s | 0 | 0 | $0 | 1024×1024 | Rate-limited | ~50-100/día |
| SD 1.5 Local | 6.5 | 220-670s | ~4.5GB | ~3 cores | $0 | 768×768 | Ilimitado | ❌ Ninguno |
| Pollo AI | 7.5 | 300-420s | 0 | 0 | créditos | 1024×1024 | Según créditos | Según créditos |
| Stock (Pixabay) | 5.0 | <1s | 0 | 0 | $0 | Variable | API rate | API rate |

---

*Última actualización: 2026-08-12 — Fase 1 completada (Pollinations + SD Local)*

---

## Aviso: Pollinations legacy tras muro de pago x402 (oct 2026)

A partir de ~28-sep-2026 el endpoint anónimo de Pollinations
(`image.pollinations.ai/prompt`) dejó de servir imágenes de forma sostenida:
la primera petición responde `200` y las siguientes devuelven **`402 Payment
Required`** con un challenge **x402** en la cabecera `payment-required`
(pago en USDC, `serviceName: "Pollinations legacy image"`). No es un rate
limit: espaciar peticiones (probado a 16 s) no lo evita, ni `referrer`.

**Diagnóstico (reproducible):**

```bash
# 1ª OK, resto 402
for i in 1 2 3; do
  curl -s -o /dev/null -w "%{http_code}\n" \
    "https://image.pollinations.ai/prompt/test$i?width=512&height=512&model=flux&nologo=true"
done
```

**Solución soportada:** registrar una cuenta gratuita (tier *Seed*) en
<https://auth.pollinations.ai> y exportar el token. El provider lo envía como
`?token=` y como cabecera `Authorization: Bearer`:

```env
POLLINATIONS_TOKEN=<token>
# opcional, identifica la app (para el modo web/referrer)
POLLINATIONS_REFERRER=autotube
# disyuntor tras un 402 (s) — evita martillear la API por escena
POLLINATIONS_X402_COOLDOWN_SEC=1800
```

Sin token, el provider activa un **disyuntor** al primer 402 y cede el paso al
fallback (flota SD / local SD), en vez de reintentar en cada escena.

**Recomendación operativa:** mientras no haya token, la ruta robusta es la
**flota SD** (`AUTOTUBE_DIST_IMAGES=1`), que no depende de terceros. Ver
`config/defaults.py::ai_image_providers` (orden `["pollinations", "local_sd"]`).

