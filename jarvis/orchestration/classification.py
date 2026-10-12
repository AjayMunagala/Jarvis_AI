"""Small offline intent classifier with a replaceable interface."""

from typing import Protocol

from jarvis.orchestration.models import IntentClassification, RequestContext
from jarvis.orchestration.registry import IntentPhraseRule


class IntentClassifier(Protocol):
    def classify(self, context: RequestContext) -> IntentClassification:
        """Classify a request without taking external actions."""


class RuleBasedIntentClassifier:
    """Deterministic keyword classifier; not a language model."""

    def __init__(self, rules: tuple[IntentPhraseRule, ...]):
        self._rules = rules

    def classify(self, context: RequestContext) -> IntentClassification:
        normalized = context.message.casefold()
        for rule in self._rules:
            if any(phrase.casefold() in normalized for phrase in rule.phrases):
                return IntentClassification(intent=rule.intent, confidence=0.75)
        return IntentClassification(intent="general_inquiry", confidence=0.5, rationale_code="default")


def classify_safely(classifier: IntentClassifier, context: RequestContext) -> IntentClassification:
    try:
        return IntentClassification.model_validate(classifier.classify(context))
    except Exception:
        return IntentClassification(intent="general_inquiry", confidence=0.0, rationale_code="classifier_fallback")
