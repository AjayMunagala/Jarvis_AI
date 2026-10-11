"""Small offline intent classifier with a replaceable interface."""

from typing import Protocol

from jarvis.orchestration.models import IntentClassification, IntentName, RequestContext


class IntentClassifier(Protocol):
    def classify(self, context: RequestContext) -> IntentClassification:
        """Classify a request without taking external actions."""


class RuleBasedIntentClassifier:
    """Deterministic keyword classifier; not a language model."""

    _rules: tuple[tuple[IntentName, tuple[str, ...]], ...] = (
        ("recommendation", ("recommend", "suggest", "what should i", "which should")),
        ("validation", ("validate", "verify", "check whether", "is this correct")),
        ("risk_assessment", ("risk", "threat", "danger", "what could go wrong")),
        ("compliance", ("compliance", "regulation", "policy requirement")),
        ("architecture", ("architecture", "design a system", "module boundary")),
        ("analysis", ("analyze", "analyse", "compare", "break down")),
    )

    def classify(self, context: RequestContext) -> IntentClassification:
        normalized = context.message.casefold()
        for intent, phrases in self._rules:
            if any(phrase in normalized for phrase in phrases):
                return IntentClassification(intent=intent, confidence=0.75)
        return IntentClassification(intent="general_inquiry", confidence=0.5, rationale_code="default")


def classify_safely(classifier: IntentClassifier, context: RequestContext) -> IntentClassification:
    try:
        return IntentClassification.model_validate(classifier.classify(context))
    except Exception:
        return IntentClassification(intent="general_inquiry", confidence=0.0, rationale_code="classifier_fallback")
