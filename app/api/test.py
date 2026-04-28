"""
Test API routes.

POST /api/test/start     → Start a new test session, returns session_id
GET  /api/test/stream/{session_id}  → SSE stream of live agent logs
GET  /api/test/status/{session_id}  → Current session status
"""
import uuid
import asyncio
import json
from datetime import datetime
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.models.schemas import StartTestRequest, TestSession
from app.services.session_store import create_session, get_session, list_sessions
from app.agent.browser_agent import run_test_session

router = APIRouter()

# Track background tasks per session
_running_tasks: dict = {}


def validate_url(url: str) -> str:
    """Ensure URL has a scheme."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


@router.post("/test/start")
async def start_test(request: StartTestRequest):
    """
    Start a new autonomous test session.
    Returns session_id immediately — client streams logs via /test/stream/{session_id}
    """
    url = validate_url(request.url)
    session_id = str(uuid.uuid4())

    session = TestSession(
        session_id=session_id,
        url=url,
        status="queued",
        started_at=datetime.now().isoformat(),
        max_actions=min(request.max_actions or 50, 100),
    )
    create_session(session)

    return {
        "session_id": session_id,
        "url": url,
        "status": "queued",
        "message": "Test session created. Connect to /api/test/stream/{session_id} to receive live updates.",
    }


@router.get("/test/stream/{session_id}")
async def stream_test(session_id: str):
    """
    SSE endpoint. Client connects here to receive real-time agent logs.
    Streams until the test completes or fails.
    """
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def event_generator():
        # Send initial connected event
        yield f"data: {json.dumps({'event': 'connected', 'data': {'session_id': session_id, 'url': session.url}})}\n\n"

        # Update session status to running
        session.status = "running"

        try:
            async for event in run_test_session(session):
                payload = json.dumps(event)
                yield f"data: {payload}\n\n"
                await asyncio.sleep(0)  # yield control to event loop

        except asyncio.CancelledError:
            yield f"data: {json.dumps({'event': 'error', 'data': {'message': 'Stream cancelled'}})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'event': 'error', 'data': {'message': str(e)}})}\n\n"
        finally:
            yield f"data: {json.dumps({'event': 'stream_end', 'data': {}})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Important for nginx
        },
    )


@router.get("/test/status/{session_id}")
async def get_test_status(session_id: str):
    """Get current status of a test session."""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session.session_id,
        "url": session.url,
        "status": session.status,
        "actions_taken": session.actions_taken,
        "log_count": len(session.log),
        "has_report": session.report is not None,
    }


@router.get("/test/sessions")
async def list_test_sessions():
    """List all test sessions (for debugging)."""
    return {"sessions": list_sessions()}