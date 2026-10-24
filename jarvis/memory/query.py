"""Deterministic lexical query terms for chat context retrieval.

This is lexical matching only: no embeddings, synonyms, or semantic inference.
Terms are lowercased letter/digit runs with a small English stopword list and a
light suffix trim, so "memories" and "memory" share the prefix "memor".
"""

import re
import unicodedata

MAX_QUERY_TERMS = 8
MAX_TERM_LENGTH = 64
MIN_TERM_LENGTH = 2
MIN_STEM_LENGTH = 4

# Letter/digit runs only. Excluding underscores and punctuation keeps every
# term free of PostgreSQL tsquery operators.
_TOKEN = re.compile(r"[^\W_]+")
SAFE_TERM = re.compile(r"^[^\W_]{1,64}$")
_SUFFIXES = ("ies", "ing", "es", "ed", "s", "y")

STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be because been before
being below between both but by can could did do does doing done during each few for
from further had has have having he her here hers him his how i if in into is it its
just me might more most must my no nor of off on once only or other our ours out over
own please same shall she should so some such than that the their theirs them then there
these they this those through to too under until up us very was we were what when where
which while who whom whose why will with would yes you your yours tell show give know
recall remember let lets s t
""".split())


def normalize_text(value: str) -> str:
    """NFKC plus lowercase; lowercase (not casefold) mirrors PostgreSQL lower()."""
    return unicodedata.normalize("NFKC", value).lower()


def tokenize(value: str) -> list[str]:
    return _TOKEN.findall(normalize_text(value))


def stem(term: str) -> str:
    """Trim one common English suffix while keeping at least four characters."""
    if term.isdigit():
        return term
    for suffix in _SUFFIXES:
        if term.endswith(suffix) and len(term) - len(suffix) >= MIN_STEM_LENGTH:
            return term[: -len(suffix)]
    return term


def is_significant(token: str) -> bool:
    return MIN_TERM_LENGTH <= len(token) <= MAX_TERM_LENGTH and token not in STOPWORDS


def extract_query_terms(query: str) -> tuple[str, ...]:
    """Return up to eight distinct stems in first-occurrence order.

    An empty result means the request has no searchable terms; callers report
    a legitimate empty retrieval rather than querying storage.
    """
    terms: list[str] = []
    for token in tokenize(query):
        if not is_significant(token):
            continue
        term = stem(token)
        if term not in terms:
            terms.append(term)
            if len(terms) == MAX_QUERY_TERMS:
                break
    return tuple(terms)
