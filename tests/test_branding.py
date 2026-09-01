"""Tests for Kubecost branding of FastMCP's browser-facing OAuth pages.

The theme overlay works by appending CSS to, and rewriting a few strings in,
HTML that FastMCP generates. That coupling is the thing worth testing: a FastMCP
upgrade that renames a page builder, restructures the markup, or reworks the
consent copy would otherwise silently drop the branding. These tests fail loudly
instead.
"""

from __future__ import annotations

import html as html_module
import re

import pytest
from fastmcp.server.auth.handlers import authorize as fastmcp_authorize
from fastmcp.server.auth.oauth_proxy import consent as fastmcp_consent, proxy as fastmcp_proxy

from mcp_kubecost import branding
from mcp_kubecost.branding import (
    FAVICON_MEDIA_TYPE,
    FAVICON_PNG,
    FONT_STACK,
    INK,
    KUBECOST_LOGO_DARK_DATA_URI,
    KUBECOST_LOGO_DATA_URI,
    KUBECOST_WEBSITE_URL,
    apply_kubecost_branding,
    install_oauth_page_branding,
    server_icons,
)

# Builders the overlay rebinds, as (module, attribute) pairs.
_BRANDED_BUILDERS = (
    (fastmcp_consent, "create_consent_html"),
    (fastmcp_proxy, "create_error_html"),
    (fastmcp_authorize, "create_unregistered_client_html"),
)


@pytest.fixture
def branding_installed():
    """Install the overlay and restore FastMCP's original builders afterwards."""
    originals = [(module, name, getattr(module, name)) for module, name in _BRANDED_BUILDERS]
    install_oauth_page_branding()
    yield
    for module, name, original in originals:
        setattr(module, name, original)


def _render_consent() -> str:
    return fastmcp_consent.create_consent_html(
        client_id="d3f1c0a2-5b7e-4a91-9c3d-8e2f6a4b1d07",
        redirect_uri="http://127.0.0.1:53219/oauth/callback",
        scopes=["openid", "profile"],
        txn_id="txn_abc123",
        csrf_token="csrf_xyz789",
        client_name="Claude Desktop",
        server_name="mcp-kubecost",
        server_icon_url=KUBECOST_LOGO_DATA_URI,
        server_website_url=KUBECOST_WEBSITE_URL,
    )


# --- Logo --------------------------------------------------------------------


def test_logo_is_a_self_contained_png_data_uri():
    """No external asset: the consent CSP allows `img-src data:` and egress may not exist."""
    for uri in (KUBECOST_LOGO_DATA_URI, KUBECOST_LOGO_DARK_DATA_URI):
        assert uri.startswith("data:image/png;base64,")
        # A raw quote would terminate the enclosing href/src attribute early.
        assert '"' not in uri and "'" not in uri


def test_server_icons_offers_a_light_and_dark_variant():
    icons = server_icons()
    assert [icon.src for icon in icons] == [KUBECOST_LOGO_DATA_URI, KUBECOST_LOGO_DARK_DATA_URI]
    assert [getattr(icon, "theme", None) for icon in icons] == ["light", "dark"]
    for icon in icons:
        assert icon.mimeType == "image/png"
        # Concrete pixel size for a raster image.
        assert icon.sizes == ["128x128"]


def test_light_variant_is_first_because_fastmcp_renders_icons_zero():
    """FastMCP's OAuth pages are light, and it always renders icons[0]."""
    assert server_icons()[0].src == KUBECOST_LOGO_DATA_URI
    assert getattr(server_icons()[0], "theme", None) == "light"


def test_icon_theme_survives_serialization():
    """`theme` is not a declared field on mcp.types.Icon; it rides on extra="allow"."""
    dumped = server_icons()[0].model_dump(exclude_none=True)
    assert dumped["theme"] == "light"
    assert dumped["sizes"] == ["128x128"]


