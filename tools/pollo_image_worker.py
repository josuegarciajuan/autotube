#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pollo_image_worker.py
---------------------
Worker de generacion de imagenes via Pollo.ai (endpoint tRPC web).

Compatibilidad:
- Modo antiguo: genera 1 imagen con --output-image
- Modo batch: genera N imagenes en una sola tarea con --num-outputs y --output-dir
- Descarga la version sin watermark cuando Pollo expone la URL original
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

try:
    from PIL import Image
    import numpy as np
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from curl_cffi import requests as cffi_requests
    HTTP_BACKEND = "curl-cffi"
except ImportError:
    try:
        import cloudscraper  # noqa: F401
        HTTP_BACKEND = "cloudscraper"
    except ImportError:
        HTTP_BACKEND = "none"

BASE_URL = "https://pollo.ai/api/trpc"
POLL_INTERVAL = 4
SUCCESS_GRACE_SECONDS = 36
UNKNOWN_FAILURE_GRACE_POLLS = 6  # tolerancia a flapping de Pollo.ai: 6 polls × 4s = 24s de gracia antes de rendirse
MAX_PROMPT_CHARS = 2000
COMMON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://pollo.ai/app/ai-image",
    "Origin": "https://pollo.ai",
    "x-trpc-source": "nextjs-react",
}
MODEL_CONFIG = {
    "pollo-image-v2": {
        "name": "Pollo Image v2",
        "modelName": "pollo-image-v2",
        "aspectRatio": "1:1",
    },
    "pollo-image-v1-6": {
        "name": "Pollo Image v1.6",
        "modelName": "pollo-image-v1-6",
        "aspectRatio": "1:1",
    },
    "flux-dev": {
        "name": "FLUX Dev (Black Forest Labs)",
        "modelName": "flux-dev",
        "aspectRatio": "2:3",
    },
    "seedream": {
        "name": "Seedream (ByteDance)",
        "modelName": "seedream",
        "aspectRatio": "2:3",
    },
    "nano-banana": {
        "name": "Nano Banana (Google Gemini)",
        "modelName": "nano-banana",
        "aspectRatio": "4:3",
    },
}


def compact_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def extract_cookie_value(cookie_str):
    cookie_str = (cookie_str or "").strip()
    prefix = "__Secure-next-auth.session-token="
    if cookie_str.startswith(prefix):
        return cookie_str[len(prefix):]
    return cookie_str


def make_client(cookie_value):
    if HTTP_BACKEND == "curl-cffi":
        session = cffi_requests.Session(impersonate="chrome110")
        session.headers.update(COMMON_HEADERS)
        session.cookies.set(
            "__Secure-next-auth.session-token",
            cookie_value,
            domain="pollo.ai",
            secure=True,
        )
        return session
    if HTTP_BACKEND == "cloudscraper":
        import cloudscraper
        session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "linux", "mobile": False}
        )
        session.headers.update(COMMON_HEADERS)
        session.cookies.set(
            "__Secure-next-auth.session-token",
            cookie_value,
            domain="pollo.ai",
            secure=True,
        )
        return session
    raise RuntimeError("No hay libreria HTTP compatible. Instala: pip install curl-cffi")


def parse_trpc(resp, context):
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError("Respuesta no es JSON en %s: %s" % (context, str(resp.text)[:400]))

    if isinstance(data, list):
        try:
            return data[0]["result"]["data"]["json"]
        except Exception:
            pass
        try:
            return data[0]["result"]["data"]
        except Exception:
            pass
    if isinstance(data, dict):
        return data
    raise RuntimeError("Estructura tRPC inesperada en %s: %s" % (context, str(data)[:400]))


def create_generation(client, prompt, model_cfg, aspect_ratio, num_outputs):
    url = BASE_URL + "/text2Image.create?batch=1"
    payload = {
        "prompt": prompt,
        "modelName": model_cfg["modelName"],
        "aspectRatio": aspect_ratio or model_cfg["aspectRatio"],
        "entryCode": "web",
        "numOutputs": int(num_outputs),
    }
    body = {"0": {"json": payload}}
    resp = client.post(url, json=body, timeout=30)

    if resp.status_code == 401:
        raise RuntimeError("Sesion caducada (401). Renueva la cookie en Josue > ConfigM.")
    if resp.status_code == 403:
        raise RuntimeError("Acceso denegado (403). curl-cffi no pudo pasar Cloudflare.")
    if resp.status_code not in (200, 201):
        raise RuntimeError("HTTP %s: %s" % (resp.status_code, str(resp.text)[:800]))

    inner = parse_trpc(resp, "text2Image.create")
    gen_id = inner.get("id") or inner.get("generationId")
    if not gen_id:
        raise RuntimeError("No se recibio ID de generacion: %s" % str(inner)[:400])
    return str(gen_id)


