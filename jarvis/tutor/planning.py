"""Deterministic study plans (v6.5). No model is used.

Each subtopic gets one ``learn`` and one ``practice`` session, in the goal's order, followed by one
final ``review`` session. Sessions last 30 minutes, or the weekly minutes when those are fewer.

* Without a target date, the sessions are spread over just enough weeks for the weekly minutes.
* With a target date, they are spread evenly from the start date to the target date (inclusive).
  When that period cannot hold them at the weekly minutes, the plan is still returned, compressed
  into the period, with ``feasible`` false and a note saying how many weekly minutes it really needs.
* A target date before the start date cannot be met; the plan then uses the weekly pace and says so.
"""

from datetime import date, timedelta
from math import ceil

from jarvis.tutor.models import LearningGoal, StudyPlan, StudySession

SESSION_MINUTES = 30
MAX_PLAN_SESSIONS = 200


def build_study_plan(goal: LearningGoal, start_date: date, *, max_sessions: int = MAX_PLAN_SESSIONS) -> StudyPlan:
    session_minutes = min(SESSION_MINUTES, goal.weekly_minutes)
    per_week = max(1, goal.weekly_minutes // session_minutes)
    order: list[tuple[str | None, str]] = []
    for subtopic in goal.subtopics:
        order += [(subtopic, "learn"), (subtopic, "practice")]
    order.append((None, "review"))
    required = len(order)
    weeks_at_pace = ceil(required / per_week)

    feasible, note = True, None
    target = goal.target_date
    if target is None:
        span_days = weeks_at_pace * 7
    elif target < start_date:
        span_days = weeks_at_pace * 7
        feasible = False
        note = (f"The target date {target.isoformat()} is before the start date {start_date.isoformat()}, so it "
                f"cannot be met. This plan follows your {goal.weekly_minutes} weekly minutes instead and takes "
                f"{weeks_at_pace} week(s).")
    else:
        span_days = (target - start_date).days + 1
        capacity = span_days * per_week // 7
        if capacity < required:
            feasible = False
            weeks = max(1, ceil(span_days / 7))
            needed = ceil(required * session_minutes / weeks)
            note = (f"{required} sessions of {session_minutes} minutes do not fit before {target.isoformat()} at "
                    f"{goal.weekly_minutes} minutes a week. This plan squeezes them in anyway and needs about "
                    f"{needed} minutes a week; choose a later target date or more weekly minutes to make it "
                    f"realistic.")

    sessions = [
        StudySession(date=start_date + timedelta(days=(index * span_days) // required), subtopic=subtopic,
                     minutes=session_minutes, kind=kind)
        for index, (subtopic, kind) in enumerate(order)
    ]
    truncated = len(sessions) > max_sessions
    sessions = sessions[:max(0, max_sessions)]
    plan_weeks = max(1, ceil(span_days / 7))
    return StudyPlan(
        goal_id=goal.id, start_date=start_date, end_date=sessions[-1].date if sessions else start_date,
        target_date=target, session_minutes=session_minutes, sessions_per_week=per_week,
        weekly_minutes=goal.weekly_minutes,
        weekly_minutes_needed=ceil(required * session_minutes / plan_weeks),
        total_minutes=sum(item.minutes for item in sessions), feasible=feasible, note=note,
        truncated=truncated, sessions=sessions,
    )