def test_favicon_bytes_are_the_same_mark():
    # PNG magic bytes: \x89PNG\r\n\x1a\n
    assert FAVICON_PNG[:4] == b"\x89PNG"
    assert FAVICON_MEDIA_TYPE == "image/png"


# --- Theme overlay -----------------------------------------------------------


def test_apply_branding_appends_style_inside_head():
    """Ours must land after FastMCP's block so equal-specificity rules win."""
    branded = apply_kubecost_branding("<html><head><style>a{}</style></head><body></body></html>")
    assert branded.count("</head>") == 1
    head = branded[: branded.index("</head>")]
    assert head.index("a{}") < head.index(FONT_STACK)


def test_apply_branding_leaves_html_without_a_head_untouched():
    """FastMCP emits bare fragments too; the /favicon.ico route covers those."""
    fragment = "<h1>Error</h1><p>Invalid or expired transaction</p>"
    assert apply_kubecost_branding(fragment) == fragment


def test_apply_branding_declares_an_inline_favicon():
    """Without a rel=icon, browsers request /favicon.ico and log a 404.

    Verified in a real browser: with FastMCP's consent CSP over https, Chrome
    requests /favicon.ico when no icon is declared and does not when one is.
    """
    branded = apply_kubecost_branding("<html><head></head><body></body></html>")
    assert 'rel="icon"' in branded
    # A data: URI means no second request at all, which is the point.
    assert f'href="{KUBECOST_LOGO_DATA_URI}"' in branded
    head = branded[: branded.index("</head>")]
    assert 'rel="icon"' in head, "icon link must be inside <head>"


def test_apply_branding_is_not_cumulative():
    once = apply_kubecost_branding("<html><head></head></html>")
    assert apply_kubecost_branding(once).count(FONT_STACK) == 2, (
        "apply_kubecost_branding is not idempotent by design; install_oauth_page_branding "
        "must be what guards against double-wrapping"
    )


# --- Installation ------------------------------------------------------------


def test_install_wraps_every_page_builder(branding_installed):
    for module, name in _BRANDED_BUILDERS:
        assert getattr(getattr(module, name), "__wrapped__", None) is not None, (
            f"{module.__name__}.{name} was not wrapped — FastMCP may have renamed it"
        )


def test_install_is_idempotent(branding_installed):
    wrapped = [getattr(module, name) for module, name in _BRANDED_BUILDERS]
    install_oauth_page_branding()
    assert [getattr(module, name) for module, name in _BRANDED_BUILDERS] == wrapped


def test_install_warns_and_continues_when_a_builder_is_missing(monkeypatch, caplog):
    """A FastMCP rename must not break startup — unbranded pages are cosmetic."""
    monkeypatch.delattr(fastmcp_proxy, "create_error_html")
    originals = [(m, n, getattr(m, n, None)) for m, n in _BRANDED_BUILDERS if hasattr(m, n)]
    try:
        with caplog.at_level("WARNING", logger=branding.__name__):
            install_oauth_page_branding()
        assert "create_error_html" in caplog.text
        # The other builders still got wrapped.
        assert getattr(fastmcp_consent.create_consent_html, "__wrapped__", None) is not None
    finally:
        for module, name, original in originals:
            setattr(module, name, original)


# --- Rendered pages ----------------------------------------------------------


def test_consent_page_uses_the_kubecost_palette_and_font(branding_installed):
    html = _render_consent()
    assert FONT_STACK in html
    assert INK in html
    assert "Space Grotesk" in html


def test_consent_page_drops_fastmcp_branding(branding_installed):
    html = _render_consent()
    assert "gofastmcp.com" not in html, "a FastMCP logo or link survived"
    assert "FastMCP" not in html


def test_consent_page_names_kubecost_in_the_help_tooltip(branding_installed):
    """Guards the copy substitutions in _COPY_SUBSTITUTIONS against FastMCP rewording."""
    html = _render_consent()
    assert "This Kubecost MCP server requires your consent" in html
    assert "MCP security" in html
    assert "github.com/kubecost/mcp-kubecost" in html


