"""
Provider-agnostic testing agent brain.
Uses Gemini by default, with optional Anthropic fallback support.
"""
import asyncio
import base64
import json
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import settings


INITIAL_ANALYSIS_PROMPT = """
You are a senior QA engineer performing REAL user simulation testing.

Your goal is NOT to randomly click buttons.
Your goal is to simulate a real user journey.

STRICT PRIORITY ORDER:

1. Check if login/signup is present
   - If yes → plan login/signup flow FIRST

2. After login:
   - Navigate through main pages (dashboard, profile, settings, etc.)
   - Follow real navigation links (header, sidebar)

3. On each page:
   - Trigger API actions (form submit, fetch data)
   - Validate responses (errors, loading, success states)

4. Explore app like a user:
   - Do NOT click random buttons
   - ONLY meaningful actions

5. Maintain logical flow:
   - login → dashboard → features → logout

Return structured JSON plan.

CRITICAL:
- Use REAL selectors (id, name, text)
- Prefer click over navigate
- Avoid fake URLs like "About page"
"""
NEXT_ACTION_PROMPT = """
You are an expert QA engineer performing autonomous UI testing.

Current state:
- URL: {url}
- Actions taken so far: {actions_taken}/{max_actions}
- Previous actions: {previous_actions}

You are given the current screenshot and DOM elements.

Decide the SINGLE best next action to test something new that hasn't been tested yet.

Return ONLY valid JSON:
{{
  "action": "click" | "type" | "navigate" | "scroll" | "submit" | "done",
  "target_selector": "CSS selector or URL or null",
  "value": "value if needed or null",
  "description": "what you are testing",
  "reasoning": "why this is worth testing next"
}}

If you have tested everything meaningful or hit diminishing returns, return {{"action": "done", "target_selector": null, "value": null, "description": "Testing complete", "reasoning": "All key flows tested"}}.
"""

ERROR_ANALYSIS_PROMPT = """
You are a QA engineer analyzing a UI bug found during automated testing.

Context:
- Page URL: {url}
- Action performed: {action_description}
- Console errors detected: {console_errors}
- Network errors: {network_errors}
- Visual anomaly detected: {visual_anomaly}

Analyze this and return ONLY valid JSON:
{{
  "is_bug": true | false,
  "severity": "critical" | "high" | "medium" | "low",
  "title": "Short bug title",
  "what_happened": "Detailed description of what went wrong",
  "recommendation": "How to fix this"
}}
"""

FINAL_REPORT_PROMPT = """
You are a senior QA engineer. Summarize the results of an automated website test.

URL tested: {url}
Actions taken: {actions_taken}
Bugs found: {bug_count}
Warnings: {warning_count}
Passed checks: {pass_count}

Bug titles: {bug_titles}
Warning titles: {warning_titles}

Write a 3-4 sentence executive summary of the test results. Be specific and professional.
Mention the most critical issues if any. If the site is mostly fine, say so.
Return ONLY the summary text, no JSON.
"""

SCORE_PROMPT = """
Calculate an overall quality score (0-100) for a website based on test results.

Bugs: {bug_count} (critical: {critical_count}, high: {high_count}, medium: {medium_count}, low: {low_count})
Warnings: {warning_count}
Passed checks: {pass_count}
Total checks: {total_checks}

Scoring guide:
- Start at 100
- Critical bug: -20 each
- High bug: -12 each
- Medium bug: -6 each
- Low bug: -3 each
- Warning: -2 each
- Minimum score: 0

Return ONLY a JSON object: {{"score": <number>}}
"""

TEST_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "page_summary": {"type": "string"},
        "test_plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "target_selector": {"type": ["string", "null"]},
                    "value": {"type": ["string", "null"]},
                    "description": {"type": "string"},
                    "priority": {"type": "string"},
                },
                "required": ["action", "target_selector", "value", "description", "priority"],
            },
        },
    },
    "required": ["page_summary", "test_plan"],
}

NEXT_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "target_selector": {"type": ["string", "null"]},
        "value": {"type": ["string", "null"]},
        "description": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "target_selector", "value", "description", "reasoning"],
}

ERROR_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "is_bug": {"type": "boolean"},
        "severity": {"type": "string"},
        "title": {"type": "string"},
        "what_happened": {"type": "string"},
        "recommendation": {"type": "string"},
    },
    "required": ["is_bug", "severity", "title", "what_happened", "recommendation"],
}

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
    },
    "required": ["score"],
}


