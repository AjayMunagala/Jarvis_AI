"""Tutor storage: in-memory (non-durable, bounded) and PostgreSQL (durable) (v6.5).

PostgreSQL queries are parameterized, owner-scoped and run under a bounded operation deadline.
A graded attempt updates the exercise's Leitner box with one ``UPDATE ... RETURNING`` that computes
the new box from the stored one, inside the same transaction as the attempt insert, so concurrent
attempts on one exercise are serialized by the row lock and no update is lost.
"""

from collections import deque
from datetime import datetime
from time import monotonic
import threading
from typing import Any, Callable, Protocol
from uuid import UUID

from jarvis.persistence.operation_timeout import (
    bounded_connect_timeout, execute_with_deadline, operation_deadline,
)
from jarvis.tutor.grading import next_box, next_review
from jarvis.tutor.models import (
    Attempt, Exercise, GoalProgress, GoalStatus, LearningGoal, SubtopicProgress,
)

RECENT_ATTEMPTS = 20
MEMORY_MAX_ATTEMPTS = 50_000


class TutorStorageError(Exception):
    """Sanitized storage failure for tutor operations."""


class TutorStore(Protocol):
    durable: bool

    def save_goal(self, goal: LearningGoal) -> None: ...
    def get_goal(self, owner_id: UUID, goal_id: UUID) -> LearningGoal | None: ...
    def list_goals(self, owner_id: UUID, *, status: GoalStatus | None = None, limit: int = 50,
                   offset: int = 0) -> tuple[list[LearningGoal], int]: ...
    def update_goal(self, goal: LearningGoal) -> bool: ...
    def delete_goal(self, owner_id: UUID, goal_id: UUID) -> bool: ...
    def count_goals(self, owner_id: UUID) -> int: ...
    def save_exercises(self, owner_id: UUID, goal_id: UUID, exercises: list[Exercise]) -> bool: ...
    def get_exercise(self, owner_id: UUID, exercise_id: UUID) -> Exercise | None: ...
    def list_exercises(self, owner_id: UUID, goal_id: UUID, *, limit: int = 50,
                       offset: int = 0) -> tuple[list[Exercise], int]: ...
    def count_exercises(self, owner_id: UUID, goal_id: UUID) -> int: ...
    def delete_exercise(self, owner_id: UUID, exercise_id: UUID) -> bool: ...
    def apply_attempt(self, attempt: Attempt, now: datetime) -> tuple[int, datetime] | None: ...
    def list_due(self, owner_id: UUID, goal_id: UUID, now: datetime, limit: int) -> list[Exercise]: ...
    def progress(self, owner_id: UUID, goal: LearningGoal, now: datetime) -> GoalProgress: ...


def _accuracy(correct: int, total: int) -> float | None:
    return round(correct / total, 4) if total else None


def _assemble_progress(goal: LearningGoal, now: datetime, exercise_rows, attempt_rows, recent: list[bool]) -> GoalProgress:
    """exercise_rows: (subtopic, exercises, mastered, due); attempt_rows: (subtopic, attempts, correct, attempted)."""
    exercises = {row[0]: row for row in exercise_rows}
    attempts = {row[0]: row for row in attempt_rows}
    names = list(goal.subtopics) + sorted((set(exercises) | set(attempts)) - set(goal.subtopics))
    per_subtopic = [
        SubtopicProgress(subtopic=name, exercises=int(exercises[name][1]) if name in exercises else 0,
                         attempts=int(attempts[name][1]) if name in attempts else 0,
                         accuracy=_accuracy(int(attempts[name][2]), int(attempts[name][1])) if name in attempts else None)
        for name in names
    ]
    return GoalProgress(
        goal_id=goal.id, as_of=now,
        exercises_total=sum(int(row[1]) for row in exercise_rows),
        exercises_attempted=sum(int(row[3]) for row in attempt_rows),
        attempts_total=sum(int(row[1]) for row in attempt_rows),
        recent_accuracy=_accuracy(sum(1 for item in recent if item), len(recent)),
        recent_attempts=len(recent),
        due_for_review=sum(int(row[3]) for row in exercise_rows),
        mastered=sum(int(row[2]) for row in exercise_rows),
        per_subtopic=per_subtopic,
    )


# ====================================================================== memory


