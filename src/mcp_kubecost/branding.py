"""Kubecost branding for the browser-facing OAuth pages.

FastMCP renders the pages a user actually sees during the OAuth flow — the
consent screen, the OAuth error pages, and the unregistered-client page — and
styles them with its own logo and palette. Two seams make them read as Kubecost:

1. **Supported API.** ``server.py`` passes ``icons=`` and ``website_url=`` to
   ``FastMCP()``. FastMCP reads both off the server instance at render time
   (``oauth_proxy/consent.py``), so the logo and the server-name hyperlink come
   from here with no patching.

2. **Theme overlay.** ``install_oauth_page_branding()`` wraps FastMCP's HTML
   builders and appends :data:`_THEME_CSS` to the ``<head>`` of what they
   return, overriding colors and fonts on FastMCP's own markup. FastMCP keeps
   ownership of the form fields, CSRF token, and cookie handling — this only
   restyles the result.

The overlay is deliberately additive. If FastMCP restructures its markup the
overrides that no longer match simply stop applying and the page falls back to
FastMCP's styling; nothing breaks functionally. ``tests/test_branding.py``
asserts the selectors and copy substitutions still bite, so a FastMCP upgrade
that moves them fails loudly instead of silently reverting the branding.

Palette and font stack are taken from the Kubecost demo UI's stylesheet
(``demo.kubecost.xyz``), where they are exposed as CSS custom properties.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from mcp.types import Icon

# --- Palette -----------------------------------------------------------------
# Values mirror the Kubecost UI's own design tokens. Names describe the role the
# token plays there, not the literal color, so a rebrand is a one-line change.

INK = "#023927"  # primary dark green — body text, headings
INK_SOFT = "#356152"  # secondary dark green
MUTED = "#607971"  # muted label text (4.7:1 on white — WCAG AA)
ACCENT = "#31c46c"  # primary action green — button fill, not text (2.3:1 alone)
ACCENT_BRIGHT = "#63e892"  # highlight green — logo, and links on INK (8.4:1)
ACCENT_TEXT = "#0f7a4a"  # accent darkened for text on white (5.4:1 — WCAG AA)
CANVAS = "#f7faf8"  # page background
SURFACE = "#ffffff"  # card background
SURFACE_SUNK = "#f5f7f6"  # inset panel background
BORDER = "#e6ebe9"  # hairline border
BORDER_STRONG = "#bfcdc9"  # emphasized border
SUCCESS_BG = "#eaf9f0"  # green-tinted informational background
WARNING = "#ffb30f"
WARNING_BG = "#fff7e7"
DANGER = "#f21b3f"
DANGER_BG = "#fee8ec"

# The demo UI's --base-font-family. Space Grotesk is not fetched: the consent
# page's CSP allows no font-src, and an external webfont request would fail in
# air-gapped clusters anyway. Browsers that have the family installed use it;
# everything else lands on the same system stack the Kubecost UI falls back to.
FONT_STACK = (
    '"Space Grotesk Var","Space Grotesk",ui-sans-serif,system-ui,-apple-system,'
    'BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans",sans-serif'
)
MONO_FONT_STACK = 'ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace'

KUBECOST_WEBSITE_URL = "https://www.kubecost.com"
_AUTH_DOCS_URL = "https://github.com/kubecost/mcp-kubecost/blob/main/docs/auth/README.md"

# --- Logo --------------------------------------------------------------------
# The Kubecost mark as a PNG, inlined as a base64 data: URI so the page pulls no
# external asset — the consent CSP permits `img-src data:`, and a self-contained
# logo needs no reverse-proxy rule and works with no egress.
#
# Two named files in assets/ let a future rebrand swap one or both PNGs without
# any code change. The light variant goes first — FastMCP renders `icons[0]` on
# its OAuth pages, which are light-background.

_ASSETS = Path(__file__).parent / "assets"
_png_light = (_ASSETS / "kubecost-logo-light.png").read_bytes()
_png_dark = (_ASSETS / "kubecost-logo-dark.png").read_bytes()

KUBECOST_LOGO_DATA_URI = "data:image/png;base64," + base64.b64encode(_png_light).decode()
KUBECOST_LOGO_DARK_DATA_URI = "data:image/png;base64," + base64.b64encode(_png_dark).decode()

# Served verbatim at /favicon.ico. Bytes, not a data URI — this one goes over the
# wire as a response body.
FAVICON_PNG = _png_light
FAVICON_MEDIA_TYPE = "image/png"


def server_icons() -> list[Icon]:
    """Return the server icon list for ``FastMCP(icons=...)``.

    Two uses, one list. FastMCP renders ``icons[0].src`` as the logo on the OAuth
    pages, and the whole list is advertised to MCP clients in ``serverInfo`` —
    which is the only icon mechanism MCP has. ``/favicon.ico`` is a browser
    convention and is invisible to MCP clients.

    ``sizes=["128x128"]`` reflects the concrete pixel dimensions of the PNG.
    ``theme`` lets a client pick the variant that stays visible against its own
    light or dark chrome; it rides along as an extra field on ``Icon``.
    """
    return [
        Icon(src=KUBECOST_LOGO_DATA_URI, mimeType="image/png", sizes=["128x128"], theme="light"),
        Icon(src=KUBECOST_LOGO_DARK_DATA_URI, mimeType="image/png", sizes=["128x128"], theme="dark"),
    ]


# --- Theme overlay -----------------------------------------------------------
# Overrides FastMCP's BASE_STYLES and the per-page style blocks it composes
# (INFO_BOX_STYLES, BUTTON_STYLES, DETAIL_BOX_STYLES, REDIRECT_SECTION_STYLES,
# DETAILS_STYLES, TOOLTIP_STYLES, STATUS_MESSAGE_STYLES). Appended after them,
# so plain-specificity selectors win on cascade order.
_THEME_CSS = f"""
    body {{
        font-family: {FONT_STACK};
        background: {CANVAS};
        color: {INK};
    }}

    .container {{
        background: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: 0.75rem;
        box-shadow: 0 1px 2px 0 rgba(0, 0, 0, .08), 0 2px 5px 0 rgba(2, 57, 39, .06);
    }}

    h1 {{
        color: {INK};
        letter-spacing: -0.01em;
    }}

    .logo {{
        width: 56px;
    }}

    /* Informational panels */
    .info-box {{
        background: {SUCCESS_BG};
        border-color: {ACCENT_BRIGHT};
        color: {INK_SOFT};
    }}

    .info-box strong {{
        color: {INK};
    }}

    .info-box .server-name-link {{
        color: {ACCENT_TEXT};
    }}

    .info-box-mono {{
        background: {SURFACE_SUNK};
        border-color: {BORDER};
        color: {MUTED};
        font-family: {MONO_FONT_STACK};
    }}

    .info-box-mono strong {{
        color: {INK};
    }}

    .info-box.error, .info-box-mono.error {{
        background: {DANGER_BG};
        border-color: {DANGER};
        color: {INK};
    }}

    .info-box.error strong {{
        color: {DANGER};
    }}

    .info-box.warning {{
        background: {WARNING_BG};
        border-color: {WARNING};
    }}

    .info-box.warning strong {{
        color: {INK};
    }}

    .info-box code {{
        font-family: {MONO_FONT_STACK};
        background: {SURFACE_SUNK};
        color: {INK};
    }}

    .warning-box {{
        background: {WARNING_BG};
        border-color: {WARNING};
    }}

    .warning-box p {{
        color: {INK_SOFT};
    }}

    .warning-box strong {{
        color: {INK};
    }}

    .warning-box a, .warning-box a:hover {{
        color: {ACCENT_TEXT};
    }}

    /* "Credentials will be sent to" — stays a warning, in Kubecost's amber */
    .redirect-section {{
        background: {WARNING_BG};
        border-color: {WARNING};
    }}

    .redirect-section .label {{
        color: {INK_SOFT};
    }}

    .redirect-section .value {{
        color: {INK};
        font-family: {MONO_FONT_STACK};
    }}

    /* Advanced details */
    summary {{
        color: {MUTED};
    }}

    summary:hover {{
        background: {SURFACE_SUNK};
        color: {INK};
    }}

    .detail-box {{
        background: {SURFACE_SUNK};
        border-color: {BORDER};
    }}

    .detail-row {{
        border-bottom-color: {BORDER};
    }}

    .detail-label {{
        color: {MUTED};
    }}

    .detail-value {{
        color: {INK};
        font-family: {MONO_FONT_STACK};
    }}

    /* Buttons — bright green on dark green ink is the Kubecost primary (5.7:1) */
    .btn-approve, .btn-primary {{
        background: {ACCENT};
        color: {INK};
        font-weight: 600;
    }}

    .btn-deny, .btn-secondary {{
        background: {SURFACE};
        color: {INK};
        border: 1px solid {BORDER_STRONG};
        font-weight: 600;
    }}

    button:hover {{
        box-shadow: 0 4px 6px -1px rgba(2, 57, 39, .18);
    }}

    /* CIMD verified-domain badge */
    .cimd-badge {{
        background: {SUCCESS_BG};
        border-color: {ACCENT_BRIGHT};
        color: {INK};
    }}

    .cimd-check {{
        color: {ACCENT_TEXT};
    }}

    /* Status message icons */
    .status-message .message {{
        color: {INK};
    }}

    .status-icon.success {{
        background: {SUCCESS_BG};
        color: {ACCENT_TEXT};
    }}

    .status-icon.error {{
        background: {DANGER_BG};
        color: {DANGER};
    }}

    /* Footer help link and its tooltip */
    .close-instruction, .help-text, .help-link {{
        color: {MUTED};
    }}

    .help-link {{
        border-bottom-color: {BORDER_STRONG};
    }}

    .help-link:hover {{
        color: {INK};
        border-bottom-color: {INK};
    }}

    .tooltip {{
        background: {INK};
    }}

    .tooltip::after {{
        border-top-color: {INK};
    }}

    .tooltip-link {{
        color: {ACCENT_BRIGHT};
    }}
