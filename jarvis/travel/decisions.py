"""Deterministic multi-criteria scoring for v7.4 decisions (no model).

Method:
* Each criterion is min-max scaled across the options that have a value for it: ``higher_better`` maps the
  lowest value to 0 and the highest to 1, ``lower_better`` the reverse. When every known value is equal the
  criterion does not separate the options and each of them scores 1.0 on it ("constant").
* An option's score is the weighted mean of its known scaled values, times 100. Unknown values are left out
  and the option's remaining weights re-scaled to sum to 1 (no penalty, no bonus), and this is reported.
* Ties (scores within 1e-9) share a rank.
* Sensitivity: for each criterion, holding the other weights fixed, the weight at which another option's score
  equals the winner's is a root of a quadratic in that weight (each score is a ratio of two linear functions of
  it). Candidate roots in [0, 100] are checked by re-scoring just past them, and the nearest real flip below and
  above the current weight is reported.
"""

import math
from typing import Sequence

from jarvis.travel.models import (
    Criterion, CriterionScore, Decision, DecisionOption, DecisionResult, OptionResult, SensitivityItem,
)

EPS = 1e-9
STEP = 1e-6


def _fmt(number: float) -> str:
    return f"{round(number, 2):g}"


def _normalize(options: Sequence[DecisionOption], criteria: Sequence[Criterion]):
    """Return ({(option_index, criterion_index): scaled}, constant names, names with no value at all)."""
    scaled: dict[tuple[int, int], float] = {}
    constant: list[str] = []
    missing: list[str] = []
    for ci, criterion in enumerate(criteria):
        known = [(oi, option.value(criterion.name)) for oi, option in enumerate(options)
                 if option.value(criterion.name) is not None]
        if not known:
            missing.append(criterion.name)
            continue
        low, high = min(value for _, value in known), max(value for _, value in known)
        if high == low:
            constant.append(criterion.name)
            for oi, _ in known:
                scaled[(oi, ci)] = 1.0
            continue
        for oi, value in known:
            fraction = (value - low) / (high - low)
            scaled[(oi, ci)] = fraction if criterion.direction == "higher_better" else 1.0 - fraction
    return scaled, constant, missing


def _scores(n_options: int, weights: Sequence[float], scaled: dict[tuple[int, int], float]) -> list[float | None]:
    result: list[float | None] = []
    for oi in range(n_options):
        numerator = denominator = 0.0
        for ci, weight in enumerate(weights):
            if weight > 0 and (oi, ci) in scaled:
                numerator += weight * scaled[(oi, ci)]
                denominator += weight
        result.append(numerator / denominator if denominator > 0 else None)
    return result


def _winners(scores: Sequence[float | None]) -> list[int]:
    known = [score for score in scores if score is not None]
    if not known:
        return []
    top = max(known)
    return [index for index, score in enumerate(scores) if score is not None and top - score <= EPS]


def _roots(c2: float, c1: float, c0: float) -> list[float]:
    scale = max(abs(c2), abs(c1), abs(c0), 1e-300)
    if abs(c2) <= 1e-12 * scale:
        if abs(c1) <= 1e-12 * scale:
            return []
        return [-c0 / c1]
    disc = c1 * c1 - 4 * c2 * c0
    if disc < 0:
        if disc > -1e-12 * scale * scale:
            disc = 0.0
        else:
            return []
    root = math.sqrt(disc)
    return [(-c1 - root) / (2 * c2), (-c1 + root) / (2 * c2)]


