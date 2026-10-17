"""Deterministic capability matching: task description -> required capabilities.

This is a transparent keyword classifier, not a model call. Callers that know
their requirements should construct ``TaskRequirements`` directly; inference is
a convenience whose matched signals are returned for inspection.
"""

import re

from jarvis.routing.models import Modality, PrivacyRequirement, TaskRequirements, TaskType

_SIGNALS: tuple[tuple[TaskType, tuple[str, ...]], ...] = (
    ("vision", ("image", "screenshot", "diagram", "photo", "picture", "chart image")),
    ("coding", ("code", "function", "bug", "debug", "stack trace", "traceback", "refactor", "compile",
                "python", "typescript", "javascript", "unit test", "repository", "exception")),
    ("structured_extraction", ("extract", "json", "fields", "parse into", "structured")),
    ("classification", ("classify", "categorize", "label", "which category", "sentiment")),
    ("long_context_summarization", ("entire document", "whole document", "long document", "all chapters",
                                    "full transcript")),
    ("summarization", ("summarize", "summary", "tl;dr", "key points")),
    ("multilingual", ("translate", "translation", "in spanish", "in french", "in german", "in hindi",
                      "in telugu", "in japanese")),
    ("reasoning", ("compare", "trade-off", "tradeoff", "analyze", "plan", "why", "prove", "reason")),
)
_LONG_CONTEXT_TOKENS = 32_000


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (about 3 characters per token, rounded up)."""
    return max(1, -(-len(text) // 3))


def infer_requirements(description: str, *, modalities: tuple[Modality, ...] = ("text",),
                       estimated_input_tokens: int | None = None, needs_structured_output: bool = False,
                       privacy: PrivacyRequirement = "any") -> tuple[TaskRequirements, tuple[str, ...]]:
    text = " " + re.sub(r"\s+", " ", description.lower()) + " "
    matched: list[str] = []
    task: TaskType = "general"
    for candidate, words in _SIGNALS:
        hits = [word for word in words if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", text)]
        if hits:
            matched.extend(f"{candidate}:{word}" for word in hits)
            if task == "general":
                task = candidate
    if "image" in modalities and task != "vision":
        matched.append("vision:image_input")
        task = "vision" if task == "general" else task
    tokens = estimated_input_tokens if estimated_input_tokens is not None else estimate_tokens(description)
    if tokens >= _LONG_CONTEXT_TOKENS and task in {"summarization", "general"}:
        task = "long_context_summarization"
        matched.append(f"long_context:{tokens}_tokens")
    if task == "structured_extraction":
        needs_structured_output = True
    requirements = TaskRequirements(
        task_type=task, modalities=tuple(dict.fromkeys(("text", *modalities))),
        min_context_tokens=tokens if tokens >= 4_000 else None,
        needs_structured_output=needs_structured_output, privacy=privacy,
    )
    return requirements, tuple(matched)