class MemoryTutorStore:
    """Thread-safe, non-durable store. Attempts are capped; the oldest are dropped first (``evicted_attempts``)."""

    durable = False

    def __init__(self, *, max_attempts: int = MEMORY_MAX_ATTEMPTS) -> None:
        self._lock = threading.Lock()
        self._goals: dict[UUID, LearningGoal] = {}
        self._exercises: dict[UUID, Exercise] = {}
        self._attempts: deque[Attempt] = deque(maxlen=max_attempts)
        self.evicted_attempts = 0

    def save_goal(self, goal: LearningGoal) -> None:
        with self._lock:
            self._goals[goal.id] = goal

    def get_goal(self, owner_id: UUID, goal_id: UUID) -> LearningGoal | None:
        with self._lock:
            goal = self._goals.get(goal_id)
            return goal if goal is not None and goal.owner_id == owner_id else None

    def list_goals(self, owner_id, *, status=None, limit=50, offset=0):
        with self._lock:
            items = [goal for goal in self._goals.values()
                     if goal.owner_id == owner_id and (status is None or goal.status == status)]
        items.sort(key=lambda goal: (goal.created_at, str(goal.id)), reverse=True)
        return items[offset:offset + limit], len(items)

    def update_goal(self, goal: LearningGoal) -> bool:
        with self._lock:
            current = self._goals.get(goal.id)
            if current is None or current.owner_id != goal.owner_id:
                return False
            self._goals[goal.id] = goal
            return True

    def delete_goal(self, owner_id: UUID, goal_id: UUID) -> bool:
        with self._lock:
            goal = self._goals.get(goal_id)
            if goal is None or goal.owner_id != owner_id:
                return False
            del self._goals[goal_id]
            for exercise_id in [key for key, item in self._exercises.items() if item.goal_id == goal_id]:
                del self._exercises[exercise_id]
            kept = [item for item in self._attempts if item.goal_id != goal_id]
            self._attempts.clear()
            self._attempts.extend(kept)
            return True

    def count_goals(self, owner_id: UUID) -> int:
        with self._lock:
            return sum(1 for goal in self._goals.values() if goal.owner_id == owner_id)

    def save_exercises(self, owner_id: UUID, goal_id: UUID, exercises: list[Exercise]) -> bool:
        with self._lock:
            goal = self._goals.get(goal_id)
            if goal is None or goal.owner_id != owner_id:
                return False
            for exercise in exercises:
                self._exercises[exercise.id] = exercise
            return True

    def get_exercise(self, owner_id: UUID, exercise_id: UUID) -> Exercise | None:
        with self._lock:
            exercise = self._exercises.get(exercise_id)
            return exercise if exercise is not None and exercise.owner_id == owner_id else None

    def list_exercises(self, owner_id, goal_id, *, limit=50, offset=0):
        with self._lock:
            items = [item for item in self._exercises.values() if item.owner_id == owner_id and item.goal_id == goal_id]
        items.sort(key=lambda item: (item.created_at, str(item.id)))
        return items[offset:offset + limit], len(items)

    def count_exercises(self, owner_id: UUID, goal_id: UUID) -> int:
        with self._lock:
            return sum(1 for item in self._exercises.values() if item.owner_id == owner_id and item.goal_id == goal_id)

    def delete_exercise(self, owner_id: UUID, exercise_id: UUID) -> bool:
        with self._lock:
            exercise = self._exercises.get(exercise_id)
            if exercise is None or exercise.owner_id != owner_id:
                return False
            del self._exercises[exercise_id]
            kept = [item for item in self._attempts if item.exercise_id != exercise_id]
            self._attempts.clear()
            self._attempts.extend(kept)
            return True

    def apply_attempt(self, attempt: Attempt, now: datetime) -> tuple[int, datetime] | None:
        with self._lock:
            exercise = self._exercises.get(attempt.exercise_id)
            if exercise is None or exercise.owner_id != attempt.owner_id:
                return None
            box = next_box(exercise.box, attempt.correct)
            due = next_review(box, now)
            self._exercises[exercise.id] = exercise.model_copy(update={"box": box, "next_review_at": due})
            if len(self._attempts) == self._attempts.maxlen:
                self.evicted_attempts += 1
            self._attempts.append(attempt)
            return box, due

    def list_due(self, owner_id: UUID, goal_id: UUID, now: datetime, limit: int) -> list[Exercise]:
        with self._lock:
            items = [item for item in self._exercises.values()
                     if item.owner_id == owner_id and item.goal_id == goal_id and item.next_review_at <= now]
        items.sort(key=lambda item: (item.next_review_at, item.created_at, str(item.id)))
        return items[:limit]

    def progress(self, owner_id: UUID, goal: LearningGoal, now: datetime) -> GoalProgress:
        with self._lock:
            exercises = [item for item in self._exercises.values() if item.owner_id == owner_id and item.goal_id == goal.id]
            attempts = [item for item in self._attempts if item.owner_id == owner_id and item.goal_id == goal.id]
        subtopic_of = {item.id: item.subtopic for item in exercises}
        exercise_rows: dict[str, list[int]] = {}
        for item in exercises:
            row = exercise_rows.setdefault(item.subtopic, [0, 0, 0])
            row[0] += 1
            row[1] += item.box >= 4
            row[2] += item.next_review_at <= now
        attempt_rows: dict[str, list[Any]] = {}
        for item in attempts:
            name = subtopic_of.get(item.exercise_id)
            if name is None:
                continue
            row = attempt_rows.setdefault(name, [0, 0, set()])
            row[0] += 1
            row[1] += item.correct
            row[2].add(item.exercise_id)
        # Newest first; among equal timestamps the later-recorded attempt counts as newer.
        recent = [item.correct for item in sorted(reversed(attempts), key=lambda item: item.created_at,
                                                  reverse=True)[:RECENT_ATTEMPTS]]
        return _assemble_progress(
            goal, now, [(name, *values) for name, values in exercise_rows.items()],
            [(name, row[0], row[1], len(row[2])) for name, row in attempt_rows.items()], recent)


