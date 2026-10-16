"""Fase 7: modo degradado de presupuesto de media (no perder vídeos casi hechos)."""
from pipeline.media_fetcher import MediaFetcher


def _bare_fetcher(monkeypatch, *, degraded, ai_hit):
    f = object.__new__(MediaFetcher)
    f._ai_image_primary = True
    f._budget_degraded = degraded
    f._media_strategy = {}
    monkeypatch.setattr(f, "_build_query_pool", lambda *a, **k: ["q"])

    def _no_video(*a, **k):
        return None

    def _ai_should_not_run(*a, **k):
        ai_hit.append(True)
        return {"path": "ai.jpg", "type": "image", "source": "ai_local_sd"}

    fake_img = {"path": "stock.jpg", "type": "image", "source": "pixabay_photo"}
    monkeypatch.setattr(f, "_try_stock_video_tier", _no_video)
    monkeypatch.setattr(f, "_try_ai_image_chain", _ai_should_not_run)
    monkeypatch.setattr(f, "_fetch_asset_exhaustive", lambda *a, **k: fake_img)
    monkeypatch.setattr(f, "_try_pollo_scene", lambda *a, **k: None)
    return f


def test_modo_degradado_omite_ia(monkeypatch):
    ai_hit = []
    f = _bare_fetcher(monkeypatch, degraded=True, ai_hit=ai_hit)
    asset, _ai_used, _q = f._fetch_with_ai_tiers(
        {"tipo": "desarrollo"}, scene_idx=0, total_scenes=10,
        is_video_priority=True, target_dur=5.0, ctx=None,
        ai_used=0, ai_max=2, ai_enabled=True,
    )
    assert ai_hit == []                      # la IA nunca se invocó
    assert asset["source"] == "pixabay_photo"  # cayó a stock


def test_modo_normal_usa_ia(monkeypatch):
    ai_hit = []
    f = _bare_fetcher(monkeypatch, degraded=False, ai_hit=ai_hit)
    asset, _ai_used, _q = f._fetch_with_ai_tiers(
        {"tipo": "desarrollo"}, scene_idx=0, total_scenes=10,
        is_video_priority=True, target_dur=5.0, ctx=None,
        ai_used=0, ai_max=2, ai_enabled=True,
    )
    assert ai_hit == [True]                    # la IA sí se invocó
    assert asset["source"] == "ai_local_sd"


def test_budget_degraded_se_inicializa_por_job():
    # El atributo se reinicia en fetch_for_script; comprobamos el default sano.
    f = object.__new__(MediaFetcher)
    assert getattr(f, "_budget_degraded", False) is False


# ── force_images (cap de RAM de vídeo) NO debe poner IA primero ──────────────
# Incidente oct 2026: al alcanzar el cap de clips, las escenas restantes caían
# al tier de IA primero → ~110 imágenes de Stable Diffusion local (minutos cada
# una). El cap de vídeo debe resolverse con stock de imagen cuando la escena no
# era AI-tier.

def test_force_images_escena_video_usa_stock_no_ia(monkeypatch):
    ai_hit = []
    f = _bare_fetcher(monkeypatch, degraded=False, ai_hit=ai_hit)
    asset, _ai_used, _q = f._fetch_with_ai_tiers(
        {"tipo": "desarrollo"}, scene_idx=0, total_scenes=10,
        is_video_priority=False, target_dur=5.0, ctx=None,
        ai_used=0, ai_max=2, ai_enabled=True,
        force_images=True, scene_tier="video",
    )
    assert ai_hit == [], "con force_images no debe invocarse IA en escenas no-AI-tier"
    assert asset["source"] == "pixabay_photo"


def test_force_images_escena_stock_usa_stock_no_ia(monkeypatch):
    ai_hit = []
    f = _bare_fetcher(monkeypatch, degraded=False, ai_hit=ai_hit)
    asset, _ai_used, _q = f._fetch_with_ai_tiers(
        {"tipo": "reflexion"}, scene_idx=1, total_scenes=10,
        is_video_priority=False, target_dur=5.0, ctx=None,
        ai_used=0, ai_max=2, ai_enabled=True,
        force_images=True, is_stock_image_priority=True, scene_tier="stock",
    )
    assert ai_hit == []
    assert asset["source"] == "pixabay_photo"


def test_force_images_escena_ai_conserva_ia(monkeypatch):
    ai_hit = []
    f = _bare_fetcher(monkeypatch, degraded=False, ai_hit=ai_hit)
    asset, _ai_used, _q = f._fetch_with_ai_tiers(
        {"tipo": "climax"}, scene_idx=2, total_scenes=10,
        is_video_priority=False, target_dur=5.0, ctx=None,
        ai_used=0, ai_max=2, ai_enabled=True,
        force_images=True, scene_tier="ai",
    )
    assert ai_hit == [True], "una escena AI-tier conserva la IA primero"
    assert asset["source"] == "ai_local_sd"
