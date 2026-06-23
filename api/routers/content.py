"""Content browser router — full CRUD + scheduling."""
from fastapi import APIRouter, HTTPException, BackgroundTasks
from api.deps import get_db
from api.schemas.models import ContentCreate, ContentUpdate, ContentSchedule, ScriptGenerateRequest

router = APIRouter()


@router.get("")
def list_content(channel_slug: str = None, channel_id: int = None, unused_only: bool = True, 
                 status: str = None, limit: int = 50, offset: int = 0):
    db = get_db()
    import sqlite3
    with db._connect() as conn:
        q = "SELECT * FROM raw_content WHERE 1=1"
        params = []
        
        if channel_id:
            ch = db.get_channel(channel_id)
            if ch:
                q += " AND canal = ?"
                params.append(ch["slug"])
        elif channel_slug:
            q += " AND canal = ?"
            params.append(channel_slug)
        
        if unused_only:
            q += " AND used = 0"
        if status:
            q += " AND status = ?"
            params.append(status)
        
        q += " ORDER BY scraped_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(q, params).fetchall()
    
    result = [dict(r) for r in rows]
    for r in result:
        r["scraped_at"] = str(r.get("scraped_at", ""))
        r["scheduled_at"] = str(r.get("scheduled_at", "")) if r.get("scheduled_at") else None
        r["used"] = bool(r.get("used", False))
    return result


@router.post("")
def create_content(data: ContentCreate):
    db = get_db()
    import sqlite3
    with db._connect() as conn:
        # Generate unique URL for manual content
        url = data.url or f"manual://{data.canal}/{data.title.lower().replace(' ', '-')[:50]}"
        try:
            cursor = conn.execute(
                """INSERT INTO raw_content (source, subreddit, url, title, text, score, canal, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (data.source, data.subreddit, url, data.title, data.text, data.score, data.canal),
            )
            conn.commit()
            cid = cursor.lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(400, "Content with this URL already exists")
    
    row = conn.execute("SELECT * FROM raw_content WHERE id = ?", (cid,)).fetchone()
    result = dict(row)
    result["scraped_at"] = str(result.get("scraped_at", ""))
    result["used"] = bool(result.get("used", False))
    return result


@router.put("/{content_id}")
def update_content(content_id: int, data: ContentUpdate):
    db = get_db()
    import sqlite3
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM raw_content WHERE id = ?", (content_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Content not found")
    
    fields, values = [], []
    updates = data.model_dump(exclude_none=True)
    for k, v in updates.items():
        fields.append(f"{k} = ?")
        values.append(v)
    
    if fields:
        values.append(content_id)
        conn.execute(f"UPDATE raw_content SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    
    row = conn.execute("SELECT * FROM raw_content WHERE id = ?", (content_id,)).fetchone()
    result = dict(row)
    result["scraped_at"] = str(result.get("scraped_at", ""))
    result["scheduled_at"] = str(result.get("scheduled_at", "")) if result.get("scheduled_at") else None
    result["used"] = bool(result.get("used", False))
    return result


@router.delete("/{content_id}")
def delete_content(content_id: int):
    db = get_db()
    import sqlite3
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM raw_content WHERE id = ?", (content_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Content not found")
        conn.execute("DELETE FROM raw_content WHERE id = ?", (content_id,))
        conn.commit()
    return {"ok": True}


@router.post("/{content_id}/schedule")
def schedule_content(content_id: int, data: ContentSchedule):
    db = get_db()
    import sqlite3
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM raw_content WHERE id = ?", (content_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Content not found")
        conn.execute(
            "UPDATE raw_content SET status = 'scheduled', scheduled_at = ? WHERE id = ?",
            (data.scheduled_at, content_id),
        )
        conn.commit()
    
    row = conn.execute("SELECT * FROM raw_content WHERE id = ?", (content_id,)).fetchone()
    result = dict(row)
    result["scraped_at"] = str(result.get("scraped_at", ""))
    result["scheduled_at"] = str(result.get("scheduled_at", "")) if result.get("scheduled_at") else None
    return result
