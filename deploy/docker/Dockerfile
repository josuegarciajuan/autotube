# Runtime TTS Kokoro (autotube) para el pool de SuperServer (M4).
#
# Imagen portable: sólo necesita un Docker que arranque (los nodos tienen
# Python 3.4–3.8 en el host, insuficiente para torch/kokoro). El código del
# proyecto y el modelo se MONTAN en solo lectura desde el nodo; el sandbox
# efímero va en /work.
#
#   docker run --rm --network=none \
#     -e HF_HOME=/app/.hfcache \
#     -v /run/taildeck/jobs/<id>:/work \
#     -v /opt/taildeck/autotube:/app:ro \
#     -w /work autotube-tts:1 \
#     python /app/api/services/tts_worker.py --request /work/push/in/request.json --out-dir /work/out
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 \
    PYTHONPATH=/app \
    HF_HOME=/hfcache

# espeak-ng: G2P de misaki para español. libsndfile1: soundfile/pydub.
# libgomp1: OpenMP de torch/scipy. ffmpeg: por si el pipeline lo requiere.
RUN apt-get update \
 && apt-get install -y --no-install-recommends espeak-ng libsndfile1 libgomp1 ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-tts.txt /tmp/requirements-tts.txt
# torch CPU PRIMERO (índice CPU, sin CUDA): así `kokoro`/`misaki` lo dan por
# satisfecho y no arrastran las nvidia-* (imagen ~2 GB menos).
RUN pip install --upgrade pip \
 && pip install torch --extra-index-url https://download.pytorch.org/whl/cpu \
 && pip install -r /tmp/requirements-tts.txt

WORKDIR /work
CMD ["python", "-V"]
