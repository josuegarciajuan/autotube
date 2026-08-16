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
| 9 | **Pollinations.ai** | Flux | ❌ Ninguna | Ilimitado (generoso) | ✅ Implementado | `pipeline/providers/pollinations_provider.py` |
| 10 | **SD 1.5 Local (CPU)** | SD 1.5 | ❌ Ninguna | CPU: 2-3 paralelo | ✅ Implementado | `pipeline/providers/local_sd_provider.py` |

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
1. Pollinations.ai     (sin auth, rápido, calidad media-alta)
2. Cloudflare Workers  (SDXL, alta calidad, limitado)
3. HuggingFace         (FLUX, máxima calidad, rate-limited)
4. SD 1.5 Local CPU    (ilimitado, lento, fallback último)
5. Pollo AI            (ya integrado, créditos limitados)
6. Stock images        (Pixabay/Unsplash, fallback final)
7. Stock videos        (Pexels/Pixabay/Mixkit/Coverr/YT)
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
