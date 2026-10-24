"""Task-specific reasoning strategies and answer-structure checks.

A strategy is a reusable instruction plus the sections a good answer for that
task class must contain. ``check_answer`` verifies the sections are present
(deterministically); it does not judge whether the content is correct.
"""

import re

from pydantic import BaseModel, ConfigDict

from jarvis.reasoning.models import VerifierVerdict


class Strategy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str
    instruction: str
    required_sections: tuple[tuple[str, tuple[str, ...]], ...]  # (section, accepted keywords)


STRATEGIES: dict[str, Strategy] = {item.task_class: item for item in (
    Strategy(task_class="coding_analysis",
             instruction="State the observed symptom, the evidence you used, the likely cause, a fix, and how to verify it. "
                         "Separate facts from hypotheses.",
             required_sections=(("symptom", ("symptom", "observed", "error")), ("evidence", ("evidence", "trace", "log")),
                                ("cause", ("cause", "because", "root")), ("fix", ("fix", "change", "patch")),
                                ("verification", ("verify", "test", "check")))),
    Strategy(task_class="technical_comparison",
             instruction="Name the options and the criteria, compare each option on each criterion, state trade-offs and "
                         "a conditional recommendation.",
             required_sections=(("options", ("option", "alternative", " vs ", "versus")), ("criteria", ("criteria", "criterion", "factor")),
                                ("tradeoffs", ("trade-off", "tradeoff", "downside", "cost")),
                                ("recommendation", ("recommend", "choose", "prefer")))),
    Strategy(task_class="research_synthesis",
             instruction="Summarize findings with their sources, note agreement and disagreement between sources, and "
                         "state what remains unknown.",
             required_sections=(("findings", ("finding", "found", "shows")), ("sources", ("source", "[", "according to")),
                                ("disagreement", ("disagree", "conflict", "however", "in contrast")),
                                ("unknowns", ("unknown", "unclear", "not known", "open question")))),
    Strategy(task_class="constraint_satisfaction",
             instruction="List every constraint, show how the proposal satisfies each one, and say which cannot be met.",
             required_sections=(("constraints", ("constraint", "requirement", "must")),
                                ("satisfaction", ("satisf", "meets", "complies")),
                                ("violations", ("cannot", "violat", "not met", "none violated")))),
    Strategy(task_class="decision_analysis",
             instruction="State the decision, options, criteria with weights, the evaluation, the decision and what would "
                         "change it.",
             required_sections=(("options", ("option", "alternative")), ("criteria", ("criteria", "weight")),
                                ("decision", ("decision", "recommend", "choose")),
                                ("sensitivity", ("would change", "if ", "unless", "sensitiv")))),
    Strategy(task_class="risk_analysis",
             instruction="List risks with likelihood and impact, mitigations, and residual risk.",
             required_sections=(("risks", ("risk",)), ("likelihood", ("likelihood", "probab", "likely")),
                                ("impact", ("impact", "severity", "consequence")), ("mitigation", ("mitigat", "reduce", "control")),
                                ("residual", ("residual", "remaining")))),
)}


def strategy_for(task_class: str) -> Strategy:
    try:
        return STRATEGIES[task_class]
    except KeyError as exc:
        raise KeyError(f"No reasoning strategy for {task_class!r}.") from exc


def check_answer(task_class: str, answer: str) -> VerifierVerdict:
    strategy = strategy_for(task_class)
    text = " " + re.sub(r"\s+", " ", answer.lower()) + " "
    missing = [name for name, keywords in strategy.required_sections if not any(word in text for word in keywords)]
    return VerifierVerdict(verifier_id=f"structure:{task_class}", kind="deterministic",
                           outcome="agree" if not missing else "disagree",
                           reasons=tuple(f"missing section: {name}" for name in missing)
                           or ("all required sections are present (content correctness not checked)",))