def test_consent_page_keeps_the_security_critical_form_intact(branding_installed):
    """Branding must not disturb what makes the consent screen work."""
    html = _render_consent()
    assert 'name="csrf_token" value="csrf_xyz789"' in html
    assert 'name="txn_id" value="txn_abc123"' in html
    assert 'value="approve"' in html and 'value="deny"' in html
    assert "http://127.0.0.1:53219/oauth/callback" in html
    # CSP meta tag must survive the head rewrite.
    assert "Content-Security-Policy" in html


def test_consent_page_needs_no_csp_relaxation(branding_installed):
    """The overlay adds an inline <style> and a data: image — both already permitted."""
    html = _render_consent()
    csp = re.search(r'Content-Security-Policy" content="([^"]+)"', html)
    assert csp is not None
    policy = html_module.unescape(csp.group(1))  # attribute is escaped (&#x27; for ')
    assert "style-src 'unsafe-inline'" in policy
    # `img-src data:` is what makes both the logo and the inline favicon legal.
    assert "data:" in policy
    # No webfont is fetched, so the absent font-src directive is not a problem.
    assert "font-src" not in policy


def test_consent_page_declares_a_favicon(branding_installed):
    """The page must not fall back to requesting /favicon.ico."""
    assert 'rel="icon"' in _render_consent()


def test_error_pages_declare_a_favicon(branding_installed):
    html = fastmcp_proxy.create_error_html(
        error_title="Authorization Failed",
        error_message="Expired.",
        server_name="mcp-kubecost",
        server_icon_url=KUBECOST_LOGO_DATA_URI,
    )
    assert 'rel="icon"' in html


def test_callback_error_pages_use_kubecost_logo_not_fastmcp(branding_installed):
    """Callback error pages omit server_icon_url; FastMCP falls back to its own logo URL.

    The branding overlay must replace that URL with the Kubecost data URI so the page
    never makes an external request and the FastMCP logo never appears.
    """
    html = fastmcp_proxy.create_error_html(
        error_title="OAuth Error",
        error_message="Missing authorization code.",
        # No server_icon_url — simulates the callback handler's create_error_html calls.
    )
    assert "gofastmcp.com" not in html, "FastMCP logo URL must be replaced"
    assert KUBECOST_LOGO_DATA_URI in html, "Kubecost logo data URI must appear instead"


def test_error_pages_are_branded(branding_installed):
    html = fastmcp_proxy.create_error_html(
        error_title="Authorization Failed",
        error_message="The authorization code has expired.",
        error_details={"Error Code": "invalid_grant"},
        server_name="mcp-kubecost",
        server_icon_url=KUBECOST_LOGO_DATA_URI,
    )
    assert FONT_STACK in html
    assert "gofastmcp.com" not in html
    assert "The authorization code has expired." in html


def test_unregistered_client_page_is_branded(branding_installed):
    html = fastmcp_authorize.create_unregistered_client_html(
        client_id="unknown-client",
        registration_endpoint="https://kubecost.example.com/mcp/register",
        discovery_endpoint="https://kubecost.example.com/.well-known/oauth-authorization-server",
        server_name="mcp-kubecost",
        server_icon_url=KUBECOST_LOGO_DATA_URI,
    )
    assert FONT_STACK in html
    assert "FastMCP" not in html


# --- Server wiring -----------------------------------------------------------


def test_fastmcp_still_reads_branding_off_the_server_instance():
    """The logo and server-name link come from FastMCP(icons=..., website_url=...).

    FastMCP reads both in ``ConsentMixin._show_consent_page``. If it stops doing
    so, ``server.py`` passing them is dead configuration.
    """
    import inspect

    source = inspect.getsource(fastmcp_consent.ConsentMixin._show_consent_page)
    assert "fastmcp.icons" in source
    assert "fastmcp.website_url" in source