def find_first_url(obj):
    if isinstance(obj, str) and obj.startswith(("http://", "https://")):
        return obj
    if isinstance(obj, dict):
        preferred = [
            "noWatermarkUrl", "withoutWatermarkUrl", "originalUrl", "oriUrl",
            "originUrl", "downloadUrl", "videoUrl", "imageUrl", "url",
            "cover", "thumbnail", "src", "sourceUrl", "outputUrl",
        ]
        for key in preferred:
            val = obj.get(key)
            if isinstance(val, str) and val.startswith(("http://", "https://")):
                return val
            if isinstance(val, list):
                for item in val:
                    url = find_first_url(item)
                    if url:
                        return url
        for _, val in obj.items():
            url = find_first_url(val)
            if url:
                return url
    if isinstance(obj, list):
        for item in obj:
            url = find_first_url(item)
            if url:
                return url
    return ""


def add_original_download_query(url):
    if not url:
        return ""
    if "type=download" in url and "format=original" in url:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "type=download&quality=high&format=original"


def collect_values_for_keys(obj, wanted_keys, found=None, limit=40):
    if found is None:
        found = {}
    if len(found) >= int(limit):
        return found
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in wanted_keys and value not in (None, ""):
                bucket = found.setdefault(key, [])
                if value not in bucket:
                    bucket.append(value)
            collect_values_for_keys(value, wanted_keys, found, limit=limit)
    elif isinstance(obj, list):
        for value in obj:
            collect_values_for_keys(value, wanted_keys, found, limit=limit)
            if len(found) >= int(limit):
                break
    return found


def build_nowatermark_payload_groups(item, generation_id=None):
    specific = []
    generic = []
    seen_specific = set()
    seen_generic = set()
    id_keys = [
        "videoId", "id", "generationId", "recordId", "assetId",
        "taskId", "projectId", "resourceId", "mediaId",
    ]
    url_keys = ["videoUrl", "imageUrl", "url", "cover", "thumbnail", "downloadUrl"]

    def push(target, seen, payload):
        serial = compact_json(payload)
        if serial in seen:
            return
        seen.add(serial)
        target.append(payload)

    values = collect_values_for_keys(item, set(id_keys + url_keys), limit=60)
    for key in id_keys:
        for value in values.get(key, []):
            push(specific, seen_specific, {"0": {"json": {key: value}}})
            if generation_id not in (None, ""):
                push(specific, seen_specific, {"0": {"json": {key: value, "generationId": generation_id}}})
                push(specific, seen_specific, {"0": {"json": {key: value, "id": generation_id}}})
    for key in url_keys:
        for value in values.get(key, []):
            push(specific, seen_specific, {"0": {"json": {key: value}}})
            if generation_id not in (None, ""):
                push(specific, seen_specific, {"0": {"json": {key: value, "generationId": generation_id}}})

    if generation_id not in (None, ""):
        push(generic, seen_generic, {"0": {"json": {"id": generation_id}}})
        push(generic, seen_generic, {"0": {"json": {"generationId": generation_id}}})
        push(generic, seen_generic, {"0": {"json": {"videoId": generation_id}}})

    return specific, generic

def extract_output_items(result):
    candidates = []
    for key in ["generations", "outputs", "results", "images", "assets", "records", "imageList", "videoList", "items", "medias"]:
        items = result.get(key) if isinstance(result, dict) else None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    candidates.append(item)
                elif isinstance(item, str):
                    candidates.append({"url": item})
    direct_url = find_first_url(result)
    if direct_url and not candidates:
        candidates.append({"url": direct_url})
    return candidates


def count_output_items(result):
    try:
        return len(extract_output_items(result))
    except Exception:
        return 0


