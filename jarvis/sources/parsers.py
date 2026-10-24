"""Defused RSS 2.0 / RSS 1.0 (RDF) / Atom parsing (v7.3).

The document is first read by a streaming expat parser whose DOCTYPE, entity, external-entity and notation
handlers raise, so a DTD is refused in any encoding (no external entities, no entity expansion "billion laughs").
Only a document that passes is built into a tree with ``xml.etree``. Element names are matched by local name.

Extracted text is untrusted plain text: tags are stripped, control characters removed, whitespace collapsed,
titles capped at 300 characters and summaries at 1000. Links are kept only when they are http(s).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree
from xml.parsers import expat

MAX_ENTRIES = 200
MAX_TITLE_CHARS = 300
MAX_SUMMARY_CHARS = 1000
MAX_LINK_CHARS = 2000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class FeedParseError(Exception):
    """The document is not a feed JARVIS can read safely; the message is safe to show."""


@dataclass(frozen=True)
class FeedEntry:
    title: str
    link: str | None
    guid: str | None
    published_at: datetime | None
    summary: str | None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def plain_text(value: str | None, limit: int) -> str:
    """Strip tags and entities to text, remove control characters, collapse whitespace, cap the length."""
    if not value:
        return ""
    extractor = _TextExtractor()
    try:
        extractor.feed(value[: limit * 20])
        extractor.close()
        text = "".join(extractor.parts)
    except Exception:  # noqa: BLE001 - malformed markup: fall back to removing anything tag-like
        text = re.sub(r"<[^>]*>", " ", value)
    text = " ".join(_CONTROL.sub(" ", text).split())
    return text[:limit].rstrip()


def safe_link(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if len(value) > MAX_LINK_CHARS or any(ord(char) < 0x21 for char in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return value


def _refuse(*_args):
    raise FeedParseError("The feed declares a DTD or XML entities, which are refused for safety.")


def _precheck(raw: bytes) -> None:
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    parser.UnparsedEntityDeclHandler = _refuse
    parser.NotationDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse
    try:
        parser.Parse(raw, True)
    except expat.ExpatError:
        raise FeedParseError("The feed is not well-formed XML.") from None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child(element, *names: str):
    for child in element:
        if _local(child.tag) in names:
            return child
    return None


def _text(element) -> str:
    if element is None:
        return ""
    return "".join(element.itertext())


def _parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _atom_link(entry) -> str | None:
    fallback = None
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = safe_link(child.get("href"))
        if href is None:
            continue
        if child.get("rel", "alternate") == "alternate":
            return href
        fallback = fallback or href
    return fallback


def _entry(item, atom: bool) -> FeedEntry | None:
    title = plain_text(_text(_child(item, "title")), MAX_TITLE_CHARS)
    if atom:
        link = _atom_link(item)
        guid = _text(_child(item, "id")).strip() or None
        published = _parse_date(_text(_child(item, "published")) or _text(_child(item, "updated")))
        summary = plain_text(_text(_child(item, "summary")) or _text(_child(item, "content")), MAX_SUMMARY_CHARS)
    else:
        link = safe_link(_text(_child(item, "link")))
        guid = _text(_child(item, "guid")).strip() or None
        published = _parse_date(_text(_child(item, "pubdate")) or _text(_child(item, "date")))
        summary = plain_text(_text(_child(item, "description")) or _text(_child(item, "encoded")), MAX_SUMMARY_CHARS)
    if not title:
        title = (link or summary[:80] or "").strip()[:MAX_TITLE_CHARS]
    if not title:
        return None
    return FeedEntry(title=title, link=link, guid=guid[:500] if guid else None, published_at=published,
                     summary=summary or None)


def parse_feed(raw: bytes) -> list[FeedEntry]:
    """Entries of an RSS 2.0, RSS 1.0 or Atom document, in document order, at most ``MAX_ENTRIES``."""
    if not raw or not raw.strip():
        raise FeedParseError("The feed is empty.")
    _precheck(raw)
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise FeedParseError("The feed is not well-formed XML.") from None
    kind = _local(root.tag)
    if kind == "feed":
        items, atom = [child for child in root if _local(child.tag) == "entry"], True
    elif kind == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise FeedParseError("The RSS document has no channel.")
        items, atom = [child for child in channel if _local(child.tag) == "item"], False
    elif kind == "rdf":
        items, atom = [child for child in root if _local(child.tag) == "item"], False
    else:
        raise FeedParseError("The document is not an RSS or Atom feed.")
    entries: list[FeedEntry] = []
    for item in items[:MAX_ENTRIES]:
        entry = _entry(item, atom)
        if entry is not None:
            entries.append(entry)
    return entries
