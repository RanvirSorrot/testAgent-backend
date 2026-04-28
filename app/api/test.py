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


def validate_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


@router.post("/test/start")
async def start_test(request: StartTestRequest):
    """
    Start a new autonomous test session
    """

    url = validate_url(request.url)
    session_id = str(uuid.uuid4())

    # ✅ SAFE credentials extraction
    creds = request.test_credentials or {}

    session = TestSession(
        session_id=session_id,  # ✅ FIXED
        url=url,                # ✅ FIXED
        username=creds.get("username"),
        password=creds.get("password"),
        max_actions=request.max_actions or 50,
        started_at=datetime.now().isoformat(),
        status="queued",
    )

    create_session(session)

    return {
        "session_id": session_id,
        "url": url,
        "status": "queued",
        "message": "Connect to /api/test/stream/{session_id} for live logs",
    }


@router.get("/test/stream/{session_id}")
async def stream_test(session_id: str):
    """
    SSE stream for live agent execution
    """

    session = get_session(session_id)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def event_generator():

        # initial event
        yield f"data: {json.dumps({'event': 'connected', 'data': {'session_id': session_id, 'url': session.url}})}\n\n"

        session.status = "running"

        try:
            async for event in run_test_session(session):
                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0)

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
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/test/status/{session_id}")
async def get_test_status(session_id: str):
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
    return {"sessions": list_sessions()}