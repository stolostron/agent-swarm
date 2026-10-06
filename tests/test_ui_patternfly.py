"""Playwright-based UI tests for the PatternFly-migrated Swarmer UI.

These tests verify that all pages render correctly with PatternFly components,
that navigation works, forms are functional, and HTMX interactions are preserved.

Requires the dev server running at http://127.0.0.1:8091 with SWARMER_DEV_AUTH=1.
"""

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright

BASE_URL = "http://127.0.0.1:8091"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    yield page
    page.close()
    context.close()


# ── Login Page ────────────────────────────────────────────────

class TestLoginPage:
    def test_login_page_loads(self, page: Page):
        page.goto(f"{BASE_URL}/login")
        assert page.title() == "Swarmer — Login"

    def test_login_has_patternfly_dark_theme(self, page: Page):
        page.goto(f"{BASE_URL}/login")
        html = page.locator("html")
        expect(html).to_have_class(re.compile(r"pf-v6-theme-dark"))

    def test_login_has_card_layout(self, page: Page):
        page.goto(f"{BASE_URL}/login")
        card = page.locator(".pf-v6-c-card")
        expect(card).to_be_visible()

    def test_login_has_title(self, page: Page):
        page.goto(f"{BASE_URL}/login")
        title = page.locator(".pf-v6-c-title")
        expect(title).to_contain_text("Swarmer")

    def test_login_has_bearer_token_field(self, page: Page):
        page.goto(f"{BASE_URL}/login")
        textarea = page.locator("textarea[name='token']")
        expect(textarea).to_be_visible()

    def test_login_has_sign_in_button(self, page: Page):
        page.goto(f"{BASE_URL}/login")
        btn = page.locator("button[type='submit']")
        expect(btn).to_contain_text("Sign in")
        expect(btn).to_have_class(re.compile(r"pf-v6-c-button"))
        expect(btn).to_have_class(re.compile(r"pf-m-primary"))


# ── Masthead (on authenticated pages) ─────────────────────────

