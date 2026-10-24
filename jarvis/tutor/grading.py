"""Deterministic grading and Leitner scheduling (v6.5). No model is used.

Short answers match when the normalized texts are equal, or when both parse as finite numbers that are
equal within a relative and absolute tolerance of 1e-9. Normalization: Unicode NFKC, case folding,
trimming, collapsing internal whitespace, and dropping trailing ``.``, ``!`` and ``?``. Meaning is not
checked: "four" and "4" do not match.
"""

from datetime import datetime, timedelta
import math
import re
import unicodedata

from jarvis.tutor.models import LEITNER_MAX_BOX, Exercise

REVIEW_INTERVAL_DAYS = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16}
_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$")


def normalize_answer(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold().strip()
    value = _WHITESPACE.sub(" ", value)
    return value.rstrip(".!? ")


def _as_number(text: str) -> float | None:
    if len(text) > 100 or not _NUMBER.match(text):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def short_answer_matches(given: str, accepted: list[str]) -> bool:
    normalized = normalize_answer(given)
    given_number = _as_number(normalized)
    for candidate in accepted:
        expected = normalize_answer(candidate)
        if normalized == expected:
            return True
        expected_number = _as_number(expected)
        if given_number is not None and expected_number is not None and math.isclose(
                given_number, expected_number, rel_tol=1e-9, abs_tol=1e-9):
            return True
    return False


def grade(exercise: Exercise, answer: int | str) -> bool:
    """Return whether ``answer`` is correct. A multiple-choice answer must be a choice index."""
    if exercise.kind == "multiple_choice":
        if not isinstance(answer, int) or isinstance(answer, bool):
            raise ValueError("A multiple-choice answer must be the index of a choice.")
        if not 0 <= answer < len(exercise.choices or []):
            raise ValueError("The answer index does not point at a choice.")
        return answer == exercise.answer_index
    if not isinstance(answer, str):
        raise ValueError("A short answer must be text.")
    return short_answer_matches(answer, exercise.accepted_answers or [])


def next_box(box: int, correct: bool) -> int:
    return min(box + 1, LEITNER_MAX_BOX) if correct else 1


def next_review(box: int, now: datetime) -> datetime:
    return now + timedelta(days=REVIEW_INTERVAL_DAYS[box])