def encode_screenshot(screenshot_bytes: bytes) -> str:
    return base64.standard_b64encode(screenshot_bytes).decode("utf-8")


def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a JSON-Schema-like dict into the narrower schema shape Gemini expects.

    Gemini's responseSchema does not accept JSON Schema unions like:
    {"type": ["string", "null"]}
    so we rewrite those to:
    {"type": "string", "nullable": true}
    """
    converted: dict[str, Any] = {}

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null_types = [item for item in schema_type if item != "null"]
        if len(non_null_types) == 1:
            converted["type"] = non_null_types[0]
            converted["nullable"] = "null" in schema_type
        elif non_null_types:
            converted["type"] = non_null_types[0]
        elif schema_type:
            converted["type"] = schema_type[0]
    elif schema_type is not None:
        converted["type"] = schema_type

    if "description" in schema:
        converted["description"] = schema["description"]
    if "enum" in schema:
        converted["enum"] = schema["enum"]
    if "format" in schema:
        converted["format"] = schema["format"]

    if "properties" in schema:
        converted["properties"] = {
            key: _to_gemini_schema(value)
            for key, value in schema["properties"].items()
        }
    if "items" in schema:
        converted["items"] = _to_gemini_schema(schema["items"])
    if "required" in schema:
        converted["required"] = schema["required"]

    return converted


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _provider_name() -> str:
    return (settings.llm_provider or "gemini").strip().lower()


def _raise_if_missing_key(provider: str) -> None:
    if provider == "gemini" and not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    if provider == "anthropic" and not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    if provider == "groq" and not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")


def _parse_gemini_text(response_data: dict) -> str:
    candidates = response_data.get("candidates") or []
    for candidate in candidates:
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        chunks = [part.get("text", "") for part in parts if part.get("text")]
        if chunks:
            return "".join(chunks).strip()

    prompt_feedback = response_data.get("promptFeedback") or {}
    block_reason = prompt_feedback.get("blockReason")
    if block_reason:
        raise RuntimeError(f"Gemini blocked the request: {block_reason}")

    raise RuntimeError("Gemini returned no text content")


def _gemini_request(
    prompt_text: str,
    *,
    screenshot_bytes: Optional[bytes] = None,
    response_schema: Optional[dict] = None,
    max_output_tokens: int = 1000,
) -> str:
    _raise_if_missing_key("gemini")

    parts: list[dict[str, Any]] = [{"text": prompt_text}]
    if screenshot_bytes:
        parts.append(
            {
                "inlineData": {
                    "mimeType": "image/png",
                    "data": encode_screenshot(screenshot_bytes),
                }
            }
        )

    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": max_output_tokens,
        },
    }
    if response_schema:
        payload["generationConfig"]["responseMimeType"] = "application/json"
        payload["generationConfig"]["responseSchema"] = _to_gemini_schema(
            response_schema)

    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": settings.gemini_api_key,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=60) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Gemini API error ({exc.code}): {details}") from exc
    except URLError as exc:
        raise RuntimeError(f"Gemini connection error: {exc.reason}") from exc

    return _parse_gemini_text(response_data)


def _anthropic_request(
    prompt_text: str,
    *,
    screenshot_bytes: Optional[bytes] = None,
    max_output_tokens: int = 1000,
) -> str:
    _raise_if_missing_key("anthropic")

    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("Anthropic SDK is not installed") from exc

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    content: list[dict[str, Any]] = []
    if screenshot_bytes:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": encode_screenshot(screenshot_bytes),
                },
            }
        )
    content.append({"type": "text", "text": prompt_text})

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=max_output_tokens,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text.strip()


def _groq_request(
    prompt_text: str,
    *,
    screenshot_bytes: Optional[bytes] = None,
    max_output_tokens: int = 1000,
) -> str:
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")

    try:
        from groq import Groq
    except ImportError as exc:
        raise RuntimeError(
            "Groq SDK not installed. Run: pip install groq") from exc

    client = Groq(api_key=settings.groq_api_key)

    response = client.chat.completions.create(
        model=settings.groq_model,
        messages=[
            {"role": "system", "content": """
You are NOT allowed to randomly click buttons.