def poll_generation(client, gen_id, timeout_sec, expected_outputs=1):
    params = compact_json({"0": {"json": {"id": int(gen_id)}}})
    url = BASE_URL + "/generation.queryRecordDetail?batch=1&input=" + urllib.parse.quote(params)

    elapsed = 0
    timeout_total = int(timeout_sec) + (SUCCESS_GRACE_SECONDS if int(expected_outputs or 1) > 1 else 0)
    last_success_inner = None
    failed_unknown_polls = 0

    while elapsed < timeout_total:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        try:
            resp = client.get(url, timeout=15)
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        try:
            inner = parse_trpc(resp, "generation.queryRecordDetail")
        except Exception:
            continue

        status = str(inner.get("status") or inner.get("state") or "").lower()
        output_count = count_output_items(inner)

        if status in ("succeed", "success", "completed", "done", "finished"):
            last_success_inner = inner
            failed_unknown_polls = 0
            if int(expected_outputs or 1) <= 1 or output_count >= int(expected_outputs or 1):
                return inner
            continue

        if status in ("failed", "error", "cancelled"):
            reason = str(inner.get("failReason") or inner.get("error") or "").strip()
            if reason == "":
                reason = "desconocido"
            if reason.lower() in ("desconocido", "unknown", "null", "none") and failed_unknown_polls < UNKNOWN_FAILURE_GRACE_POLLS:
                failed_unknown_polls += 1
                continue
            raise RuntimeError("La generacion fallo: %s" % reason)

        failed_unknown_polls = 0

    if last_success_inner is not None:
        actual = count_output_items(last_success_inner)
        if int(expected_outputs or 1) > 1 and actual < int(expected_outputs or 1):
            raise RuntimeError(
                "La generacion termino, pero solo devolvio %s de %s imagenes tras esperar %ss." % (
                    actual,
                    int(expected_outputs or 1),
                    timeout_total,
                )
            )
        return last_success_inner

    raise RuntimeError("Timeout tras %ss esperando imagen(es)." % timeout_total)


def get_nowatermark_url(client, item, generation_id=None, used_urls=None, allow_generic_fallback=True):
    used_urls = set(used_urls or [])

    explicit = collect_values_for_keys(
        item or {},
        set(["noWatermarkUrl", "withoutWatermarkUrl", "originalUrl", "oriUrl", "originUrl", "downloadUrl"]),
        limit=20,
    )
    for bucket in explicit.values():
        for val in bucket:
            if isinstance(val, str) and val.startswith(("http://", "https://")):
                url = add_original_download_query(val)
                if url not in used_urls:
                    return url

    endpoint = BASE_URL + "/video.getVideoNoWatermarkUrl?batch=1"
    specific_payloads, generic_payloads = build_nowatermark_payload_groups(item or {}, generation_id=generation_id)

    def try_payloads(payloads):
        duplicate_hit = False
        for payload in payloads:
            try:
                resp = client.post(endpoint, json=payload, timeout=20)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            try:
                data = parse_trpc(resp, "video.getVideoNoWatermarkUrl")
            except Exception:
                continue
            url = add_original_download_query(find_first_url(data))
            if not url:
                continue
            if url in used_urls:
                duplicate_hit = True
                continue
            return url, duplicate_hit
        return "", duplicate_hit

    url, duplicate_hit = try_payloads(specific_payloads)
    if url:
        return url

    base_urls = collect_values_for_keys(item or {}, set(["videoUrl", "imageUrl", "url", "cover", "thumbnail"]), limit=20)
    for bucket in base_urls.values():
        for base_url in bucket:
            if isinstance(base_url, str) and base_url.startswith(("http://", "https://")):
                candidate = add_original_download_query(base_url)
                if candidate and candidate not in used_urls:
                    return candidate

    if allow_generic_fallback or not duplicate_hit:
        url, _ = try_payloads(generic_payloads)
        if url:
            return url
    return ""

def detect_ext(raw, content_type):
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    if raw[:3] == b'\xff\xd8\xff':
        return "jpg"
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return "webp"
    if raw[:6] in (b'GIF87a', b'GIF89a'):
        return "gif"
    ctype = (content_type or "").lower()
    if "webp" in ctype:
        return "webp"
    if "jpeg" in ctype or "jpg" in ctype:
        return "jpg"
    if "png" in ctype:
        return "png"
    return "bin"