class TestMasthead:
    def test_masthead_visible(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces")
        masthead = page.locator(".pf-v6-c-masthead")
        expect(masthead).to_be_visible()

    def test_masthead_brand_text(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces")
        brand = page.locator(".pf-v6-c-masthead__brand")
        expect(brand).to_contain_text("Agent Swarm")

    def test_masthead_has_logout(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces")
        logout = page.locator("button:has-text('Logout')")
        expect(logout).to_be_visible()


# ── Workspace List (Empty State) ─────────────────────────────

class TestWorkspaceListEmpty:
    def test_page_title(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces")
        assert page.title() == "Workspaces — Swarmer"

    def test_empty_state_shown(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces")
        empty = page.locator(".pf-v6-c-empty-state")
        expect(empty).to_be_visible()
        expect(empty).to_contain_text("No workspaces yet")

    def test_new_workspace_button(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces")
        btn = page.locator("a:has-text('+ New Workspace')")
        expect(btn).to_be_visible()
        expect(btn).to_have_class(re.compile(r"pf-v6-c-button"))
        expect(btn).to_have_class(re.compile(r"pf-m-primary"))

    def test_create_first_workspace_link(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces")
        link = page.locator("a:has-text('Create your first workspace')")
        expect(link).to_be_visible()
        expect(link).to_have_attribute("href", "/workspaces/new")


# ── New Workspace Form ────────────────────────────────────────

class TestNewWorkspaceForm:
    def test_page_title(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces/new")
        assert page.title() == "New Workspace — Swarmer"

    def test_breadcrumb_present(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces/new")
        breadcrumb = page.locator(".pf-v6-c-breadcrumb")
        expect(breadcrumb).to_be_visible()
        expect(breadcrumb).to_contain_text("Workspaces")
        expect(breadcrumb).to_contain_text("New Workspace")

    def test_form_fields_present(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces/new")
        name_input = page.locator("input[name='display_name']")
        expect(name_input).to_be_visible()
        expect(name_input).to_have_class(re.compile(r"pf-v6-c-form-control"))

        desc_textarea = page.locator("textarea[name='description']")
        expect(desc_textarea).to_be_visible()

    def test_namespace_preview_card(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces/new")
        card = page.locator(".pf-v6-c-card")
        expect(card).to_be_visible()
        expect(card).to_contain_text("Kubernetes Namespace")

    def test_submit_button(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces/new")
        btn = page.locator("button.pf-m-primary[type='submit']")
        expect(btn).to_contain_text("Create Workspace")

    def test_cancel_link(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces/new")
        cancel = page.locator("a:has-text('Cancel')")
        expect(cancel).to_be_visible()

    def test_htmx_namespace_preview_wired(self, page: Page):
        """Verify the HTMX attributes for live namespace preview are present."""
        page.goto(f"{BASE_URL}/workspaces/new")
        name_input = page.locator("input[name='display_name']")
        expect(name_input).to_have_attribute("hx-get", "/workspaces/preview-namespace")
        expect(name_input).to_have_attribute("hx-trigger", "input delay:300ms")
        expect(name_input).to_have_attribute("hx-target", "#namespace-preview")

    def test_required_asterisk_shown(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces/new")
        required = page.locator(".pf-v6-c-form__label-required")
        expect(required.first).to_be_visible()


# ── PatternFly Component Usage ────────────────────────────────

class TestPatternFlyComponents:
    def test_no_bootstrap_css_loaded(self, page: Page):
        """Verify Bootstrap CSS is not loaded in any page."""
        page.goto(f"{BASE_URL}/workspaces")
        # Check that no bootstrap stylesheet is linked
        bootstrap_links = page.locator("link[href*='bootstrap']")
        assert bootstrap_links.count() == 0

    def test_patternfly_css_loaded(self, page: Page):
        """Verify PatternFly CSS is loaded."""
        page.goto(f"{BASE_URL}/workspaces")
        pf_links = page.locator("link[href*='patternfly']")
        assert pf_links.count() >= 1

    def test_no_bootstrap_js_loaded(self, page: Page):
        """Verify Bootstrap JS bundle is not loaded."""
        page.goto(f"{BASE_URL}/workspaces")
        bootstrap_scripts = page.locator("script[src*='bootstrap']")
        assert bootstrap_scripts.count() == 0

    def test_htmx_loaded(self, page: Page):
        """Verify HTMX is still loaded (not removed during migration)."""
        page.goto(f"{BASE_URL}/workspaces")
        htmx_scripts = page.locator("script[src*='htmx']")
        assert htmx_scripts.count() >= 1

    def test_dark_theme_on_all_pages(self, page: Page):
        """Verify dark theme class is on html element."""
        for path in ["/login", "/workspaces", "/workspaces/new"]:
            page.goto(f"{BASE_URL}{path}")
            html = page.locator("html")
            expect(html).to_have_class(re.compile(r"pf-v6-theme-dark"))

    def test_no_bootstrap_class_remnants(self, page: Page):
        """Verify no Bootstrap-specific class prefixes remain in the page HTML."""
        page.goto(f"{BASE_URL}/workspaces")
        html_content = page.content()
        # These Bootstrap-specific patterns should not appear
        assert 'class="btn ' not in html_content
        assert 'class="card ' not in html_content
        assert 'data-bs-' not in html_content
        assert 'class="navbar' not in html_content


# ── Auth Callback Page ────────────────────────────────────────

class TestAuthCallback:
    def test_callback_page_loads(self, page: Page):
        page.goto(f"{BASE_URL}/auth/callback")
        assert "Completing sign-in" in page.title() or "Swarmer" in page.title()

    def test_callback_has_dark_theme(self, page: Page):
        page.goto(f"{BASE_URL}/auth/callback")
        html = page.locator("html")
        expect(html).to_have_class(re.compile(r"pf-v6-theme-dark"))

    def test_callback_has_patternfly_card(self, page: Page):
        page.goto(f"{BASE_URL}/auth/callback")
        card = page.locator(".pf-v6-c-card")
        expect(card).to_be_visible()


# ── Navigation ────────────────────────────────────────────────

class TestNavigation:
    def test_root_redirects_to_workspaces(self, page: Page):
        page.goto(f"{BASE_URL}/")
        page.wait_for_url(f"{BASE_URL}/workspaces")
        assert "/workspaces" in page.url

    def test_new_workspace_button_navigates(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces")
        page.click("a:has-text('+ New Workspace')")
        page.wait_for_url(f"{BASE_URL}/workspaces/new")
        assert "/workspaces/new" in page.url

    def test_breadcrumb_navigation(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces/new")
        page.click(".pf-v6-c-breadcrumb__link:has-text('Workspaces')")
        page.wait_for_url(f"{BASE_URL}/workspaces")
        assert "/workspaces" in page.url

    def test_brand_link_goes_to_workspaces(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces/new")
        page.click(".pf-v6-c-masthead__brand a")
        page.wait_for_url(f"{BASE_URL}/workspaces")


# ── Responsive Design ────────────────────────────────────────

class TestResponsiveDesign:
    def test_mobile_viewport(self, browser):
        context = browser.new_context(viewport={"width": 375, "height": 667})
        page = context.new_page()
        page.goto(f"{BASE_URL}/workspaces")
        masthead = page.locator(".pf-v6-c-masthead")
        expect(masthead).to_be_visible()
        page.close()
        context.close()

    def test_tablet_viewport(self, browser):
        context = browser.new_context(viewport={"width": 768, "height": 1024})
        page = context.new_page()
        page.goto(f"{BASE_URL}/workspaces/new")
        # Form should still be visible
        name_input = page.locator("input[name='display_name']")
        expect(name_input).to_be_visible()
        page.close()
        context.close()


class TestLaunchConfirmationDialog:
    def test_escape_clear_backdrop_and_focus_management(self, page: Page):
        page.goto(f"{BASE_URL}/workspaces")
        page.route(
            "**/launch-dialog*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body='''<form id="launch-confirm-form">
                  <h2 id="launch-dialog-title">Launch session</h2>
                  <label for="launch-mode">Launch mode</label>
                  <select id="launch-mode" autofocus><option>Prompt</option></select>
                  <select id="launch-prompt-id"><option data-content="">None</option></select>
                  <pre id="launch-prompt-preview-content"></pre><span id="launch-no-prompt"></span>
                  <textarea id="launch-instruction-prompt"></textarea>
                  <button id="clear-launch-instructions" type="button">Clear</button>
                  <button type="button" data-launch-dialog-close>Cancel</button>
                  <button type="submit">Confirm launch</button>
                </form>''',
            ),
        )
        host = Path("swarmer/templates/sessions/_launch_dialog_host.html").read_text()
        page.set_content(
            '<button id="opener" data-launch-dialog-url="/launch-dialog?id=7">Launch</button>'
            + host
        )
        launches = []
        page.on("request", lambda request: launches.append(request.url) if request.method == "POST" else None)
        opener = page.locator("#opener")
        opener.click()
        dialog = page.get_by_role("dialog", name="Launch session")
        expect(dialog).to_be_visible()
        expect(page.locator("#launch-instruction-prompt")).to_have_value("")
        expect(page.locator("#launch-mode")).to_be_focused()

        page.locator("#launch-instruction-prompt").fill("temporary")
        page.get_by_role("button", name="Clear").click()
        expect(page.locator("#launch-instruction-prompt")).to_have_value("")

        page.get_by_role("button", name="Confirm launch").focus()
        page.keyboard.press("Tab")
        expect(page.locator("#launch-mode")).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(page.get_by_role("button", name="Confirm launch")).to_be_focused()

        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(opener).to_be_focused()
        assert launches == []

        opener.click()
        page.locator("#launch-dialog-backdrop").click(position={"x": 5, "y": 5})
        expect(dialog).to_be_hidden()
        expect(opener).to_be_focused()
        assert launches == []
