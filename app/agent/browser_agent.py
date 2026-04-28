"""
Playwright-based browser automation engine.
Visits the URL, extracts DOM, executes actions, captures screenshots and errors.
"""
import asyncio
import base64
import json
from datetime import datetime
from typing import AsyncGenerator, List, Optional
from playwright.async_api import async_playwright, Page, Browser, ConsoleMessage

from app.models.schemas import LogEntry, BugItem, WarningItem, PassItem, TestReport, TestSession
from app.agent.ai_agent import (
    get_initial_test_plan,
    get_next_action,
    analyze_error,
    generate_summary,
    calculate_score,
)
from app.services.session_store import update_session


# ── Helpers ───────────────────────────────────────────────────────

def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def iso_now() -> str:
    return datetime.now().isoformat()


def b64_screenshot(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("utf-8")


async def extract_dom_elements(page: Page) -> list:
    """Extract all interactive elements from the page."""
    try:
        elements = await page.evaluate("""
        () => {
            const results = [];
            const selectors = [
                'button', 'a[href]', 'input', 'textarea', 'select',
                '[role="button"]', '[onclick]', 'form', '[tabindex]'
            ];

            selectors.forEach(selector => {
                document.querySelectorAll(selector).forEach((el, idx) => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) return;

                    results.push({
                        tag: el.tagName.toLowerCase(),
                        selector: selector,
                        text: (el.textContent || '').trim().slice(0, 80),
                        type: el.getAttribute('type') || '',
                        href: el.getAttribute('href') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        id: el.id || '',
                        name: el.getAttribute('name') || '',
                        visible: rect.width > 0 && rect.height > 0,
                        index: idx,
                    });
                });
            });

            return results.slice(0, 60);
        }
        """)
        return elements or []
    except Exception:
        return []


async def check_accessibility(page: Page) -> list:
    """Basic accessibility checks."""
    warnings = []
    try:
        issues = await page.evaluate("""
        () => {
            const issues = [];

            // Images without alt text
            document.querySelectorAll('img').forEach(img => {
                if (!img.alt && !img.getAttribute('aria-label')) {
                    issues.push({type: 'missing_alt', element: img.src?.slice(0, 60) || 'img'});
                }
            });

            // Buttons without accessible text
            document.querySelectorAll('button').forEach(btn => {
                const text = btn.textContent?.trim();
                const label = btn.getAttribute('aria-label');
                if (!text && !label) {
                    issues.push({type: 'button_no_text', element: btn.outerHTML?.slice(0, 60)});
                }
            });

            // Inputs without labels
            document.querySelectorAll('input:not([type="hidden"])').forEach(input => {
                const id = input.id;
                const hasLabel = id && document.querySelector(`label[for="${id}"]`);
                const hasAria = input.getAttribute('aria-label') || input.getAttribute('aria-labelledby');
                const hasPlaceholder = input.placeholder;
                if (!hasLabel && !hasAria && !hasPlaceholder) {
                    issues.push({type: 'input_no_label', element: input.outerHTML?.slice(0, 60)});
                }
            });

            return issues.slice(0, 10);
        }
        """)
        return issues or []
    except Exception:
        return []


async def check_broken_images(page: Page) -> list:
    """Check for broken images."""
    try:
        broken = await page.evaluate("""
        () => {
            return Array.from(document.querySelectorAll('img'))
                .filter(img => !img.complete || img.naturalWidth === 0)
                .map(img => img.src)
                .slice(0, 10);
        }
        """)
        return broken or []
    except Exception:
        return []


# ── Main runner ───────────────────────────────────────────────────

async def run_test_session(session: TestSession) -> AsyncGenerator[dict, None]:
    """
    Main agent loop. Yields SSE-compatible dicts as events happen.
    Each yield is: {"event": "log"|"complete"|"error", "data": {...}}
    """

    bugs: List[BugItem] = []
    warnings: List[WarningItem] = []
    passed: List[PassItem] = []
    full_log: List[LogEntry] = []
    console_errors: List[str] = []
    network_errors: List[dict] = []
    previous_actions: List[dict] = []
    actions_taken = 0
    started_at = iso_now()
    start_time = asyncio.get_event_loop().time()

    def emit_log(type_: str, message: str, screenshot: Optional[str] = None, url: str = "") -> dict:
        entry = LogEntry(type=type_, message=message, screenshot=screenshot, url=url)
        full_log.append(entry)
        session.log.append(entry)
        update_session(session)
        return {"event": "log", "data": entry.model_dump()}

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (compatible; TestPilotAI/1.0; automated-testing)",
        )
        page: Page = await context.new_page()

        # Capture console errors
        def on_console(msg: ConsoleMessage):
            if msg.type == "error":
                console_errors.append(f"{msg.text[:200]}")

        # Capture network failures
        def on_request_failed(request):
            network_errors.append({
                "url": request.url[:100],
                "failure": request.failure or "unknown",
            })

        page.on("console", on_console)
        page.on("requestfailed", on_request_failed)

        try:
            # ── Step 1: Navigate to URL ──────────────────────────
            yield emit_log("info", f"Navigating to {session.url}...")

            try:
                response = await page.goto(
                    session.url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await page.wait_for_timeout(2000)

                if response and response.status >= 400:
                    yield emit_log("error", f"Page returned HTTP {response.status}")
                    bugs.append(BugItem(
                        title=f"Page returns HTTP {response.status}",
                        severity="critical",
                        page_url=session.url,
                        action_taken="Initial navigation",
                        what_happened=f"The page returned a {response.status} status code.",
                        recommendation="Check your server configuration and ensure the URL is correct.",
                    ))
                else:
                    yield emit_log("pass", f"Page loaded successfully ({response.status if response else 'ok'})")
                    passed.append(PassItem(title="Page loads successfully", page_url=session.url))

            except Exception as e:
                yield emit_log("error", f"Failed to load page: {str(e)}")
                bugs.append(BugItem(
                    title="Page failed to load",
                    severity="critical",
                    page_url=session.url,
                    action_taken="Initial navigation",
                    what_happened=str(e),
                    recommendation="Verify the URL is correct and the server is running.",
                ))
                await browser.close()
                report = await _build_report(
                    session, bugs, warnings, passed, full_log,
                    actions_taken, started_at, start_time
                )
                yield {"event": "complete", "data": report.model_dump()}
                return

            current_url = page.url

            # ── Step 2: Take initial screenshot ─────────────────
            screenshot_bytes = await page.screenshot(full_page=False, type="png")
            screenshot_b64 = b64_screenshot(screenshot_bytes)
            yield emit_log("info", "Captured initial screenshot", screenshot=screenshot_b64, url=current_url)

            # ── Step 3: Extract DOM elements ─────────────────────
            yield emit_log("agent", "Analyzing page structure and interactive elements...")
            dom_elements = await extract_dom_elements(page)
            yield emit_log("info", f"Found {len(dom_elements)} interactive elements")

            # ── Step 4: Get initial test plan from the configured AI ──
            yield emit_log("agent", "AI agent is building the test plan...")
            test_plan_response = await get_initial_test_plan(screenshot_bytes, dom_elements, current_url)
            page_summary = test_plan_response.get("page_summary", "")
            test_plan = test_plan_response.get("test_plan", [])

            yield emit_log("agent", f"Page identified: {page_summary}")
            yield emit_log("info", f"Test plan ready — {len(test_plan)} planned actions")

            # ── Step 5: Check accessibility ───────────────────────
            yield emit_log("info", "Running accessibility checks...")
            a11y_issues = await check_accessibility(page)
            for issue in a11y_issues:
                if issue["type"] == "missing_alt":
                    warnings.append(WarningItem(
                        title="Image missing alt text",
                        page_url=current_url,
                        description=f"Image at {issue['element']} has no alt attribute.",
                        recommendation="Add descriptive alt text to all images for screen reader accessibility.",
                    ))
                elif issue["type"] == "button_no_text":
                    warnings.append(WarningItem(
                        title="Button has no accessible text",
                        page_url=current_url,
                        description="A button element has no text content or aria-label.",
                        recommendation="Add text content or aria-label to all buttons.",
                    ))
                elif issue["type"] == "input_no_label":
                    warnings.append(WarningItem(
                        title="Input field missing label",
                        page_url=current_url,
                        description="An input field has no associated label.",
                        recommendation="Associate labels with inputs using the 'for' attribute or aria-label.",
                    ))

            if a11y_issues:
                yield emit_log("warning", f"Found {len(a11y_issues)} accessibility issues")
            else:
                yield emit_log("pass", "Basic accessibility checks passed")
                passed.append(PassItem(title="Basic accessibility checks passed", page_url=current_url))

            # ── Step 6: Check broken images ───────────────────────
            broken_images = await check_broken_images(page)
            if broken_images:
                for img_src in broken_images:
                    bugs.append(BugItem(
                        title="Broken image",
                        severity="medium",
                        page_url=current_url,
                        action_taken="Page load — image check",
                        what_happened=f"Image failed to load: {img_src}",
                        recommendation="Fix the image path or remove the broken image reference.",
                    ))
                yield emit_log("error", f"Found {len(broken_images)} broken images")
            else:
                yield emit_log("pass", "All images loaded successfully")
                passed.append(PassItem(title="All images load correctly", page_url=current_url))

            # ── Step 7: Execute planned test actions ──────────────
            yield emit_log("agent", "Starting automated interaction testing...")

            for planned_action in test_plan[:session.max_actions]:
                if actions_taken >= session.max_actions:
                    break

                action = planned_action.get("action", "")
                selector = planned_action.get("target_selector", "")
                value = planned_action.get("value", "")
                description = planned_action.get("description", "")

                yield emit_log("agent", f"Testing: {description}")

                prev_console_count = len(console_errors)
                prev_network_count = len(network_errors)

                try:
                    success = await _execute_action(page, action, selector, value, description)
                    await page.wait_for_timeout(1000)

                    current_url = page.url
                    new_console_errors = console_errors[prev_console_count:]
                    new_network_errors = network_errors[prev_network_count:]

                    if new_console_errors or new_network_errors:
                        error_analysis = await analyze_error(
                            url=current_url,
                            action_description=description,
                            console_errors=new_console_errors,
                            network_errors=new_network_errors,
                        )
                        if error_analysis.get("is_bug"):
                            ss = await page.screenshot(type="png")
                            bugs.append(BugItem(
                                title=error_analysis.get("title", "Unknown bug"),
                                severity=error_analysis.get("severity", "medium"),
                                page_url=current_url,
                                action_taken=description,
                                what_happened=error_analysis.get("what_happened", ""),
                                screenshot=b64_screenshot(ss),
                                recommendation=error_analysis.get("recommendation", ""),
                            ))
                            yield emit_log("error", f"Bug found: {error_analysis.get('title', 'Unknown bug')}")
                        else:
                            yield emit_log("warning", f"Minor issue during: {description}")
                    elif success:
                        yield emit_log("pass", f"✓ {description}")
                        passed.append(PassItem(title=description, page_url=current_url))
                    else:
                        yield emit_log("warning", f"Could not interact: {description}")
                        warnings.append(WarningItem(
                            title=f"Could not interact with element",
                            page_url=current_url,
                            description=f"Action failed: {description}",
                            recommendation="Check if the element is visible and enabled.",
                        ))

                    previous_actions.append({"action": action, "description": description})
                    actions_taken += 1
                    session.actions_taken = actions_taken
                    update_session(session)

                except Exception as e:
                    yield emit_log("warning", f"Action skipped ({description}): {str(e)[:80]}")

            # ── Step 8: AI-driven follow-up loop ──────────────────
            yield emit_log("agent", "Running AI-driven follow-up checks...")

            follow_up_count = 0
            max_follow_up = max(0, session.max_actions - actions_taken)

            while actions_taken < session.max_actions and follow_up_count < max_follow_up:
                try:
                    screenshot_bytes = await page.screenshot(full_page=False, type="png")
                    dom_elements = await extract_dom_elements(page)
                    current_url = page.url

                    next_action = await get_next_action(
                        screenshot_bytes=screenshot_bytes,
                        dom_elements=dom_elements,
                        url=current_url,
                        previous_actions=previous_actions,
                        actions_taken=actions_taken,
                        max_actions=session.max_actions,
                    )

                    if next_action.get("action") == "done":
                        yield emit_log("agent", "Agent decided all key flows have been tested")
                        break

                    description = next_action.get("description", "")
                    yield emit_log("agent", f"Agent testing: {description}")

                    prev_console = len(console_errors)
                    prev_network = len(network_errors)

                    success = await _execute_action(
                        page,
                        next_action.get("action", ""),
                        next_action.get("target_selector", ""),
                        next_action.get("value", ""),
                        description,
                    )
                    await page.wait_for_timeout(1000)

                    new_console = console_errors[prev_console:]
                    new_network = network_errors[prev_network:]

                    if new_console or new_network:
                        error_analysis = await analyze_error(
                            url=page.url,
                            action_description=description,
                            console_errors=new_console,
                            network_errors=new_network,
                        )
                        if error_analysis.get("is_bug"):
                            ss = await page.screenshot(type="png")
                            bugs.append(BugItem(
                                title=error_analysis.get("title", "Bug detected"),
                                severity=error_analysis.get("severity", "medium"),
                                page_url=page.url,
                                action_taken=description,
                                what_happened=error_analysis.get("what_happened", ""),
                                screenshot=b64_screenshot(ss),
                                recommendation=error_analysis.get("recommendation", ""),
                            ))
                            yield emit_log("error", f"Bug: {error_analysis.get('title', 'Bug detected')}")
                        else:
                            warnings.append(WarningItem(
                                title="Minor issue detected",
                                page_url=page.url,
                                description=f"During: {description}",
                                recommendation="Review the console output for details.",
                            ))
                            yield emit_log("warning", f"Minor issue: {description}")
                    elif success:
                        yield emit_log("pass", f"✓ {description}")
                        passed.append(PassItem(title=description, page_url=page.url))

                    previous_actions.append({"action": next_action.get("action"), "description": description})
                    actions_taken += 1
                    follow_up_count += 1
                    session.actions_taken = actions_taken
                    update_session(session)

                except Exception as e:
                    yield emit_log("warning", f"Follow-up action skipped: {str(e)[:80]}")
                    follow_up_count += 1

            # ── Step 9: Final console error sweep ─────────────────
            if console_errors:
                yield emit_log("warning", f"Total console errors detected: {len(console_errors)}")
                if len(console_errors) > 3:
                    warnings.append(WarningItem(
                        title=f"{len(console_errors)} JavaScript console errors",
                        page_url=session.url,
                        description=f"Errors: {'; '.join(console_errors[:3])}",
                        recommendation="Review and fix JavaScript errors in the browser console.",
                    ))

            # ── Step 10: Build final report ────────────────────────
            yield emit_log("agent", "Generating final test report...")
            report = await _build_report(
                session, bugs, warnings, passed, full_log,
                actions_taken, started_at, start_time
            )

            session.status = "completed"
            session.report = report
            update_session(session)

            yield emit_log("info", f"Test complete — {len(bugs)} bugs, {len(warnings)} warnings, {len(passed)} passed")
            yield {"event": "complete", "data": report.model_dump()}

        except Exception as e:
            session.status = "failed"
            update_session(session)
            yield emit_log("error", f"Agent crashed: {str(e)}")
            yield {"event": "error", "data": {"message": str(e)}}

        finally:
            await browser.close()


async def _execute_action(page: Page, action: str, selector: str, value: str, description: str) -> bool:
    """Execute a single Playwright action. Returns True if successful."""
    try:
        if action == "navigate" and selector:
            url = selector if selector.startswith("http") else value
            if url:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1500)
                return True

        elif action == "click" and selector:
            try:
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=5000)
                await locator.click(timeout=5000)
                return True
            except Exception:
                # Try by text if selector fails
                if value:
                    await page.get_by_text(value, exact=False).first.click(timeout=3000)
                    return True
                return False

        elif action == "type" and selector:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=5000)
            await locator.fill(value or "test input", timeout=5000)
            return True

        elif action == "submit" and selector:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=5000)
            await locator.click(timeout=5000)
            await page.wait_for_timeout(1500)
            return True

        elif action == "scroll":
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            return True

        elif action == "hover" and selector:
            locator = page.locator(selector).first
            await locator.hover(timeout=5000)
            return True

    except Exception:
        return False

    return False


async def _build_report(
    session: TestSession,
    bugs: list,
    warnings: list,
    passed: list,
    full_log: list,
    actions_taken: int,
    started_at: str,
    start_time: float,
) -> TestReport:
    completed_at = iso_now()
    duration = asyncio.get_event_loop().time() - start_time

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

    return TestReport(
        session_id=session.session_id,
        url=session.url,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=round(duration, 1),
        overall_score=score,
        actions_taken=actions_taken,
        bugs=bugs,
        warnings=warnings,
        passed=passed,
        full_log=full_log,
        summary=summary,
        status="completed",
    )
