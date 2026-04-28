"""
Report API routes.

GET /api/report/{session_id}  → Fetch completed test report
"""
from fastapi import APIRouter, HTTPException
from app.services.session_store import get_session

router = APIRouter()


@router.get("/report/{session_id}")
async def get_report(session_id: str):
    """
    Fetch the completed test report for a session.
    Returns 404 if session not found, 202 if still running.
    """
    session = get_session(session_id)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status == "running" or session.status == "queued":
        return {
            "status": session.status,
            "message": "Test is still running. Poll /api/test/status or wait for SSE stream to complete.",
            "actions_taken": session.actions_taken,
        }

    if session.status == "failed":
        raise HTTPException(status_code=500, detail="Test session failed")

    if not session.report:
        raise HTTPException(status_code=404, detail="Report not yet available")

    return session.report.model_dump()


@router.get("/report/{session_id}/summary")
async def get_report_summary(session_id: str):
    """Light summary of the report without full log and screenshots."""
    session = get_session(session_id)

    if not session or not session.report:
        raise HTTPException(status_code=404, detail="Report not found")

    report = session.report
    return {
        "session_id": report.session_id,
        "url": report.url,
        "overall_score": report.overall_score,
        "duration_seconds": report.duration_seconds,
        "actions_taken": report.actions_taken,
        "bug_count": len(report.bugs),
        "warning_count": len(report.warnings),
        "pass_count": len(report.passed),
        "summary": report.summary,
        "status": report.status,
        "completed_at": report.completed_at,
    }