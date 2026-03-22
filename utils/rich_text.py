import html
import re
from typing import Any


_BLOCK_CLOSE_RE = re.compile(r"</(?:p|div|ul|ol|li|h[1-6]|section|article|blockquote)\s*>", re.IGNORECASE)
_LINE_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_LI_OPEN_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)


def rich_text_to_plain_text(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""

    text = _SCRIPT_STYLE_RE.sub(" ", text)
    text = _LINE_BREAK_RE.sub("\n", text)
    text = _BLOCK_CLOSE_RE.sub("\n", text)
    text = _LI_OPEN_RE.sub("- ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = _WHITESPACE_RE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()