def apply_realism_postprocess(image_path):
    if not HAS_PIL:
        return
    try:
        img = Image.open(image_path).convert("RGB")
        arr = np.array(img, dtype=np.float32)
        noise = np.random.normal(0, 1.2, arr.shape).astype(np.float32)
        arr = arr + noise
        arr = np.clip(arr, 0, 255).astype(np.uint8)
        img = Image.fromarray(arr)
        img.save(image_path, "JPEG", quality=92, optimize=True)
    except Exception:
        pass


def fetch_url_bytes(url, client=None):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0.0.0",
            "Referer": "https://pollo.ai/",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except Exception:
        if client is None:
            raise
        resp = client.get(url, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError("HTTP %s al descargar" % resp.status_code)
        return resp.content, resp.headers.get("Content-Type", "")


def save_single_output(url, output_path, client=None):
    raw, content_type = fetch_url_bytes(url, client=client)
    if not raw:
        raise RuntimeError("El CDN devolvio respuesta vacia.")
    ctype = (content_type or "").lower()
    if "text/html" in ctype or "application/json" in ctype:
        raise RuntimeError("El CDN devolvio %s en lugar de imagen" % content_type)
    head = raw[:16]
    is_image = (
        head[:8] == b"\x89PNG\r\n\x1a\n" or
        head[:3] == b"\xff\xd8\xff" or
        (head[:4] == b"RIFF" and head[8:12] == b"WEBP") or
        head[:6] in (b"GIF87a", b"GIF89a")
    )
    if not is_image and len(raw) < 5000:
        raise RuntimeError("Los bytes descargados no parecen una imagen: %s" % raw[:20].hex())
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "wb") as fh:
        fh.write(raw)
    apply_realism_postprocess(output_path)
    return output_path, len(raw), detect_ext(raw, content_type)


def save_multiple_outputs(client, result, generation_id, output_dir, output_prefix, expected_outputs=1):
    items = extract_output_items(result)
    if not items:
        raise RuntimeError("Generacion terminada pero no se encontraron outputs.")
    os.makedirs(output_dir, exist_ok=True)
    saved = []
    used_urls = set()
    download_errors = []
    expected_outputs = int(expected_outputs or 1)

    for index, item in enumerate(items, 1):
        last_error = ""
        for attempt in range(1, 5):
            url = get_nowatermark_url(
                client,
                item,
                generation_id=generation_id,
                used_urls=used_urls,
                allow_generic_fallback=(attempt >= 3),
            )
            if not url:
                last_error = "No se pudo resolver URL sin watermark"
                time.sleep(2)
                continue
            try:
                raw, content_type = fetch_url_bytes(url, client=client)
            except Exception as exc:
                last_error = str(exc)
                time.sleep(2)
                continue
            if not raw:
                last_error = "El CDN devolvio respuesta vacia"
                time.sleep(2)
                continue
            ctype = (content_type or "").lower()
            if "text/html" in ctype or "application/json" in ctype:
                last_error = "El CDN devolvio %s en lugar de imagen" % content_type
                time.sleep(2)
                continue
            ext = detect_ext(raw, content_type)
            output_path = os.path.join(output_dir, "%s%02d.%s" % (output_prefix, index, ext))
            with open(output_path, "wb") as fh:
                fh.write(raw)
            apply_realism_postprocess(output_path)
            saved.append({
                "index": index,
                "path": output_path,
                "url": url,
                "size_bytes": len(raw),
                "ext": ext,
            })
            used_urls.add(url)
            last_error = ""
            break
        if last_error:
            download_errors.append({"index": index, "error": last_error})

    saved.sort(key=lambda item: int(item.get("index") or 0))
    if expected_outputs > 1 and len(saved) < expected_outputs:
        detail = "; ".join(["%s:%s" % (err.get("index"), err.get("error")) for err in download_errors[:6]])
        if detail:
            raise RuntimeError(
                "La generacion termino, pero solo se pudieron descargar %s de %s imagenes sin watermark. Detalle: %s" % (
                    len(saved), expected_outputs, detail
                )
            )
        raise RuntimeError(
            "La generacion termino, pero solo se pudieron descargar %s de %s imagenes sin watermark." % (
                len(saved), expected_outputs
            )
        )
    return saved