def _sensitivity(names: list[str], criteria: Sequence[Criterion], scaled, winner: int) -> list[SensitivityItem]:
    weights = [criterion.weight for criterion in criteria]
    n = len(names)
    items: list[SensitivityItem] = []
    for ci, criterion in enumerate(criteria):
        current = criterion.weight

        def winners_at(weight: float) -> list[int]:
            trial = list(weights)
            trial[ci] = weight
            return _winners(_scores(n, trial, scaled))

        def parts(oi: int) -> tuple[float, float, float, float]:
            a = sum(weights[j] * scaled[(oi, j)] for j in range(len(criteria))
                    if j != ci and weights[j] > 0 and (oi, j) in scaled)
            b = sum(weights[j] for j in range(len(criteria)) if j != ci and weights[j] > 0 and (oi, j) in scaled)
            known = (oi, ci) in scaled
            return a, b, scaled[(oi, ci)] if known else 0.0, 1.0 if known else 0.0

        a_w, b_w, p_w, q_w = parts(winner)
        candidates: set[float] = set()
        for oi in range(n):
            if oi == winner:
                continue
            a_o, b_o, p_o, q_o = parts(oi)
            # (a_o + p_o w)(b_w + q_w w) - (a_w + p_w w)(b_o + q_o w) = 0
            c2 = p_o * q_w - p_w * q_o
            c1 = a_o * q_w + p_o * b_w - a_w * q_o - p_w * b_o
            c0 = a_o * b_w - a_w * b_o
            candidates.update(root for root in _roots(c2, c1, c0) if -EPS <= root <= 100 + EPS)
        lower = lower_new = upper = upper_new = None
        for root in sorted((r for r in candidates if r < current - EPS), reverse=True):
            probe = root - STEP
            if probe < 0:
                continue
            found = winners_at(probe)
            if found != [winner]:
                lower, lower_new = max(root, 0.0), found
                break
        if lower is None and current > 0:
            found = winners_at(0.0)
            if found != [winner]:
                lower, lower_new = 0.0, found
        for root in sorted(r for r in candidates if r > current + EPS):
            probe = root + STEP
            if probe > 100:
                continue
            found = winners_at(probe)
            if found != [winner]:
                upper, upper_new = min(root, 100.0), found
                break
        changes = [value - current for value in (lower, upper) if value is not None]
        smallest = min(changes, key=abs) if changes else None
        lower_names = [names[i] for i in lower_new] if lower_new is not None else None
        upper_names = [names[i] for i in upper_new] if upper_new is not None else None
        statement = _statement(names[winner], criterion.name, current, lower, lower_names, upper, upper_names)
        items.append(SensitivityItem(
            criterion=criterion.name, weight=current,
            lower_threshold=round(lower, 2) if lower is not None else None, lower_new_winner=lower_names,
            upper_threshold=round(upper, 2) if upper is not None else None, upper_new_winner=upper_names,
            smallest_change=round(smallest, 2) if smallest is not None else None, statement=statement))
    return items


def _who(names: list[str] | None) -> str:
    if not names:
        return "no option"
    if len(names) == 1:
        return f"'{names[0]}'"
    return "a tie between " + ", ".join(f"'{name}'" for name in names)


def _statement(winner: str, criterion: str, current: float, lower, lower_names, upper, upper_names) -> str:
    parts = []
    if lower is not None:
        if lower <= 0:
            parts.append(f"the winner changes from '{winner}' to {_who(lower_names)} if the '{criterion}' weight "
                         f"is set to 0 (now {_fmt(current)})")
        else:
            parts.append(f"the winner changes from '{winner}' to {_who(lower_names)} if the '{criterion}' weight "
                         f"drops below {_fmt(lower)} (now {_fmt(current)})")
    if upper is not None:
        parts.append(f"the winner changes from '{winner}' to {_who(upper_names)} if the '{criterion}' weight "
                     f"rises above {_fmt(upper)} (now {_fmt(current)})")
    if not parts:
        return f"No weight for '{criterion}' between 0 and 100 changes the winner (now {_fmt(current)})."
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


def evaluate_decision(decision: Decision) -> DecisionResult:
    options, criteria = decision.options, decision.criteria
    names = [option.name for option in options]
    weights = [criterion.weight for criterion in criteria]
    scaled, constant, missing = _normalize(options, criteria)
    raw = _scores(len(options), weights, scaled)
    winners = _winners(raw)
    ranking: list[OptionResult] = []
    for oi, option in enumerate(options):
        denominator = sum(weights[ci] for ci in range(len(criteria)) if weights[ci] > 0 and (oi, ci) in scaled)
        breakdown = []
        for ci, criterion in enumerate(criteria):
            norm = scaled.get((oi, ci))
            share = weights[ci] / denominator if norm is not None and weights[ci] > 0 and denominator > 0 else None
            breakdown.append(CriterionScore(
                criterion=criterion.name, value=option.value(criterion.name),
                normalized=round(norm, 6) if norm is not None else None,
                weight_share=round(share, 6) if share is not None else None,
                contribution=round(share * norm * 100, 4) if share is not None and norm is not None else None))
        score = raw[oi]
        rank = (1 + sum(1 for other in raw if other is not None and other - score > EPS)) if score is not None else None
        ranking.append(OptionResult(
            option=option.name, rank=rank, score=round(score * 100, 2) if score is not None else None,
            tied=score is not None and sum(1 for other in raw if other is not None and abs(other - score) <= EPS) > 1,
            unknown_criteria=[criterion.name for criterion in criteria if option.value(criterion.name) is None],
            breakdown=breakdown))
    order = sorted(range(len(options)), key=lambda i: (raw[i] is None, -(raw[i] or 0.0), i))
    ranking = [ranking[i] for i in order]
    unused = sorted(set(missing) | {criterion.name for criterion in criteria if criterion.weight == 0},
                    key=[c.name for c in criteria].index)
    sensitivity = _sensitivity(names, criteria, scaled, winners[0]) if len(winners) == 1 else []
    explanation = _explain(decision, raw, winners, order, scaled, constant, missing, sensitivity)
    return DecisionResult(decision_id=decision.id, question=decision.question, ranking=ranking,
                          winners=[names[i] for i in winners], tie=len(winners) > 1, constant_criteria=constant,
                          unused_criteria=unused, sensitivity=sensitivity, explanation=explanation)


