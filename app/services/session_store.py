"""
Simple in-memory session store.
Replace with Supabase persistence when ready.
"""
from typing import Dict, Optional
from app.models.schemas import TestSession

# Global session registry
_sessions: Dict[str, TestSession] = {}


def create_session(session: TestSession) -> TestSession:
    _sessions[session.session_id] = session
    return session


def get_session(session_id: str) -> Optional[TestSession]:
    return _sessions.get(session_id)


def update_session(session: TestSession) -> TestSession:
    _sessions[session.session_id] = session
    return session


def delete_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


def list_sessions() -> list:
    return [
        {"session_id": s.session_id, "url": s.url, "status": s.status, "started_at": s.started_at}
        for s in _sessions.values()
    ]