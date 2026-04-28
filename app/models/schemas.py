from pydantic import BaseModel, HttpUrl
from typing import Optional, List, Literal
from datetime import datetime
import uuid


# ── Request models ──────────────────────────────────────────────

class StartTestRequest(BaseModel):
    url: str
    max_actions: Optional[int] = 50
    
    test_credentials: Optional[dict] = None  # {"username": "", "password": ""}
    test_scope: Optional[List[str]] = [
        "navigation", "forms", "buttons",
        "console_errors", "broken_images", "accessibility"
    ]


# ── Activity log entry ───────────────────────────────────────────

class LogEntry(BaseModel):
    id: str = ""
    timestamp: str = ""
    type: Literal["info", "pass", "error", "warning", "agent"]
    message: str
    url: Optional[str] = None
    screenshot: Optional[str] = None  # base64 encoded

    def __init__(self, **data):
        if not data.get("id"):
            data["id"] = str(uuid.uuid4())
        if not data.get("timestamp"):
            data["timestamp"] = datetime.now().strftime("%H:%M:%S")
        super().__init__(**data)


# ── Bug / Warning / Pass items ───────────────────────────────────

class BugItem(BaseModel):
    id: str = ""
    title: str
    severity: Literal["critical", "high", "medium", "low"]
    page_url: str
    action_taken: str
    what_happened: str
    screenshot: Optional[str] = None  # base64
    recommendation: str

    def __init__(self, **data):
        if not data.get("id"):
            data["id"] = str(uuid.uuid4())
        super().__init__(**data)


class WarningItem(BaseModel):
    id: str = ""
    title: str
    page_url: str
    description: str
    recommendation: str
    screenshot: Optional[str] = None

    def __init__(self, **data):
        if not data.get("id"):
            data["id"] = str(uuid.uuid4())
        super().__init__(**data)


class PassItem(BaseModel):
    id: str = ""
    title: str
    page_url: str

    def __init__(self, **data):
        if not data.get("id"):
            data["id"] = str(uuid.uuid4())
        super().__init__(**data)


# ── Final report ─────────────────────────────────────────────────

class TestReport(BaseModel):
    session_id: str
    url: str
    started_at: str
    completed_at: str
    duration_seconds: float
    overall_score: int
    actions_taken: int
    bugs: List[BugItem] = []
    warnings: List[WarningItem] = []
    passed: List[PassItem] = []
    full_log: List[LogEntry] = []
    summary: str = ""
    status: Literal["completed", "failed", "running"] = "completed"


# ── Session state (in-memory store) ─────────────────────────────

class TestSession(BaseModel):
    session_id: str
    url: str
    status: Literal["queued", "running", "completed", "failed"] = "queued"

    username: Optional[str] = None   # ✅ ADD
    password: Optional[str] = None   # ✅ ADD

    started_at: str = ""
    log: List[LogEntry] = []
    report: Optional[TestReport] = None
    actions_taken: int = 0
    max_actions: int = 50

# ── SSE event ────────────────────────────────────────────────────

class SSEEvent(BaseModel):
    event: str  # "log", "complete", "error"
    data: dict