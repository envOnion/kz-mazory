"""Plain-text projections for indexing and context; never rewrite audit sources."""

import re
from html import unescape
from html.parser import HTMLParser


_ENTITY = re.compile(r"&(?:#[xX][0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);")


def _decode_entities(text):
    # A bare '&not=...' in a URL is not the HTML entity '&not;'.
    return _ENTITY.sub(lambda match: unescape(match[0]), text)


class _TextParser(HTMLParser):
    hidden = {"script", "style", "head", "template", "noscript", "iframe", "object", "svg"}
    blocks = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
        "section", "table", "tr", "ul",
    }
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = []

    def handle_starttag(self, tag, attrs):
        if self.suppressed or tag in self.hidden:
            if tag not in self.void:
                self.suppressed.append(tag)
        elif tag in self.blocks:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if self.suppressed:
            if tag in self.suppressed:
                last = len(self.suppressed) - 1 - self.suppressed[::-1].index(tag)
                del self.suppressed[last:]
        elif tag in self.blocks:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def plain_text(value):
    """Remove raw/escaped HTML while preserving readable text and line breaks.

    Excessively nested encodings fail closed. Limiting the passes prevents an
    adversarial source from making decoding unbounded.
    """
    if not value:
        return ""
    text = str(value)
    for _ in range(8):
        decoded = _decode_entities(text)
        if decoded == text:
            break
        text = decoded
    else:
        if _decode_entities(text) != text:
            return ""
    parser = _TextParser()
    # Decode entities once here; protect remaining literal ampersands from
    # HTMLParser's permissive handling of names without a trailing semicolon.
    try:
        parser.feed(text.replace("&", "&amp;"))
        parser.close()
    except (AssertionError, ValueError):
        # Malformed declarations must not break a trace page or poison a queue.
        return ""
    text = "".join(parser.parts).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)
    lines = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", lines).strip()


def clean_context(messages):
    """Return copies so a read never mutates a historical trace or raw source."""
    cleaned = []
    for message in messages:
        item = dict(message)
        for key in ("content", "text", "sender_name", "author"):
            if key in item:
                item[key] = plain_text(item[key])
        if item.get("content") or item.get("text"):
            cleaned.append(item)
    return cleaned