# ====================================================================== PostgreSQL

_GOAL_COLUMNS = "id, owner_id, topic, level, weekly_minutes, target_date, subtopics, status, created_at, updated_at"
_EXERCISE_COLUMNS = ("id, owner_id, goal_id, subtopic, prompt, kind, choices, answer_index, accepted_answers, "
                     "explanation, source, box, next_review_at, created_at")


def _goal(row) -> LearningGoal:
    return LearningGoal(id=row[0], owner_id=row[1], topic=row[2], level=row[3], weekly_minutes=row[4],
                        target_date=row[5], subtopics=list(row[6]), status=row[7], created_at=row[8],
                        updated_at=row[9])


def _exercise(row) -> Exercise:
    return Exercise(id=row[0], owner_id=row[1], goal_id=row[2], subtopic=row[3], prompt=row[4], kind=row[5],
                    choices=list(row[6]) if row[6] is not None else None, answer_index=row[7],
                    accepted_answers=list(row[8]) if row[8] is not None else None, explanation=row[9],
                    source=row[10], box=row[11], next_review_at=row[12], created_at=row[13])


class PostgresTutorStore:
    durable = True

    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 3, operation_timeout_seconds: int = 5,
                 connect: Callable | None = None) -> None:
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError("PostgreSQL storage requires psycopg.") from exc
            connect = psycopg.connect
        self._database_url = database_url
        self._connect = connect
        self._connect_timeout = connect_timeout_seconds
        self._operation_timeout = operation_timeout_seconds

    def _run(self, work: Callable[[Callable[..., Any]], Any], failure: str) -> Any:
        """Run ``work(execute)`` in one transaction under one operation deadline."""
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        try:
            timeout = bounded_connect_timeout(self._connect_timeout, deadline, clock=monotonic)
            with self._connect(self._database_url, connect_timeout=timeout) as conn:
                return work(lambda sql, params=(): execute_with_deadline(conn, deadline, sql, params, clock=monotonic))
        except Exception as exc:  # noqa: BLE001 - sanitized: no driver message, SQL or URL is exposed
            raise TutorStorageError(failure) from exc

    # ---------------------------------------------------------------- goals

    def save_goal(self, goal: LearningGoal) -> None:
        self._run(lambda execute: execute(
            f"INSERT INTO jarvis_app.tutor_goals ({_GOAL_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (goal.id, goal.owner_id, goal.topic, goal.level, goal.weekly_minutes, goal.target_date,
             list(goal.subtopics), goal.status, goal.created_at, goal.updated_at)),
            "Failed to store the learning goal.")

    def get_goal(self, owner_id: UUID, goal_id: UUID) -> LearningGoal | None:
        row = self._run(lambda execute: execute(
            f"SELECT {_GOAL_COLUMNS} FROM jarvis_app.tutor_goals WHERE id = %s AND owner_id = %s",
            (goal_id, owner_id)).fetchone(), "Failed to read the learning goal.")
        return _goal(row) if row else None

    def list_goals(self, owner_id, *, status=None, limit=50, offset=0):
        where, params = "owner_id = %s", [owner_id]
        if status is not None:
            where += " AND status = %s"
            params.append(status)

        def work(execute):
            total = execute(f"SELECT count(*) FROM jarvis_app.tutor_goals WHERE {where}", tuple(params)).fetchone()[0]
            rows = execute(f"SELECT {_GOAL_COLUMNS} FROM jarvis_app.tutor_goals WHERE {where} "
                           "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", tuple(params + [limit, offset])).fetchall()
            return [_goal(row) for row in rows], int(total)

        return self._run(work, "Failed to list learning goals.")

    def update_goal(self, goal: LearningGoal) -> bool:
        return self._run(lambda execute: execute(
            """UPDATE jarvis_app.tutor_goals SET topic = %s, level = %s, weekly_minutes = %s, target_date = %s,
                   subtopics = %s, status = %s, updated_at = %s WHERE id = %s AND owner_id = %s""",
            (goal.topic, goal.level, goal.weekly_minutes, goal.target_date, list(goal.subtopics), goal.status,
             goal.updated_at, goal.id, goal.owner_id)).rowcount == 1, "Failed to update the learning goal.")

    def delete_goal(self, owner_id: UUID, goal_id: UUID) -> bool:
        return self._run(lambda execute: execute(
            "DELETE FROM jarvis_app.tutor_goals WHERE id = %s AND owner_id = %s", (goal_id, owner_id)).rowcount == 1,
            "Failed to delete the learning goal.")

    def count_goals(self, owner_id: UUID) -> int:
        return int(self._run(lambda execute: execute(
            "SELECT count(*) FROM jarvis_app.tutor_goals WHERE owner_id = %s", (owner_id,)).fetchone()[0],
            "Failed to count learning goals."))

    # ---------------------------------------------------------------- exercises

    def save_exercises(self, owner_id: UUID, goal_id: UUID, exercises: list[Exercise]) -> bool:
        """Insert all exercises in one transaction, or none; False when the goal does not exist for the owner."""

        def work(execute):
            # Lock the goal row so a concurrent goal delete cannot interleave with the inserts.
            if execute("SELECT 1 FROM jarvis_app.tutor_goals WHERE id = %s AND owner_id = %s FOR SHARE",
                       (goal_id, owner_id)).fetchone() is None:
                return False
            for item in exercises:
                execute(
                    f"INSERT INTO jarvis_app.tutor_exercises ({_EXERCISE_COLUMNS}) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (item.id, item.owner_id, item.goal_id, item.subtopic, item.prompt, item.kind,
                     list(item.choices) if item.choices is not None else None, item.answer_index,
                     list(item.accepted_answers) if item.accepted_answers is not None else None,
                     item.explanation, item.source, item.box, item.next_review_at, item.created_at))
            return True

        return self._run(work, "Failed to store exercises.")

    def get_exercise(self, owner_id: UUID, exercise_id: UUID) -> Exercise | None:
        row = self._run(lambda execute: execute(
            f"SELECT {_EXERCISE_COLUMNS} FROM jarvis_app.tutor_exercises WHERE id = %s AND owner_id = %s",
            (exercise_id, owner_id)).fetchone(), "Failed to read the exercise.")
        return _exercise(row) if row else None

    def list_exercises(self, owner_id, goal_id, *, limit=50, offset=0):
        def work(execute):
            total = execute("SELECT count(*) FROM jarvis_app.tutor_exercises WHERE owner_id = %s AND goal_id = %s",
                            (owner_id, goal_id)).fetchone()[0]
            rows = execute(f"SELECT {_EXERCISE_COLUMNS} FROM jarvis_app.tutor_exercises "
                           "WHERE owner_id = %s AND goal_id = %s ORDER BY created_at, id LIMIT %s OFFSET %s",
                           (owner_id, goal_id, limit, offset)).fetchall()
            return [_exercise(row) for row in rows], int(total)

        return self._run(work, "Failed to list exercises.")

    def count_exercises(self, owner_id: UUID, goal_id: UUID) -> int:
        return int(self._run(lambda execute: execute(
            "SELECT count(*) FROM jarvis_app.tutor_exercises WHERE owner_id = %s AND goal_id = %s",
            (owner_id, goal_id)).fetchone()[0], "Failed to count exercises."))

    def delete_exercise(self, owner_id: UUID, exercise_id: UUID) -> bool:
        return self._run(lambda execute: execute(
            "DELETE FROM jarvis_app.tutor_exercises WHERE id = %s AND owner_id = %s",
            (exercise_id, owner_id)).rowcount == 1, "Failed to delete the exercise.")

    # ---------------------------------------------------------------- attempts, review, progress

    def apply_attempt(self, attempt: Attempt, now: datetime) -> tuple[int, datetime] | None:
        def work(execute):
            # The new box is computed from the stored box under the row lock; intervals: 1, 2, 4, 8, 16 days.
            row = execute(
                """UPDATE jarvis_app.tutor_exercises
                   SET box = CASE WHEN %(correct)s THEN LEAST(box + 1, 5) ELSE 1 END,
                       next_review_at = %(now)s::timestamptz + make_interval(
                           days => CASE WHEN %(correct)s THEN (1 << (LEAST(box + 1, 5) - 1)) ELSE 1 END)
                   WHERE id = %(id)s AND owner_id = %(owner)s
                   RETURNING box, next_review_at, goal_id""",
                {"correct": attempt.correct, "now": now, "id": attempt.exercise_id, "owner": attempt.owner_id},
            ).fetchone()
            if row is None:
                return None
            execute(
                """INSERT INTO jarvis_app.tutor_attempts
                       (id, owner_id, exercise_id, goal_id, given_answer, correct, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (attempt.id, attempt.owner_id, attempt.exercise_id, row[2], attempt.given_answer, attempt.correct,
                 attempt.created_at))
            return int(row[0]), row[1]

        return self._run(work, "Failed to record the attempt.")

    def list_due(self, owner_id: UUID, goal_id: UUID, now: datetime, limit: int) -> list[Exercise]:
        rows = self._run(lambda execute: execute(
            f"SELECT {_EXERCISE_COLUMNS} FROM jarvis_app.tutor_exercises "
            "WHERE owner_id = %s AND goal_id = %s AND next_review_at <= %s "
            "ORDER BY next_review_at, created_at, id LIMIT %s", (owner_id, goal_id, now, limit)).fetchall(),
            "Failed to read the review queue.")
        return [_exercise(row) for row in rows]

    def progress(self, owner_id: UUID, goal: LearningGoal, now: datetime) -> GoalProgress:
        def work(execute):
            exercise_rows = execute(
                """SELECT subtopic, count(*), count(*) FILTER (WHERE box >= 4),
                          count(*) FILTER (WHERE next_review_at <= %s)
                   FROM jarvis_app.tutor_exercises WHERE owner_id = %s AND goal_id = %s GROUP BY subtopic""",
                (now, owner_id, goal.id)).fetchall()
            attempt_rows = execute(
                """SELECT e.subtopic, count(*), count(*) FILTER (WHERE a.correct), count(DISTINCT a.exercise_id)
                   FROM jarvis_app.tutor_attempts a
                   JOIN jarvis_app.tutor_exercises e ON e.id = a.exercise_id AND e.owner_id = a.owner_id
                   WHERE a.owner_id = %s AND a.goal_id = %s GROUP BY e.subtopic""",
                (owner_id, goal.id)).fetchall()
            recent = execute(
                """SELECT correct FROM jarvis_app.tutor_attempts WHERE owner_id = %s AND goal_id = %s
                   ORDER BY created_at DESC, id DESC LIMIT %s""", (owner_id, goal.id, RECENT_ATTEMPTS)).fetchall()
            return exercise_rows, attempt_rows, [bool(row[0]) for row in recent]

        exercise_rows, attempt_rows, recent = self._run(work, "Failed to compute progress.")
        return _assemble_progress(goal, now, exercise_rows, attempt_rows, recent)
