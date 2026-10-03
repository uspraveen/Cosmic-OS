"""The OAuth result page a provider redirects the user's browser to.

One design for every integration, matching the Cosmic sign-in confirmation
page: white background, the COSMIC wordmark, the provider's real logo. These
tests pin the renderer's contract: the design carries over untouched, each
provider gets its own mark, and account display names — which come from
provider payloads and land in the HTML — are escaped.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.credentials.routes import (  # noqa: E402
    _GITHUB_MARK_SVG,
    _GOOGLE_MARK_SVG,
    _NOTION_MARK_SVG,
    _oauth_result_page,
)


class TestResultPageDesign:
    def test_it_matches_the_signin_page_language(self) -> None:
        page = _oauth_result_page("Notion Connected", "Penn is now available in COSMIC.")
        assert 'class="cosmic-brand"' in page
        assert "COSMIC</p>" in page
        assert "background: #fff" in page
        assert "Bahnschrift" in page  # the Spaces header wordmark face

    def test_each_provider_carries_its_own_mark(self) -> None:
        google = _oauth_result_page("Google Connected", "…", logo_svg=_GOOGLE_MARK_SVG)
        github = _oauth_result_page("GitHub Connected", "…", logo_svg=_GITHUB_MARK_SVG)
        notion = _oauth_result_page("Notion Connected", "…", logo_svg=_NOTION_MARK_SVG)
        assert 'fill="#4285F4"' in google
        assert "#181717" in github
        assert "4.459 4.208" in notion  # the simple-icons Notion geometry

    def test_a_page_without_a_logo_still_renders(self) -> None:
        page = _oauth_result_page("Connected", "…")
        assert '<div class="oauth-provider-mark"' not in page
        assert "cosmic-brand" in page

    def test_account_names_are_escaped(self) -> None:
        """Display names come from provider payloads and are interpolated into
        the page; a workspace named like a tag must not become markup."""
        page = _oauth_result_page(
            "Notion Connected",
            '<script>alert("x")</script> is now available in COSMIC.',
        )
        assert "<script>" not in page
        assert "&lt;script&gt;" in page

    def test_the_badge_reflects_the_outcome(self) -> None:
        connected = _oauth_result_page("GitHub Connected", "…")
        updated = _oauth_result_page("Repositories Updated", "…", badge="Updated")
        assert ">Connected<" in connected
        assert ">Updated<" in updated