"""

# Copy that names FastMCP to the end user. Each pattern is a single-line
# substring of FastMCP's own template — nothing here spans a line break, which
# is what keeps the substitution robust against reindentation.
_COPY_SUBSTITUTIONS: tuple[tuple[str, str], ...] = (
    ("This FastMCP server", "This Kubecost MCP server"),
    ("FastMCP security &rarr;", "MCP security &rarr;"),
    ("FastMCP security →", "MCP security →"),
    ("https://gofastmcp.com/servers/auth/oauth-proxy#confused-deputy-attacks", _AUTH_DOCS_URL),
)

_HEAD_CLOSE = re.compile(r"</head>", re.IGNORECASE)

# FastMCP's create_page() declares no icon, so a browser falls back to requesting
# /favicon.ico at the origin root — a 404 in the access log on every fresh visit.
# Declaring the icon inline suppresses that request entirely rather than serving
# it, which also means it works on a shared Kubecost hostname, where the root
# /favicon.ico belongs to the Kubecost frontend and never reaches this server.
# `img-src data:` is already in the consent CSP, so this needs no CSP change.
_FAVICON_LINK = f'<link rel="icon" href="{KUBECOST_LOGO_DATA_URI}">'

# Marker set on our wrappers so install_oauth_page_branding() can tell a builder
# it already branded from one wrapped by FastMCP or another library — both of
# which set the generic ``__wrapped__``, but only ours sets this.
_KUBECOST_BRANDED = "_kubecost_branded"


def apply_kubecost_branding(html: str) -> str:
    """Return ``html`` restyled as Kubecost.

    Adds the theme stylesheet and an inline favicon to ``<head>``, and rewrites
    the few strings in FastMCP's copy that name FastMCP to the reader. Returns
    the input unchanged if there is no ``<head>`` to extend — FastMCP emits a few
    bare HTML fragments, which is what the ``/favicon.ico`` route covers.
    """
    if not _HEAD_CLOSE.search(html):
        return html  # bare fragment: nothing to restyle, leave it verbatim
    for old, new in _COPY_SUBSTITUTIONS:
        html = html.replace(old, new)
    # The <style> goes in as its own block after FastMCP's, so equal-specificity
    # rules win on cascade order. `style-src 'unsafe-inline'` is already allowed.
    return _HEAD_CLOSE.sub(f"{_FAVICON_LINK}<style>{_THEME_CSS}</style></head>", html, count=1)


def _brand_html_builder(builder: Callable[..., str]) -> Callable[..., str]:
    """Wrap a FastMCP page builder so its HTML comes back branded.

    ``*args``/``**kwargs`` are passed straight through and never inspected, so
    the wrapper survives signature changes in FastMCP's builders. ``wraps`` sets
    ``__wrapped__``; the Kubecost-specific ``_KUBECOST_BRANDED`` sentinel is what
    :func:`install_oauth_page_branding` checks, so a builder wrapped by FastMCP
    or another library (which would also set ``__wrapped__``) is still branded
    rather than mistaken for one of ours.
    """

    @wraps(builder)
    def branded(*args: Any, **kwargs: Any) -> str:
        return apply_kubecost_branding(builder(*args, **kwargs))

    setattr(branded, _KUBECOST_BRANDED, True)
    return branded


def install_oauth_page_branding() -> None:
    """Restyle FastMCP's browser-facing OAuth pages as Kubecost.

    FastMCP exposes no theming hook: ``fastmcp.utilities.ui`` composes its
    palette into a ``<style>`` block at render time, and the only documented
    customization is the server name, icon, and website URL (all supplied via
    ``FastMCP(icons=..., website_url=...)``). The remaining option short of
    reimplementing FastMCP's CSRF and cookie handling is to rebind the page
    builders in the modules that call them.

    Each builder is imported by name into its calling module, so the binding has
    to be replaced there rather than on ``fastmcp.utilities.ui``:

    - ``oauth_proxy.consent.create_consent_html`` — the consent screen
    - ``oauth_proxy.proxy.create_error_html`` — OAuth error pages
    - ``handlers.authorize.create_unregistered_client_html`` — unregistered-client page

    Idempotent, and a builder FastMCP has renamed is skipped with a warning
    rather than raising: unbranded pages are a cosmetic regression, not a
    reason to fail startup.
    """
    import logging

    from fastmcp.server.auth.handlers import authorize as _authorize
    from fastmcp.server.auth.oauth_proxy import consent as _consent, proxy as _proxy

    logger = logging.getLogger(__name__)

    targets = (
        (_consent, "create_consent_html"),
        (_proxy, "create_error_html"),
        (_authorize, "create_unregistered_client_html"),
    )

    for module, name in targets:
        builder = getattr(module, name, None)
        if builder is None:
            logger.warning(
                "Skipping Kubecost branding for %s.%s — not found in FastMCP. "
                "OAuth pages will use FastMCP's default styling.",
                module.__name__,
                name,
            )
            continue
        if getattr(builder, _KUBECOST_BRANDED, False):
            continue  # already branded by us (a foreign wrapper is re-branded)
        setattr(module, name, _brand_html_builder(builder))
