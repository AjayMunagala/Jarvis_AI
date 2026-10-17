"""Bounded, standard-library HTML extraction shared by every browser provider.

The extractor produces visible text, headings, forms, and interactive elements
with server-assigned ids (``el-1``…) and a structural CSS path used only by the
application to locate the element again. Values of input fields are never
captured. Hidden content (``hidden``, ``aria-hidden="true"``, inline
``display:none``/``visibility:hidden``) is excluded from text and elements.
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from jarvis.browser.redaction import is_sensitive_field, redact_text

_SKIP_TEXT_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head", "iframe", "object"})
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr",
})
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "header", "footer", "main", "nav", "aside", "li", "ul", "ol",
    "table", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "br", "form", "fieldset", "pre",
    "blockquote", "dl", "dt", "dd", "figure", "figcaption", "label",
})
_TEXT_INPUT_TYPES = frozenset({
    "", "text", "email", "search", "url", "tel", "number", "password", "date", "datetime-local",
    "month", "week", "time", "color", "range",
})
_HIDDEN_STYLE = re.compile(r"(display\s*:\s*none|visibility\s*:\s*hidden)", re.IGNORECASE)
_WS = re.compile(r"[ \t\r\f\v]+")
MAX_LABEL = 200


def _clip(value: str, limit: int = MAX_LABEL) -> str:
    value = " ".join((value or "").split())
    return value[:limit]


@dataclass
class ExtractedElement:
    element_id: str
    kind: str
    tag: str
    css_path: str
    label: str = ""
    text: str = ""
    href: str | None = None
    input_type: str | None = None
    name: str | None = None
    form_id: str | None = None
    required: bool = False
    disabled: bool = False
    sensitive: bool = False
    dom_id: str | None = None
    options: list[tuple[str, str, bool]] = field(default_factory=list)
    explicit_button_type: str | None = None


@dataclass
class ExtractedForm:
    form_id: str
    action: str | None
    method: str
    label: str = ""
    element_ids: list[str] = field(default_factory=list)


@dataclass
class Extraction:
    title: str
    text: str
    text_truncated: bool
    headings: list[str]
    elements: list[ExtractedElement]
    elements_truncated: bool
    forms: list[ExtractedForm]
    redaction_count: int
    skipped_links: int


class _Parser(HTMLParser):
    def __init__(self, base_url: str, max_elements: int):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.max_elements = max_elements
        self.stack: list[dict] = [{"tag": "#root", "counts": {}, "path": "", "hidden": False, "skip": False}]
        self.text_parts: list[str] = []
        self.title = ""
        self.headings: list[str] = []
        self.elements: list[ExtractedElement] = []
        self.elements_truncated = False
        self.forms: list[ExtractedForm] = []
        self.current_form: ExtractedForm | None = None
        self.captures: list[tuple[str, list[str], object]] = []
        self.label_for: dict[str, str] = {}
        self.open_labels: list[list[ExtractedElement]] = []
        self.current_select: ExtractedElement | None = None
        self.skipped_links = 0

    # ------------------------------------------------------------ helpers
    @property
    def hidden(self) -> bool:
        return self.stack[-1]["hidden"]

    @property
    def skip_text(self) -> bool:
        return self.stack[-1]["skip"]

    def _push(self, tag: str, attrs: dict) -> str:
        parent = self.stack[-1]
        count = parent["counts"].get(tag, 0) + 1
        parent["counts"][tag] = count
        path = f"{parent['path']} > {tag}:nth-of-type({count})" if parent["path"] else f"{tag}:nth-of-type({count})"
        hidden = parent["hidden"] or "hidden" in attrs or attrs.get("aria-hidden") == "true" or bool(
            _HIDDEN_STYLE.search(attrs.get("style") or "")
        ) or (tag == "input" and (attrs.get("type") or "").lower() == "hidden")
        # Textarea and option content are field values or choices, not page prose.
        skip = parent["skip"] or tag in _SKIP_TEXT_TAGS or tag in {"textarea", "option"}
        if tag not in _VOID_TAGS:
            self.stack.append({"tag": tag, "counts": {}, "path": path, "hidden": hidden, "skip": skip})
        return path, hidden, skip

    def _new_element(self, **kwargs) -> ExtractedElement | None:
        if len(self.elements) >= self.max_elements:
            self.elements_truncated = True
            return None
        element = ExtractedElement(element_id=f"el-{len(self.elements) + 1}", **kwargs)
        if self.current_form is not None:
            element.form_id = self.current_form.form_id
            self.current_form.element_ids.append(element.element_id)
        for label_elements in self.open_labels:
            label_elements.append(element)
        self.elements.append(element)
        return element

    def _capture(self, kind: str, target) -> None:
        self.captures.append((kind, [], target))

    # ------------------------------------------------------------- events
    def handle_starttag(self, tag, attrs_list):
        tag = tag.lower()
        attrs = {key.lower(): (value or "") for key, value in attrs_list}
        path, hidden, skip = self._push(tag, attrs)
        if tag in _BLOCK_TAGS and not skip:
            self.text_parts.append("\n")
        if tag == "title":
            self._capture("title", None)
            return
        if hidden or (skip and tag not in {"textarea", "option"}):
            if tag not in _VOID_TAGS:
                self._capture("ignore", None)
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._capture("heading", None)
        elif tag == "form":
            if len(self.forms) < 20:
                action = attrs.get("action")
                self.current_form = ExtractedForm(
                    form_id=f"form-{len(self.forms) + 1}",
                    action=urljoin(self.base_url, action) if action else self.base_url,
                    method="post" if attrs.get("method", "").lower() == "post" else "get",
                    label=_clip(attrs.get("aria-label") or attrs.get("name") or ""),
                )
                self.forms.append(self.current_form)
        elif tag == "a" and attrs.get("href") is not None:
            href = urljoin(self.base_url, attrs["href"].strip())
            if not href.lower().startswith(("http://", "https://")):
                self.skipped_links += 1
                self._capture("ignore", None)
                return
            element = self._new_element(kind="link", tag=tag, css_path=path, href=href[:2000],
                                        label=_clip(attrs.get("aria-label") or attrs.get("title") or ""),
                                        dom_id=attrs.get("id") or None)
            self._capture("element_text", element)
        elif tag == "button":
            button_type = (attrs.get("type") or "").lower() or None
            kind = "button" if button_type in {"button", "reset"} or (button_type is None and self.current_form is None) else "submit"
            element = self._new_element(kind=kind, tag=tag, css_path=path,
                                        label=_clip(attrs.get("aria-label") or attrs.get("title") or ""),
                                        disabled="disabled" in attrs, dom_id=attrs.get("id") or None,
                                        explicit_button_type=button_type, name=_clip(attrs.get("name") or "") or None)
            self._capture("element_text", element)
        elif tag == "input":
            self._input(attrs, path)
        elif tag == "textarea":
            element = self._field("textarea", tag, attrs, path, input_type="textarea")
            self._capture("ignore", element)  # textarea content is a value, never captured
        elif tag == "select":
            element = self._field("select", tag, attrs, path, input_type="select")
            self.current_select = element
        elif tag == "option" and self.current_select is not None:
            self._capture("option", (self.current_select, attrs.get("value"), "selected" in attrs))
        elif tag == "label":
            self.open_labels.append([])
            self._capture("label", attrs.get("for") or None)

    def _input(self, attrs: dict, path: str) -> None:
        input_type = (attrs.get("type") or "text").lower()
        if input_type in {"submit", "image", "button", "reset"}:
            kind = "submit" if input_type in {"submit", "image"} else "button"
            self._new_element(kind=kind, tag="input", css_path=path, input_type=input_type,
                              label=_clip(attrs.get("aria-label") or attrs.get("value") or attrs.get("title") or input_type),
                              disabled="disabled" in attrs, dom_id=attrs.get("id") or None,
                              name=_clip(attrs.get("name") or "") or None)
            return
        if input_type in {"checkbox", "radio"}:
            self._field(input_type, "input", attrs, path, input_type=input_type)
            return
        if input_type == "file":
            # File uploads are never supported; record as non-interactive "other".
            self._field("other", "input", attrs, path, input_type="file")
            return
        self._field("text_input" if input_type in _TEXT_INPUT_TYPES else "other", "input", attrs, path,
                    input_type=input_type)

    def _field(self, kind, tag, attrs, path, *, input_type):
        label = attrs.get("aria-label") or attrs.get("placeholder") or attrs.get("title") or ""
        element = self._new_element(
            kind=kind, tag=tag, css_path=path, input_type=input_type,
            name=_clip(attrs.get("name") or "") or None, label=_clip(label),
            required="required" in attrs, disabled="disabled" in attrs or "readonly" in attrs,
            dom_id=attrs.get("id") or None,
        )
        if element is not None:
            element.sensitive = input_type == "file" or is_sensitive_field(
                input_type=input_type, name=attrs.get("name"), label=label,
                autocomplete=attrs.get("autocomplete"),
            )
        return element

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        # Pop to the matching open tag (tolerates unclosed children).
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break
        else:
            return
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")
        if tag == "form":
            self.current_form = None
        if tag == "select":
            self.current_select = None
        if tag in {"title", "h1", "h2", "h3", "h4", "h5", "h6", "a", "button", "option", "label", "textarea"} and self.captures:
            kind, parts, target = self.captures.pop()
            value = _clip("".join(parts))
            if kind == "title":
                self.title = value
            elif kind == "heading" and value and len(self.headings) < 40:
                self.headings.append(value)
            elif kind == "element_text" and target is not None:
                target.text = value
                if not target.label:
                    target.label = value
            elif kind == "option":
                select, option_value, selected = target
                if select is not None and len(select.options) < 30:
                    select.options.append((_clip(option_value if option_value is not None else value), value, selected))
            elif kind == "label":
                elements = self.open_labels.pop() if self.open_labels else []
                if target:
                    self.label_for[target] = value
                for element in elements:
                    if not element.label:
                        element.label = value

    def handle_data(self, data):
        for _, parts, _ in self.captures:
            parts.append(data)
        if not self.skip_text and not self.hidden:
            self.text_parts.append(data)


def extract_page(html: str, *, base_url: str, max_text_chars: int, max_elements: int) -> Extraction:
    parser = _Parser(base_url, max_elements)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Malformed markup degrades to whatever was parsed so far.
        pass
    for element in parser.elements:
        if element.dom_id and element.dom_id in parser.label_for and not element.label:
            element.label = parser.label_for[element.dom_id]
        if element.kind in {"text_input", "textarea", "select", "checkbox", "radio"}:
            element.sensitive = element.sensitive or is_sensitive_field(
                input_type=element.input_type, name=element.name, label=element.label, autocomplete=None,
            )
    lines = [_WS.sub(" ", line).strip() for line in "".join(parser.text_parts).split("\n")]
    text = "\n".join(line for line in lines if line)
    text, redactions = redact_text(text)
    truncated = len(text) > max_text_chars
    if truncated:
        text = text[:max_text_chars]
    for element in parser.elements:
        element.label, count_a = redact_text(element.label)
        element.text, count_b = redact_text(element.text)
        redactions += count_a + count_b
    title, count = redact_text(parser.title)
    return Extraction(
        title=title, text=text, text_truncated=truncated, headings=parser.headings,
        elements=parser.elements, elements_truncated=parser.elements_truncated,
        forms=parser.forms, redaction_count=redactions + count, skipped_links=parser.skipped_links,
    )
