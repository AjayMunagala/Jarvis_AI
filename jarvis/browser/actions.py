"""Deterministic action classification, enforced in trusted code.

The model and the page never classify their own actions. Anything that could
submit, send, buy, delete, publish, authorize, or change an account is
``consequential``; in v2.6 consequential actions are never executed — they are
recorded as ``approval_required`` and the workflow stops before the action.
"""

import re

from jarvis.browser.extraction import ExtractedElement, ExtractedForm

_CONSEQUENTIAL_WORDS = re.compile(
    r"\b(buy|purchase|pay|payment|checkout|check out|order|place order|subscribe|unsubscribe|donate|"
    r"transfer|send|submit|post|publish|share|tweet|reply|comment|delete|remove|erase|destroy|"
    r"deactivate|close account|cancel (my )?(account|subscription|order|plan)|confirm|approve|authori[sz]e|"
    r"accept|agree|consent|sign up|register|log ?out|sign ?out|save changes|update (account|settings|"
    r"password|email|profile)|change (password|email)|reset password|upload|install|grant|allow access|"
    r"book|reserve|apply|enroll|vote)\b",
    re.IGNORECASE,
)
_CONSEQUENTIAL_HREF = re.compile(
    r"(/|=|\b)(delete|remove|destroy|logout|log-out|signout|sign-out|unsubscribe|checkout|purchase|pay|"
    r"transfer|deactivate|confirm|approve)(\b|/|\?|&|$)",
    re.IGNORECASE,
)


def _words(element: ExtractedElement) -> str:
    return " ".join(part for part in (element.label, element.text, element.name or "") if part)


def classify_click(element: ExtractedElement, form: ExtractedForm | None) -> tuple[str, str]:
    """Return (classification, reason) for clicking an observed element."""
    words = _words(element)
    if element.kind == "submit":
        return "consequential", "Clicking this control submits a form."
    if element.kind == "link":
        if _CONSEQUENTIAL_WORDS.search(words) or (element.href and _CONSEQUENTIAL_HREF.search(element.href)):
            return "consequential", "The link appears to perform a consequential operation."
        return "navigate", "Follows an observed link."
    if element.kind in {"button", "checkbox", "radio"}:
        if _CONSEQUENTIAL_WORDS.search(words):
            return "consequential", "The control's label indicates a consequential operation."
        if form is not None and form.method == "post" and element.kind == "button" and element.explicit_button_type is None:
            return "consequential", "The control may submit a POST form."
        return "interact", "Activates an observed control."
    return "blocked", "This element type cannot be clicked."


def classify(action: str, element: ExtractedElement | None, form: ExtractedForm | None) -> tuple[str, str]:
    if action in {"observe", "screenshot"}:
        return "observe", "Read-only observation."
    if action in {"navigate", "back", "reload"}:
        return "navigate", "Navigation to a policy-checked destination."
    if action == "scroll":
        return "interact", "Scrolls the viewport."
    if element is None:
        return "blocked", "An observed element is required."
    if action == "click":
        return classify_click(element, form)
    if action in {"type", "select"}:
        return "interact", "Changes a field value without submitting."
    return "blocked", "Unsupported action."