You must:
- follow user journey
- prioritize login
- navigate logically
- avoid meaningless actions
"""},
            {"role": "user", "content": prompt_text},
        ],
        temperature=0.2,
        max_tokens=max_output_tokens,
    )

    return response.choices[0].message.content.strip()


# async def _generate_text(
#     prompt_text: str,
#     *,
#     screenshot_bytes: Optional[bytes] = None,
#     response_schema: Optional[dict] = None,
#     max_output_tokens: int = 1000,
# ) -> str:
#     provider = _provider_name()
#     if provider == "anthropic":
#         return await asyncio.to_thread(
#             _anthropic_request,
#             prompt_text,
#             screenshot_bytes=screenshot_bytes,
#             max_output_tokens=max_output_tokens,
#         )
#     if provider == "gemini":
#         return await asyncio.to_thread(
#             _gemini_request,
#             prompt_text,
#             screenshot_bytes=screenshot_bytes,
#             response_schema=response_schema,
#             max_output_tokens=max_output_tokens,
#         )
#     raise RuntimeError(f"Unsupported LLM provider: {settings.llm_provider}")


async def _generate_text(
    prompt_text: str,
    *,
    screenshot_bytes: Optional[bytes] = None,
    response_schema: Optional[dict] = None,
    max_output_tokens: int = 1000,
) -> str:
    provider = _provider_name()

    if provider == "groq":
        return await asyncio.to_thread(
            _groq_request,
            prompt_text,
            screenshot_bytes=screenshot_bytes,
            max_output_tokens=max_output_tokens,
        )

    if provider == "anthropic":
        return await asyncio.to_thread(
            _anthropic_request,
            prompt_text,
            screenshot_bytes=screenshot_bytes,
            max_output_tokens=max_output_tokens,
        )

    if provider == "gemini":
        return await asyncio.to_thread(
            _gemini_request,
            prompt_text,
            screenshot_bytes=screenshot_bytes,
            response_schema=response_schema,
            max_output_tokens=max_output_tokens,
        )

    raise RuntimeError(f"Unsupported LLM provider: {settings.llm_provider}")






async def analyze_error(
    url: str,
    action_description: str,
    console_errors: list,
    network_errors: list,
    visual_anomaly: bool = False,
) -> dict:
    """
    Ask the configured LLM to analyze whether something is a real bug and how severe.
    """
    prompt = ERROR_ANALYSIS_PROMPT.format(
        url=url,
        action_description=action_description,
        console_errors=json.dumps(console_errors),
        network_errors=json.dumps(network_errors),
        visual_anomaly=visual_anomaly,
    )

    try:
        raw = await _generate_text(
            prompt,
            response_schema=ERROR_ANALYSIS_SCHEMA,
            max_output_tokens=500,
        )
        return json.loads(_strip_json_fences(raw))
    except Exception:
        return {"is_bug": False}


async def generate_summary(
    url: str,
    actions_taken: int,
    bugs: list,
    warnings: list,
    passed: list,
) -> str:
    """
    Generate a human-readable executive summary of the test.
    """
    prompt = FINAL_REPORT_PROMPT.format(
        url=url,
        actions_taken=actions_taken,
        bug_count=len(bugs),
        warning_count=len(warnings),
        pass_count=len(passed),
        bug_titles=[b.get("title", "") for b in bugs],
        warning_titles=[w.get("title", "") for w in warnings],
    )

    try:
        raw = await _generate_text(prompt, max_output_tokens=300)
        return raw.strip()
    except Exception:
        return f"Automated test completed on {url}. Found {len(bugs)} bugs and {len(warnings)} warnings."


async def calculate_score(bugs: list, warnings: list, passed: list) -> int:
    """
    Calculate overall quality score.
    """
    critical = sum(1 for b in bugs if b.get("severity") == "critical")
    high = sum(1 for b in bugs if b.get("severity") == "high")
    medium = sum(1 for b in bugs if b.get("severity") == "medium")
    low = sum(1 for b in bugs if b.get("severity") == "low")

    prompt = SCORE_PROMPT.format(
        bug_count=len(bugs),
        critical_count=critical,
        high_count=high,
        medium_count=medium,
        low_count=low,
        warning_count=len(warnings),
        pass_count=len(passed),
        total_checks=len(bugs) + len(warnings) + len(passed),
    )

    try:
        raw = await _generate_text(
            prompt,
            response_schema=SCORE_SCHEMA,
            max_output_tokens=50,
        )
        result = json.loads(_strip_json_fences(raw))
        return max(0, min(100, int(result.get("score", 70))))
    except Exception:
        score = 100
        score -= critical * 20
        score -= high * 12
        score -= medium * 6
        score -= low * 3
        score -= len(warnings) * 2
        return max(0, score)