def _value_text(value: float | None, unit: str | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:g}" + (f" {unit}" if unit else "")


def _explain(decision: Decision, raw, winners, order, scaled, constant, missing, sensitivity) -> str:
    options, criteria = decision.options, decision.criteria
    weights = [criterion.weight for criterion in criteria]
    sentences: list[str] = []
    if not winners:
        return "No option has a value for any criterion with a weight above 0, so nothing can be ranked."
    if len(winners) > 1:
        tied = ", ".join(f"'{options[i].name}'" for i in winners)
        sentences.append(f"{tied} are tied at {raw[winners[0]] * 100:.1f} of 100, so there is no single winner; "
                         "change a weight or add a criterion to separate them.")
    else:
        first = winners[0]
        sentences.append(f"'{options[first].name}' ranks first with {raw[first] * 100:.1f} of 100.")
        runner = next((i for i in order if i != first and raw[i] is not None), None)
        if runner is not None:
            gap = (raw[first] - raw[runner]) * 100
            sentences.append(f"'{options[runner].name}' is next with {raw[runner] * 100:.1f} ({gap:.1f} lower).")

            def contribution(oi: int, ci: int) -> float:
                denominator = sum(weights[j] for j in range(len(criteria)) if weights[j] > 0 and (oi, j) in scaled)
                if weights[ci] <= 0 or (oi, ci) not in scaled or denominator <= 0:
                    return 0.0
                return weights[ci] / denominator * scaled[(oi, ci)] * 100

            diffs = [(contribution(first, ci) - contribution(runner, ci), ci) for ci in range(len(criteria))]
            best = max(diffs)
            if best[0] > EPS:
                criterion = criteria[best[1]]
                sentences.append(
                    f"The biggest difference is '{criterion.name}' ({options[first].name}: "
                    f"{_value_text(options[first].value(criterion.name), criterion.unit)}, {options[runner].name}: "
                    f"{_value_text(options[runner].value(criterion.name), criterion.unit)}; "
                    f"{'higher' if criterion.direction == 'higher_better' else 'lower'} is better).")
            worst = min(diffs)
            if worst[0] < -EPS:
                criterion = criteria[worst[1]]
                sentences.append(f"'{options[runner].name}' does better on '{criterion.name}' "
                                 f"({_value_text(options[runner].value(criterion.name), criterion.unit)} vs "
                                 f"{_value_text(options[first].value(criterion.name), criterion.unit)}).")
    for name in constant:
        sentences.append(f"'{name}' is the same for every option that has a value, so it does not separate them.")
    for name in missing:
        sentences.append(f"No option has a value for '{name}', so it was left out.")
    for option in options:
        unknown = [c.name for c in criteria if option.value(c.name) is None and c.name not in missing]
        if unknown:
            listed = ", ".join(f"'{name}'" for name in unknown)
            sentences.append(f"'{option.name}' has no value for {listed}; its score uses its other criteria only "
                             "(weights re-scaled).")
    flips = [item for item in sensitivity if item.smallest_change is not None]
    if flips:
        nearest = min(flips, key=lambda item: abs(item.smallest_change))
        sentences.append("Most sensitive: " + nearest.statement[0].lower() + nearest.statement[1:])
    elif sensitivity:
        sentences.append("No single weight change between 0 and 100 changes the winner.")
    sentences.append("Scores compare these options only; they are not absolute ratings.")
    return " ".join(sentences)
