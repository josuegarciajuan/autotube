"""Content scheduling router — recurring and one-time schedule management."""
from fastapi import APIRouter, HTTPException
from api.deps import get_db
from pydantic import BaseModel
from typing import Optional

router = APIRouter()


class ScheduleCreate(BaseModel):
    channel_id: int
    action: str = "generate_and_upload"  # always generate+upload
    schedule_type: str = "recurring"     # recurring | one_time
    interval_h: int = 24
    content_id: Optional[int] = None
    next_run_at: Optional[str] = None


class ScheduleUpdate(BaseModel):
    action: Optional[str] = None
    schedule_type: Optional[str] = None
    interval_h: Optional[int] = None
    content_id: Optional[int] = None
    next_run_at: Optional[str] = None
    active: Optional[bool] = None


@router.get("")
def list_schedules(channel_id: int = None, active_only: bool = False):
    db = get_db()
    import sqlite3
    with db._connect() as conn:
        q = "SELECT cs.*, c.name as channel_name, c.slug as channel_slug FROM content_schedules cs JOIN channels c ON cs.channel_id = c.id WHERE 1=1"
        params = []
        if channel_id:
            q += " AND cs.channel_id = ?"
            params.append(channel_id)
        if active_only:
            q += " AND cs.active = 1"
        q += " ORDER BY cs.next_run_at ASC"
        rows = conn.execute(q, params).fetchall()
    
    result = [dict(r) for r in rows]
    for r in result:
        for k in ("next_run_at", "last_run_at", "created_at"):
            if r.get(k):
                r[k] = str(r[k])
        r["active"] = bool(r.get("active", False))
    return result


@router.post("")
def create_schedule(data: ScheduleCreate):
    db = get_db()
    ch = db.get_channel(data.channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    
    # Normalize next_run_at: replace 'T' separator → space for consistent string comparison
    normalized_next = data.next_run_at
    if normalized_next and 'T' in normalized_next:
        normalized_next = normalized_next.replace('T', ' ')
    
    import sqlite3
    with db._connect() as conn:
        cursor = conn.execute(
            """INSERT INTO content_schedules (channel_id, action, schedule_type, interval_h, content_id, next_run_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (data.channel_id, data.action, data.schedule_type, 
             data.interval_h, None, normalized_next),  # content_id always NULL
        )
        conn.commit()
        sid = cursor.lastrowid
    
    row = db.get_db_row("content_schedules", sid) if hasattr(db, 'get_db_row') else None
    if not row:
        with db._connect() as conn:
            row = conn.execute("SELECT * FROM content_schedules WHERE id = ?", (sid,)).fetchone()
        row = dict(row) if row else None
    
    if row:
        for k in ("next_run_at", "last_run_at", "created_at"):
            if row.get(k): row[k] = str(row[k])
        row["active"] = bool(row.get("active", False))
    return row


@router.put("/{schedule_id}")
def update_schedule(schedule_id: int, data: ScheduleUpdate):
    db = get_db()
    import sqlite3
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM content_schedules WHERE id = ?", (schedule_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Schedule not found")
    
    fields, values = [], []
    updates = data.model_dump(exclude_none=True)
    # Normalize next_run_at: replace 'T' → space for consistent comparison
    if 'next_run_at' in updates and updates['next_run_at'] and 'T' in updates['next_run_at']:
        updates['next_run_at'] = updates['next_run_at'].replace('T', ' ')
    for k, v in updates.items():
        fields.append(f"{k} = ?")
        values.append(v)
    if not fields:
        raise HTTPException(400, "No fields to update")
    
    values.append(schedule_id)
    with db._connect() as conn:
        conn.execute(f"UPDATE content_schedules SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM content_schedules WHERE id = ?", (schedule_id,)).fetchone()
    row = dict(row)
    for k in ("next_run_at", "last_run_at", "created_at"):
        if row.get(k): row[k] = str(row[k])
    row["active"] = bool(row.get("active", False))
    return row


@router.delete("/{schedule_id}")
def delete_schedule(schedule_id: int):
    db = get_db()
    import sqlite3
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM content_schedules WHERE id = ?", (schedule_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Schedule not found")
        conn.execute("DELETE FROM content_schedules WHERE id = ?", (schedule_id,))
        conn.commit()
    return {"ok": True}


@router.put("/{schedule_id}/toggle")
def toggle_schedule(schedule_id: int):
    db = get_db()
    import sqlite3
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM content_schedules WHERE id = ?", (schedule_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Schedule not found")
        new_active = 0 if row["active"] else 1
        conn.execute("UPDATE content_schedules SET active = ? WHERE id = ?", (new_active, schedule_id))
        conn.commit()
    return {"active": bool(new_active)}
