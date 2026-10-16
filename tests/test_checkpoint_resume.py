"""Fase 6: retroceso de fase al reanudar si faltan dependencias del checkpoint."""
from api.services.full_pipeline_worker import (
    _PHASE_INDEX, _PHASE_ORDER, _required_resume_idx,
)


def test_reanudar_en_video_sin_audio_retrocede_a_tts():
    idx = _required_resume_idx(_PHASE_INDEX["video"], has_audio=False,
                               has_media=True, has_video=True)
    assert _PHASE_ORDER[idx] == "tts"


def test_audio_ok_pero_media_ausente_retrocede_a_media():
    idx = _required_resume_idx(_PHASE_INDEX["video"], has_audio=True,
                               has_media=False, has_video=True)
    assert _PHASE_ORDER[idx] == "media"


def test_audio_y_media_ok_pero_video_ausente_retrocede_a_video():
    # Reanudando en 'metadata' con el vídeo borrado → hay que re-renderizar.
    idx = _required_resume_idx(_PHASE_INDEX["metadata"], has_audio=True,
                               has_media=True, has_video=False)
    assert _PHASE_ORDER[idx] == "video"


def test_reanudar_en_video_con_video_ausente_no_cambia():
    # Ya se reanuda en 'video': no hay nada que retroceder.
    assert _required_resume_idx(_PHASE_INDEX["video"], has_audio=True,
                                has_media=True, has_video=False) is None


def test_checkpoint_coherente_no_retrocede():
    assert _required_resume_idx(_PHASE_INDEX["video"], has_audio=True,
                                has_media=True, has_video=True) is None


def test_reanudar_en_tts_no_mira_dependencias_posteriores():
    # Reanudar en 'tts' ya regenera audio y todo lo posterior.
    assert _required_resume_idx(_PHASE_INDEX["tts"], has_audio=False,
                                has_media=False, has_video=False) is None


def test_audio_ausente_gana_sobre_media_ausente():
    idx = _required_resume_idx(_PHASE_INDEX["video"], has_audio=False,
                               has_media=False, has_video=False)
    assert _PHASE_ORDER[idx] == "tts"
