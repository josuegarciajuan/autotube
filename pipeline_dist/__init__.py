"""Pipeline de creación de vídeo distribuido (paralelo al pipeline clásico).

Este paquete es puramente aditivo: no modifica `orchestrator.py`,
`pipeline/video_editor.py` ni `api/services/full_pipeline_worker.py`. El objetivo
es pre-renderizar escenas en los nodos de SuperServer y dejar que el ensamblado
legacy siga produciendo el MP4 final sin cambios.

Ver `scripts/run_distributed_video.py` para el punto de entrada del piloto.
"""

__all__ = ["SCHEMA_VERSION"]

from .model import SCHEMA_VERSION  # noqa: E402
