"""The pitch outreach draft renders as a real one-click `mailto:` link when a
recipient email is present, and never emits a broken/empty mailto otherwise.

Covers both report renderers (HTML + markdown) plus the shared `_mailto_href`
encoding helper. See the action-readiness audit: the "one-click pitch" claim
was previously false because the draft only rendered as a copy-paste block.
"""

from services.audit_html_renderer import (
    _mailto_href as _html_mailto,
    _render_recommendations_html,
)
from services.audit_markdown_renderer_v2 import (
    _mailto_href as _md_mailto,
    _render_recommendations,
)


def _action_with_draft(recipient):
    return [
        {
            "title": "Pitch forbes.com editorial team",
            "pitch_draft": {
                "recipient_email": recipient,
                "subject": "Grüns vs AG1 (greens) comparison",
                # body has a ')' that must not break the markdown [..](..) link
                "body": "Hi (editorial team),\nHere is the comparison.",
            },
        }
    ]


def test_mailto_href_encodes_subject_and_body():
    href = _html_mailto(
        "vetted@forbes.com", "A vs B (greens)", "line one\nline two"
    )
    assert href.startswith("mailto:vetted@forbes.com?")
    # spaces are %20 (quote, not '+'); ')' is percent-encoded so it can't
    # terminate a surrounding markdown link
    assert "subject=A%20vs%20B%20%28greens%29" in href
    assert "body=line%20one%0Aline%20two" in href
    assert "+" not in href


def test_mailto_href_empty_recipient_returns_blank():
    assert _html_mailto("", "subj", "body") == ""
    assert _html_mailto(None, "subj", "body") == ""
    assert _md_mailto("   ", "subj", "body") == ""


def test_html_renderer_emits_mailto_link_when_recipient_present():
    html = _render_recommendations_html(_action_with_draft("vetted@forbes.com"))
    assert 'class="pitch-mailto"' in html
    assert "mailto:vetted@forbes.com?" in html
    # the visible draft is still kept alongside the link
    assert "<blockquote>" in html


def test_html_renderer_omits_mailto_when_no_recipient():
    html = _render_recommendations_html(_action_with_draft(""))
    assert "mailto:" not in html
    # but the draft text is still shown
    assert "<blockquote>" in html


def test_markdown_renderer_emits_mailto_link_when_recipient_present():
    md = _render_recommendations(_action_with_draft("vetted@forbes.com"))
    assert "](mailto:vetted@forbes.com?" in md


def test_markdown_renderer_omits_mailto_when_no_recipient():
    md = _render_recommendations(_action_with_draft(""))
    assert "mailto:" not in md
