"""GET /dashboard and /dashboard/app.js: Basic auth, headers, and what the page may contain."""

import re

import pytest

from tests.helpers import DASHBOARD_AUTH, TEST_DASHBOARD_USER

pytestmark = pytest.mark.anyio

ROUTES = ["/dashboard", "/dashboard/app.js"]

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
CHART_JS_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js"


@pytest.mark.parametrize("path", ROUTES)
async def test_without_credentials_is_401(client, path):
    response = await client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


@pytest.mark.parametrize("path", ROUTES)
async def test_wrong_password_is_401(client, path):
    response = await client.get(path, auth=(TEST_DASHBOARD_USER, "wrong"))
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


@pytest.mark.parametrize(
    ("path", "content_type"),
    [("/dashboard", "text/html"), ("/dashboard/app.js", "javascript")],
)
async def test_with_credentials_serves_file_with_csp(client, path, content_type):
    response = await client.get(path, auth=DASHBOARD_AUTH)
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert response.headers["content-security-policy"] == EXPECTED_CSP


async def test_html_references_pinned_chartjs_and_app_script(client):
    html = (await client.get("/dashboard", auth=DASHBOARD_AUTH)).text
    assert f'src="{CHART_JS_URL}"' in html
    assert 'src="/dashboard/app.js"' in html
    # The CSP blocks inline script, so there must be none: every <script>
    # has a src and an empty body, and there are no on...= event handlers.
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.DOTALL | re.IGNORECASE)
    assert len(scripts) == 2
    for attributes, body in scripts:
        assert "src=" in attributes
        assert body.strip() == ""
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.IGNORECASE)


async def test_script_never_builds_markup_from_strings(client):
    served = (await client.get("/dashboard/app.js", auth=DASHBOARD_AUTH)).text
    assert "fetch(" in served  # the real script, not an empty file
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in served
