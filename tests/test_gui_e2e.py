"""Minimal browser-level smoke tests for the QuickHatch GUI.

These tests intentionally cover only high-risk visible product flows and do
not require real SSH, model APIs, VMs, or hardware. They are optional because
Playwright needs a browser binary:

    python -m pip install -e ".[dev,e2e]"
    python -m playwright install chromium
    python -m pytest tests/test_gui_e2e.py -q

If Playwright or Chromium is unavailable, the module skips. Keep these tests
out of the default release-smoke gate unless the runner has browsers installed.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from quickhatch.bridge.server import BridgeState
from quickhatch.config import UserProfile
from quickhatch.gui.server import GUIState, make_gui_server

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import Page, expect, sync_playwright
except ImportError:  # pragma: no cover - exercised only without optional deps
    pytestmark = [
        pytest.mark.e2e_browser,
        pytest.mark.skip(reason='Playwright not installed. Run `pip install -e ".[dev,e2e]"`.'),
    ]
else:
    pytestmark = pytest.mark.e2e_browser


PRE_SETUP_STEPS = [
    "welcome",
    "ai_setup",
    "questionnaire",
    "scan",
    "apps_data",
    "additional",
    "bridge",
    "usb",
    "ssh",
    "export",
]


def _post(port: int, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


@pytest.fixture
def gui_server() -> Iterator[dict[str, Any]]:
    """Start the real GUI server without persistent bridge storage."""

    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    gui_state = GUIState(profile, bridge)
    srv, port = make_gui_server(gui_state, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"server": srv, "port": port, "gui_state": gui_state, "bridge": bridge}
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture(scope="session")
def playwright_browser():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            pytest.skip(
                f"Playwright Chromium is unavailable; run `python -m playwright install chromium`: {exc}"
            )
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(playwright_browser):
    context = playwright_browser.new_context(viewport={"width": 1280, "height": 720})
    browser_page = context.new_page()
    page_errors: list[str] = []
    browser_page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    try:
        yield browser_page
        assert page_errors == []
    finally:
        context.close()


def _set_completed(gui_server: dict[str, Any], completed: list[str]) -> None:
    gui_server["gui_state"].profile.completed_steps = completed


def _configure_migration_profile(gui_server: dict[str, Any]) -> UserProfile:
    profile = gui_server["gui_state"].profile
    profile.preferences.migration_type = "fresh_install"
    profile.preferences.another_machine = "yes"
    return profile


def _open_gui(page: Page, gui_server: dict[str, Any]) -> None:
    page.goto(f"http://127.0.0.1:{gui_server['port']}", wait_until="load")
    expect(page.locator(".brand")).to_have_text("QuickHatch")


def _open_questionnaire(page: Page, gui_server: dict[str, Any]) -> None:
    _configure_migration_profile(gui_server)
    _set_completed(gui_server, ["welcome", "ai_setup"])
    _open_gui(page, gui_server)
    expect(page.get_by_role("heading", name="Describe your ideal desktop")).to_be_visible()


def _open_setup(page: Page, gui_server: dict[str, Any], *, destructive: bool = False) -> None:
    profile = _configure_migration_profile(gui_server)
    profile.linux_ip = ""
    profile.linux_user = ""
    profile.remote_setup_mode = "wipe_and_reinstall" if destructive else "configure_only"
    profile.install_profile = "full_disk_wipe" if destructive else "configure_existing"
    _set_completed(gui_server, PRE_SETUP_STEPS.copy())
    _open_gui(page, gui_server)
    expect(page.get_by_role("heading", name="Remote setup")).to_be_visible()


def _mock_successful_ssh_test(page: Page) -> None:
    page.route(
        "**/wizard/test-ssh",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "ok", "info": "Linux test-host"}),
        ),
    )


def _center_is_uncovered(locator) -> bool:
    locator.scroll_into_view_if_needed()
    expect(locator).to_be_visible()
    return locator.evaluate(
        """target => {
            const rect = target.getBoundingClientRect();
            const x = rect.left + rect.width / 2;
            const y = rect.top + rect.height / 2;
            const top = document.elementFromPoint(x, y);
            return top === target || target.contains(top);
        }"""
    )


def test_basic_load_has_no_js_errors_and_theme_toggle_works(page: Page, gui_server: dict[str, Any]):
    _open_gui(page, gui_server)

    expect(page.get_by_role("heading", name="What are we doing?")).to_be_visible()
    before = page.locator("html").get_attribute("data-theme")
    theme_toggle = page.locator("#themeToggle")
    expect(theme_toggle).to_be_visible()

    theme_toggle.click()

    after = page.locator("html").get_attribute("data-theme")
    assert after in {"light", "dark"}
    assert after != before


def test_safe_install_profile_is_default_path(page: Page, gui_server: dict[str, Any]):
    _open_questionnaire(page, gui_server)

    selected = page.locator("#installProfileChoice .choice.selected")
    expect(selected).to_have_count(1)
    assert "Configure an existing Linux install" in selected.inner_text()
    assert "Erase one whole disk" not in selected.inner_text()


def test_full_disk_selection_shows_persistent_destructive_warning(
    page: Page, gui_server: dict[str, Any]
):
    _open_questionnaire(page, gui_server)

    page.get_by_text("Erase one whole disk and install Linux").click()

    selected = page.locator("#installProfileChoice .choice.selected")
    expect(selected).to_contain_text("Erase one whole disk")
    expect(page.get_by_text("Destructive install guardrails")).to_be_visible()
    expect(page.get_by_text("exact disk or partition confirmation")).to_be_visible()
    expect(
        page.get_by_text("QuickHatch does not automatically shrink Windows partitions")
    ).to_be_visible()


def test_remote_setup_launch_requires_successful_test_and_invalidates_on_edit(
    page: Page, gui_server: dict[str, Any]
):
    _open_setup(page, gui_server)
    _mock_successful_ssh_test(page)

    launch = page.locator("#setupLaunchBtn")
    test_button = page.locator("#testConnBtn")
    expect(launch).to_be_disabled()
    expect(test_button).to_be_disabled()

    page.locator("#linuxIp").fill("192.168.1.42")
    page.locator("#linuxUser").fill("root")
    expect(test_button).to_be_enabled()
    expect(launch).to_be_disabled()

    test_button.click()

    expect(page.locator("#connStatus")).to_contain_text("Connected")
    expect(launch).to_be_enabled()

    page.locator("#linuxIp").fill("192.168.1.43")

    expect(page.locator("#connStatus")).to_contain_text("Connection changed")
    expect(launch).to_be_disabled()


def test_pending_question_card_appears_and_clears_after_answer(
    page: Page, gui_server: dict[str, Any]
):
    _open_setup(page, gui_server)
    gui_server["bridge"].update_setup_run(status="running", stage="apps")
    gui_server["bridge"].add_question(
        "Which disk should QuickHatch inspect?",
        choices=["/dev/sda", "/dev/nvme0n1"],
        blocking=True,
        phase="setup",
    )

    page.reload(wait_until="load")

    question_card = page.locator("#setupQuestions .question-card")
    expect(question_card).to_be_visible()
    expect(question_card).to_contain_text("Pending question")
    expect(question_card).to_contain_text("Which disk should QuickHatch inspect?")

    question_card.get_by_text("/dev/sda").click()
    question_card.get_by_role("button", name="Answer").click()

    expect(question_card).to_have_count(0, timeout=5000)


@pytest.mark.parametrize(
    ("status", "terminal", "phase_text", "phase_class", "button_text", "done_enabled", "support"),
    [
        ("success", "Finished: Finished", "Finished", "done", "Finish", True, False),
        (
            "success_with_warnings",
            "Finished: Completed with warnings",
            "Completed with warnings",
            "warning",
            "Review warnings",
            True,
            True,
        ),
        (
            "manual_followup_required",
            "Finished: Manual follow-up",
            "Completed; follow-up required",
            "followup",
            "Review follow-up",
            True,
            True,
        ),
        (
            "unsupported_safe",
            "Finished: Safe but unsupported",
            "Safe but unsupported",
            "unsupported",
            "Review next steps",
            True,
            True,
        ),
        ("blocked", "Error: blocked", "Setup blocked", "error", "Setup stopped", False, True),
    ],
)
def test_final_statuses_have_distinct_visible_states(
    page: Page,
    gui_server: dict[str, Any],
    status: str,
    terminal: str,
    phase_text: str,
    phase_class: str,
    button_text: str,
    done_enabled: bool,
    support: bool,
):
    _open_setup(page, gui_server)
    gui_server["bridge"].add_message("agent", terminal, phase="setup")
    gui_server["bridge"].update_setup_run(status=status, final_status=status, stage="final_audit")

    page.reload(wait_until="load")

    phase = page.locator("#setupPhase")
    expect(phase).to_have_text(phase_text)
    assert phase_class in (phase.get_attribute("class") or "")
    if phase_class != "done":
        assert "done" not in (phase.get_attribute("class") or "")

    done_button = page.locator("#setupDone")
    expect(done_button).to_have_text(button_text)
    if done_enabled:
        expect(done_button).to_be_enabled()
    else:
        expect(done_button).to_be_disabled()

    support_action = page.locator("#supportBundleAction")
    if support:
        expect(support_action).to_be_visible()
        expect(support_action).to_contain_text("Create a redacted support ZIP")
    else:
        expect(support_action).to_have_count(0)


def test_mobile_width_keeps_primary_setup_actions_reachable(page: Page, gui_server: dict[str, Any]):
    page.set_viewport_size({"width": 390, "height": 740})
    _open_setup(page, gui_server, destructive=True)
    _mock_successful_ssh_test(page)

    page.locator("#linuxIp").fill("192.168.1.42")
    page.locator("#linuxUser").fill("root")
    page.locator("#testConnBtn").click()

    launch = page.locator("#setupLaunchBtn")
    expect(launch).to_be_enabled()
    expect(launch).to_have_text("Start destructive remote setup")
    assert _center_is_uncovered(launch)

    back = page.locator(".btn-row button", has_text="Back")
    expect(back).to_be_visible()
    assert _center_is_uncovered(back)
