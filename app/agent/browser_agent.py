import asyncio
import base64
from datetime import datetime
from typing import AsyncGenerator, List
from urllib.parse import urljoin

from playwright.async_api import async_playwright, Page, Browser

from app.models.schemas import (
    LogEntry,
    BugItem,
    WarningItem,
    PassItem,
    TestReport,
    TestSession,
)
from app.agent.ai_agent import analyze_error, generate_summary, calculate_score
from app.services.session_store import update_session


# ── Helpers ─────────────────────────

def iso_now():
    return datetime.now().isoformat()


# ── AUTH ENGINE ─────────────────────

async def handle_auth(page: Page, session: TestSession, state: dict):
    try:
        email = page.locator('input[type="email"]').first
        password = page.locator('input[type="password"]').first

        if await email.count() > 0 and not state["logged_in"]:
            await email.fill(session.username or "test@example.com")
            await password.fill(session.password or "Test@123")

            await page.locator("button").first.click()
            await page.wait_for_load_state("networkidle")

            if "login" not in page.url:
                state["logged_in"] = True
                return True
    except:
        pass

    return False


# ── MAIN RUNNER ─────────────────────

async def run_test_session(session: TestSession) -> AsyncGenerator[dict, None]:

    state = {
        "logged_in": False,
        "visited_urls": set(),
        "api_calls": [],
        "visited_actions": set(),
    }

    bugs: List[BugItem] = []
    warnings: List[WarningItem] = []
    passed: List[PassItem] = []
    full_log: List[LogEntry] = []
    console_errors: List[str] = []

    actions_taken = 0
    started_at = iso_now()
    start_time = asyncio.get_event_loop().time()

    def emit(type_, msg, url=""):
        entry = LogEntry(type=type_, message=msg, url=url)
        full_log.append(entry)
        session.log.append(entry)
        update_session(session)
        return {"event": "log", "data": entry.model_dump()}

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page: Page = await context.new_page()

        # ── Console tracking
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        # ── API tracking
        async def handle_response(response):
            try:
                if "api" in response.url:
                    body = None
                    try:
                        body = await response.text()
                    except:
                        pass

                    state["api_calls"].append({
                        "url": response.url,
                        "status": response.status,
                        "body": body[:300] if body else None,
                    })
            except:
                pass

        page.on("response", lambda r: asyncio.create_task(handle_response(r)))

        try:
            # ── LOAD
            yield emit("info", f"Opening {session.url}")
            await page.goto(session.url)
            await page.wait_for_timeout(2000)

            yield emit("pass", "Page loaded")

            # ── LOGIN
            await handle_auth(page, session, state)

            # ── MAIN LOOP
            for _ in range(session.max_actions):

                current_url = page.url

                if current_url not in state["visited_urls"]:
                    state["visited_urls"].add(current_url)
                    yield emit("info", f"Visited: {current_url}")

                await handle_auth(page, session, state)

                # ── FORM HANDLING
                inputs = await page.locator("input").all()

                for inp in inputs:
                    try:
                        key = (await inp.get_attribute("name") or "").lower()
                        action_key = f"input:{key}"

                        if action_key in state["visited_actions"]:
                            continue

                        if "email" in key:
                            await inp.fill(session.username or "test@example.com")
                        elif "password" in key:
                            await inp.fill(session.password or "Test@123")
                        else:
                            await inp.fill("test_data")

                        state["visited_actions"].add(action_key)

                    except:
                        continue

                # ── BUTTON ACTIONS
                buttons = await page.locator("button").all()

                for btn in buttons:
                    try:
                        text = (await btn.text_content() or "").lower()
                        key = f"click:{text}"

                        if key in state["visited_actions"]:
                            continue

                        if "login" in text or "submit" in text:
                            await btn.click()
                            await page.wait_for_timeout(1500)
                            yield emit("agent", f"Clicked {text}")

                            state["visited_actions"].add(key)

                    except:
                        continue

                # ── NAVIGATION (FIXED)
                links = await page.locator("a[href]").all()

                for link in links[:10]:
                    try:
                        raw_href = await link.get_attribute("href")

                        if not raw_href:
                            continue

                        raw_href = raw_href.strip()

                        if raw_href in ["#", "/", "javascript:void(0)"]:
                            continue

                        if raw_href.startswith("#"):
                            continue

                        if "mailto:" in raw_href or "tel:" in raw_href:
                            continue

                        href = urljoin(page.url, raw_href)

                        if href == page.url:
                            continue

                        # 🚫 prevent auth loops
                        if "/auth/login" in page.url and "/auth/signup" in href:
                            continue

                        if "/auth/signup" in page.url and "/auth/login" in href:
                            continue

                        if state["logged_in"] and "/auth" in href:
                            continue

                        if href in state["visited_urls"]:
                            continue

                        await link.click()
                        await page.wait_for_load_state("networkidle")

                        yield emit("agent", f"Navigated to {href}")
                        break

                    except:
                        continue

                # ── API VALIDATION
                for api in state["api_calls"]:

                    if api["status"] >= 400:
                        analysis = await analyze_error(
                            url=api["url"],
                            action_description="API call",
                            console_errors=console_errors,
                            network_errors=[],
                        )

                        bugs.append(
                            BugItem(
                                title=analysis.get("title", "API Failure"),
                                severity=analysis.get("severity", "high"),
                                page_url=api["url"],
                                action_taken="API call",
                                what_happened=analysis.get("what_happened", f"Status {api['status']}"),
                                recommendation=analysis.get("recommendation", "Fix backend API"),
                            )
                        )

                    if api.get("body") in ["{}", "[]", None]:
                        warnings.append(
                            WarningItem(
                                title="Empty API response",
                                page_url=api["url"],
                                description="API returned empty or missing data",
                                recommendation="Check backend data integrity",
                            )
                        )

                actions_taken += 1
                session.actions_taken = actions_taken
                update_session(session)

            # ── FINAL REPORT
            summary = await generate_summary(
                url=session.url,
                actions_taken=actions_taken,
                bugs=[b.model_dump() for b in bugs],
                warnings=[w.model_dump() for w in warnings],
                passed=[p.model_dump() for p in passed],
            )

            score = await calculate_score(
                bugs=[b.model_dump() for b in bugs],
                warnings=[w.model_dump() for w in warnings],
                passed=[p.model_dump() for p in passed],
            )

            report = TestReport(
                session_id=session.session_id,
                url=session.url,
                started_at=started_at,
                completed_at=iso_now(),
                duration_seconds=round(asyncio.get_event_loop().time() - start_time, 1),
                overall_score=score,
                actions_taken=actions_taken,
                bugs=bugs,
                warnings=warnings,
                passed=passed,
                full_log=full_log,
                summary=summary,
                status="completed",
            )

            yield emit("info", f"Done: {len(bugs)} bugs")
            yield {"event": "complete", "data": report.model_dump()}

        except Exception as e:
            yield emit("error", f"Crash: {str(e)}")

        finally:
            await browser.close()