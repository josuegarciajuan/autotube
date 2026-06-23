"""Progress manager for WebSocket-based real-time generation progress.

Shared between api/main.py (WebSocket endpoint) and 
api/routers/videos.py (triggering generation jobs via services).
"""
from fastapi import WebSocket


class ProgressManager:
    """Manages WebSocket connections for real-time generation progress."""
    
    def __init__(self):
        self._subscribers: dict[int, set] = {}
    
    def subscribe(self, job_id: int, ws: WebSocket):
        if job_id not in self._subscribers:
            self._subscribers[job_id] = set()
        self._subscribers[job_id].add(ws)
    
    def unsubscribe(self, job_id: int, ws: WebSocket):
        if job_id in self._subscribers:
            self._subscribers[job_id].discard(ws)
            if not self._subscribers[job_id]:
                del self._subscribers[job_id]
    
    async def broadcast(self, job_id: int, data: dict):
        if job_id in self._subscribers:
            dead = set()
            for ws in list(self._subscribers[job_id]):
                try:
                    await ws.send_json(data)
                except Exception:
                    dead.add(ws)
            for ws in dead:
                self.unsubscribe(job_id, ws)


# Singleton instance
progress_manager = ProgressManager()


def get_progress_manager() -> ProgressManager:
    return progress_manager