def cmd_generate(args):
    result = {
        "ok": False,
        "error": "",
        "image_path": "",
        "image_url": "",
        "generation_id": "",
        "images": [],
        "backend": HTTP_BACKEND,
    }
    try:
        if HTTP_BACKEND == "none":
            raise RuntimeError("No hay libreria HTTP disponible. Instala: pip install curl-cffi")

        cookie_value = extract_cookie_value(args.cookie)
        if not cookie_value:
            raise RuntimeError("Cookie vacia. Configurala en Josue > ConfigM.")

        prompt_len = len(args.prompt or "")
        result["prompt_length"] = prompt_len
        if prompt_len > MAX_PROMPT_CHARS:
            raise RuntimeError("El prompt supera el limite de %d caracteres para Pollo.ai (%d)." % (MAX_PROMPT_CHARS, prompt_len))

        model_key = args.model if args.model in MODEL_CONFIG else "flux-dev"
        model_cfg = MODEL_CONFIG[model_key]
        client = make_client(cookie_value)
        num_outputs = max(1, int(args.num_outputs or 1))
        result["requested_num_outputs"] = num_outputs

        gen_id = create_generation(
            client,
            args.prompt,
            model_cfg,
            args.aspect_ratio or model_cfg["aspectRatio"],
            num_outputs,
        )
        result["generation_id"] = str(gen_id)
        poll_result = poll_generation(client, gen_id, int(args.timeout or 300), expected_outputs=num_outputs)

        if num_outputs <= 1:
            url = get_nowatermark_url(client, poll_result, generation_id=gen_id)
            if not url:
                items = extract_output_items(poll_result)
                if items:
                    url = get_nowatermark_url(client, items[0], generation_id=gen_id)
            if not url:
                raise RuntimeError("Generacion terminada pero no se encontro URL de imagen sin watermark.")
            output_path = args.output_image
            if not output_path:
                raise RuntimeError("Falta --output-image para la generacion individual.")
            image_path, size_bytes, ext = save_single_output(url, output_path, client=client)
            result["ok"] = True
            result["image_path"] = image_path
            result["image_url"] = url
            result["image_size_bytes"] = size_bytes
            result["image_ext"] = ext
            result["images"] = [{
                "index": 1,
                "path": image_path,
                "url": url,
                "size_bytes": size_bytes,
                "ext": ext,
            }]
        else:
            output_dir = args.output_dir
            if not output_dir:
                raise RuntimeError("Falta --output-dir para la generacion por lotes.")
            output_prefix = args.output_prefix or "candidate_"
            images = save_multiple_outputs(
                client,
                poll_result,
                gen_id,
                output_dir,
                output_prefix,
                expected_outputs=num_outputs,
            )
            if not images:
                raise RuntimeError("La generacion termino, pero no se pudo descargar ninguna imagen sin watermark.")
            result["ok"] = True
            result["images"] = images
            result["image_count"] = len(images)
            result["image_path"] = images[0]["path"]
            result["image_url"] = images[0]["url"]

    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)

    json_out = json.dumps(result, ensure_ascii=False)
    if args.output_json:
        try:
            out_dir = os.path.dirname(os.path.abspath(args.output_json))
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(args.output_json, "w") as fh:
                fh.write(json_out)
        except Exception:
            pass
    print(json_out)
    return 0 if result.get("ok") else 1


def main():
    parser = argparse.ArgumentParser(description="Pollo.ai image worker")
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("generate")
    gen.add_argument("--cookie", required=True, help="Cookie de sesion completa")
    gen.add_argument("--prompt", required=True, help="Prompt de texto")
    gen.add_argument("--model", default="flux-dev", help="Modelo Pollo.ai")
    gen.add_argument("--aspect-ratio", default="", help="Ratio (2:3, 4:3, 1:1...)")
    gen.add_argument("--output-image", default="", help="Ruta donde guardar la imagen individual")
    gen.add_argument("--output-dir", default="", help="Directorio donde guardar imagenes batch")
    gen.add_argument("--output-prefix", default="candidate_", help="Prefijo para archivos batch")
    gen.add_argument("--num-outputs", type=int, default=1, help="Numero de imagenes a generar")
    gen.add_argument("--style-mode", default="", help="Compatibilidad: ignorado")
    gen.add_argument("--output-json", default="", help="Ruta donde guardar el JSON de resultado")
    gen.add_argument("--timeout", type=int, default=300, help="Timeout base en segundos")

    args = parser.parse_args()
    if args.command == "generate":
        sys.exit(cmd_generate(args))
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()