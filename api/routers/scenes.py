"""Video scenes router."""
from fastapi import APIRouter, HTTPException, BackgroundTasks
from api.deps import get_db
from api.schemas.models import SceneUpdate

router = APIRouter()


@router.get("/video/{video_id}")
def list_scenes(video_id: int):
    db = get_db()
    scenes = db.get_scenes(video_id)
    for s in scenes:
        for k in ("created_at", "updated_at"):
            if s.get(k):
                s[k] = str(s[k])
    return scenes


@router.put("/{scene_id}")
def update_scene(scene_id: int, data: SceneUpdate):
    db = get_db()
    kwargs = {k: v for k, v in data.model_dump().items() if v is not None}
    if not kwargs:
        raise HTTPException(400, "No fields to update")
    db.update_scene(scene_id, **kwargs)
    return {"ok": True}


@router.post("/{scene_id}/regenerate-audio")
def regenerate_scene_audio(scene_id: int, background_tasks: BackgroundTasks):
    """Regenerate TTS audio for a specific scene."""
    db = get_db()
    # Get scene info
    import sqlite3
    with db._connect() as conn:
        row = conn.execute(
            "SELECT vs.*, v.canal FROM video_scenes vs JOIN videos v ON vs.video_id = v.id WHERE vs.id = ?",
            (scene_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Scene not found")
    
    scene = dict(row)
    if not scene.get("script_text"):
        raise HTTPException(400, "Scene has no script text")
    
    from api.services.generation_service import regenerate_scene_audio_task
    background_tasks.add_task(regenerate_scene_audio_task, scene_id, scene["canal"])
    
    return {"message": "Audio regeneration started"}


@router.post("/{scene_id}/replace-image")
def replace_scene_image(scene_id: int, background_tasks: BackgroundTasks):
    """Search and replace image for a scene."""
    db = get_db()
    import sqlite3
    with db._connect() as conn:
        row = conn.execute(
            "SELECT vs.*, v.canal FROM video_scenes vs JOIN videos v ON vs.video_id = v.id WHERE vs.id = ?",
            (scene_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Scene not found")
    
    scene = dict(row)
    if not scene.get("description"):
        raise HTTPException(400, "Scene has no description for image search")
    
    from api.services.generation_service import replace_scene_image_task
    background_tasks.add_task(replace_scene_image_task, scene_id, scene["description"], scene.get("canal"))
    
    return {"message": "Image replacement started"}
