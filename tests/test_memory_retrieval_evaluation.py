"""Controlled lexical retrieval evaluation for chat context (no database).

Corpus, queries, and relevance labels are fixed and synthetic. Results describe
only this corpus; they are not a general retrieval-quality claim.

Baseline: the pre-phase chat retrieval passed the whole chat message to
``plainto_tsquery('simple', message)``. The 'simple' configuration has no
stopwords, so every word of the message must appear in a record. That
matching rule is reproduced exactly here at the token level; baseline ranking
(``ts_rank_cd``) is approximated by matched-token count, which affects only the
order among baseline hits, not whether anything is found.

Candidate: ``extract_query_terms`` + OR-prefix candidate matching (as
``search_context`` performs in PostgreSQL) + ``rank_candidates``.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from jarvis.memory.models import MemoryRecord
from jarvis.memory.query import extract_query_terms, tokenize
from jarvis.memory.ranking import rank_candidates

K = 3
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

CORPUS = {
    "db_choice": ("memory", "We chose PostgreSQL 18 as the JARVIS persistence database."),
    "db_timeout": ("memory", "PostgreSQL statement timeouts are recalculated from the remaining request budget."),
    "frontend_stack": ("memory", "The dashboard UI is built with React and TypeScript using Vite."),
    "deploy_rule": ("memory", "Never deploy to production on Fridays."),
    "provider_choice": ("memory", "Use the mock provider offline; OpenAI is optional and needs an API key."),
    "coffee": ("memory", "The owner prefers black coffee in the morning."),
    "gpu": ("memory", "No GPU is required; JARVIS runs on a CPU-only laptop."),
    "auth_token": ("memory", "The local bearer token must be at least 32 visible ASCII characters."),
    "runbook_backup": ("document", "Backup runbook: run backup_db.bat nightly and verify the dump file size."),
    "runbook_migrations": ("document", "Migrations are applied in order by the database runner with checksum verification."),
    "doc_voice": ("document", "Voice features such as wake word and speech to text are planned for a later phase."),
    "doc_retrieval": ("document", "Memory retrieval is lexical; it does not use embeddings or vector search."),
    "doc_testing": ("document", "Run pytest from the dev virtual environment; Playwright covers browser smoke tests."),
    "noise_weather": ("memory", "The weather in Hyderabad was hot today."),
    "noise_movie": ("memory", "Watched a science fiction movie about an AI assistant."),
    "noise_database_trivia": ("document", "Trivia: the first relational database paper was published in 1970."),
}

# (query, relevant labels) — written as a user would chat, not as keywords.
QUERIES = [
    ("Which database did we choose for persistence?", {"db_choice"}),
    ("What framework is the dashboard UI built with?", {"frontend_stack"}),
    ("Can I deploy on Friday?", {"deploy_rule"}),
    ("Do I need a GPU to run JARVIS?", {"gpu"}),
    ("How long must the bearer token be?", {"auth_token"}),
    ("How do I back up the database?", {"runbook_backup"}),
    ("How are migrations verified?", {"runbook_migrations"}),
    ("Does memory retrieval use embeddings?", {"doc_retrieval"}),
    ("How do I run the tests?", {"doc_testing"}),
    ("Which provider should I use offline?", {"provider_choice"}),
    ("How are PostgreSQL timeouts calculated?", {"db_timeout"}),
    ("When is voice support planned?", {"doc_voice"}),
]


def corpus_records():
    records = {}
    for index, (label, (kind, content)) in enumerate(CORPUS.items()):
        when = BASE + timedelta(minutes=index)
        records[label] = MemoryRecord(
            id=UUID(int=index + 1), content=content, source_kind=kind,
            source_reference=f"{label}.md" if kind == "document" else None,
            created_at=when, updated_at=when,
        )
    return records


def baseline_top_k(query, records):
    words = set(tokenize(query))
    hits = []
    for label, item in records.items():
        tokens = tokenize(item.content)
        if words and words <= set(tokens):
            hits.append((-sum(tokens.count(word) for word in words), label))
    return [label for _, label in sorted(hits)][:K]


def candidate_top_k(query, records):
    terms = extract_query_terms(query)
    by_id = {item.id: label for label, item in records.items()}
    candidates = [
        item for item in records.values()
        if any(token.startswith(term) for token in tokenize(item.content) for term in terms)
    ]
    ranked = rank_candidates(terms, candidates, memory_limit=K, document_limit=K)
    # One combined top-K list across kinds, in rank order.
    return [by_id[item.record.id] for item in ranked][:K]


def metrics(results):
    hit = recall = mrr = precision = 0.0
    for top, relevant in results:
        found = [rank for rank, label in enumerate(top, start=1) if label in relevant]
        hit += 1.0 if found else 0.0
        recall += len(set(top) & relevant) / len(relevant)
        mrr += 1 / found[0] if found else 0.0
        precision += (len(set(top) & relevant) / len(top)) if top else 0.0
    n = len(results)
    return {"hit@3": hit / n, "recall@3": recall / n, "mrr@3": round(mrr / n, 4), "precision@3": round(precision / n, 4)}


def evaluate():
    records = corpus_records()
    baseline = [(baseline_top_k(query, records), relevant) for query, relevant in QUERIES]
    candidate = [(candidate_top_k(query, records), relevant) for query, relevant in QUERIES]
    return metrics(baseline), metrics(candidate), candidate


def test_chat_style_queries_mostly_fail_under_the_legacy_all_words_rule():
    baseline, _, _ = evaluate()
    records = corpus_records()
    # Function words ("which", "do", "the", ...) must all appear in a record.
    # Only one record happens to contain every word of its query
    # ("it does not use embeddings"), so 11 of 12 queries find nothing.
    found = [query for query, _ in QUERIES if baseline_top_k(query, records)]
    assert found == ["Does memory retrieval use embeddings?"]
    assert baseline == {"hit@3": 1 / 12, "recall@3": 1 / 12, "mrr@3": 0.0833, "precision@3": 0.0833}


def test_context_retrieval_finds_the_labeled_record_for_every_query():
    _, candidate, results = evaluate()
    assert candidate["hit@3"] == 1.0
    assert candidate["recall@3"] == 1.0
    assert candidate["mrr@3"] == 1.0
    # Precision is bounded by one relevant record per query with up to three
    # results; weak-match filtering keeps unrelated records out where possible.
    assert candidate["precision@3"] == 0.7917
    assert all(len(top) <= K for top, _ in results)


def test_noise_records_never_outrank_the_relevant_record():
    _, _, results = evaluate()
    for top, relevant in results:
        assert top[0] in relevant


def test_evaluation_is_deterministic():
    assert evaluate() == evaluate()
