"""v6.5 live PostgreSQL validation: tutor CRUD, atomic Leitner updates, review, progress and cascade delete."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest


def _service(url):
    from jarvis.tutor.service import TutorService
    from jarvis.tutor.stores import PostgresTutorStore

    return TutorService(PostgresTutorStore(url))


def test_tutor_goals_and_exercises_round_trip(isolated_postgres):
    from jarvis.tutor.models import ExerciseCreateRequest, GoalCreateRequest, GoalUpdateRequest
    from jarvis.tutor.service import TutorNotFound

    service, owner, other = _service(isolated_postgres.database_url), uuid4(), uuid4()
    goal = service.create_goal(owner, GoalCreateRequest(topic="Spanish", level="intermediate", weekly_minutes=90,
                                                        target_date="2027-03-01", subtopics=["Verbs", "Nouns"]))
    assert service.get_goal(owner, goal.id) == goal
    with pytest.raises(TutorNotFound):
        service.get_goal(other, goal.id)
    updated = service.update_goal(owner, goal.id, GoalUpdateRequest(status="archived", target_date=None))
    assert service.get_goal(owner, goal.id) == updated and updated.target_date is None
    assert [item.id for item in service.list_goals(owner, status="archived").items] == [goal.id]
    assert service.list_goals(owner, status="active").total == 0
    mc = service.create_exercise(owner, goal.id, ExerciseCreateRequest(
        subtopic="verbs", prompt="Ser or estar?", kind="multiple_choice", choices=["ser", "estar"], answer_index=1,
        explanation="Location uses estar."))
    stored = service.store.get_exercise(owner, mc.id)
    assert (stored.subtopic, stored.choices, stored.answer_index, stored.explanation) == (
        "Verbs", ["ser", "estar"], 1, "Location uses estar.")
    sa = service.create_exercise(owner, goal.id, ExerciseCreateRequest(
        subtopic="Nouns", prompt="Plural of luz?", kind="short_answer", accepted_answers=["luces"]))
    assert service.store.get_exercise(owner, sa.id).accepted_answers == ["luces"]
    assert service.list_exercises(owner, goal.id).total == 2
    with pytest.raises(TutorNotFound):
        service.get_exercise(other, mc.id)
    with pytest.raises(TutorNotFound):
        service.create_exercise(other, goal.id, ExerciseCreateRequest(
            subtopic="Nouns", prompt="x", kind="short_answer", accepted_answers=["y"]))
    service.delete_exercise(owner, sa.id)
    assert service.list_exercises(owner, goal.id).total == 1


def test_concurrent_attempts_do_not_lose_a_box_update(isolated_postgres):
    from jarvis.tutor.models import AttemptRequest, ExerciseCreateRequest, GoalCreateRequest

    service, owner = _service(isolated_postgres.database_url), uuid4()
    goal = service.create_goal(owner, GoalCreateRequest(topic="Math", subtopics=["Add"]))
    ex = service.create_exercise(owner, goal.id, ExerciseCreateRequest(
        subtopic="Add", prompt="1+1", kind="short_answer", accepted_answers=["2"]))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.record_attempt(owner, ex.id, AttemptRequest(answer="2"), now=now),
                                range(4)))
    assert sorted(item.box for item in results) == [2, 3, 4, 5]  # each update saw the previous one
    stored = service.store.get_exercise(owner, ex.id)
    assert stored.box == 5 and stored.next_review_at == now + timedelta(days=16)
    wrong = service.record_attempt(owner, ex.id, AttemptRequest(answer="3"), now=now)
    assert wrong.box == 1 and wrong.next_review_at == now + timedelta(days=1)


def test_review_progress_and_cascade_delete(isolated_postgres):
    from jarvis.tutor.models import AttemptRequest, ExerciseCreateRequest, GoalCreateRequest

    url = isolated_postgres.database_url
    service, owner = _service(url), uuid4()
    goal = service.create_goal(owner, GoalCreateRequest(topic="Chem", subtopics=["Atoms", "Bonds"]))
    made = [service.create_exercise(owner, goal.id, ExerciseCreateRequest(
        subtopic=sub, prompt=f"Q{index}", kind="short_answer", accepted_answers=["a"]))
        for index, sub in enumerate(["Atoms", "Atoms", "Bonds"])]
    now = datetime.now(timezone.utc)
    service.record_attempt(owner, made[0].id, AttemptRequest(answer="a"), now=now - timedelta(seconds=30))
    service.record_attempt(owner, made[1].id, AttemptRequest(answer="b"), now=now - timedelta(days=3))
    for offset in range(3):
        service.record_attempt(owner, made[0].id, AttemptRequest(answer="a"), now=now - timedelta(seconds=20 - offset))
    queue = service.review_queue(owner, goal.id, limit=10)
    assert [item.prompt for item in queue.items] == ["Q1", "Q2"]
    assert all(not hasattr(item, "accepted_answers") for item in queue.items)
    progress = service.progress(owner, goal.id)
    assert (progress.exercises_total, progress.exercises_attempted, progress.attempts_total) == (3, 2, 5)
    assert (progress.recent_accuracy, progress.mastered, progress.due_for_review) == (0.8, 1, 2)
    by_name = {item.subtopic: item for item in progress.per_subtopic}
    assert (by_name["Atoms"].exercises, by_name["Atoms"].attempts, by_name["Atoms"].accuracy) == (2, 5, 0.8)
    assert (by_name["Bonds"].attempts, by_name["Bonds"].accuracy) == (0, None)

    import psycopg

    service.delete_goal(owner, goal.id)
    with psycopg.connect(url) as conn:
        counts = [conn.execute(f"SELECT count(*) FROM jarvis_app.{table} WHERE owner_id = %s", (owner,)).fetchone()[0]
                  for table in ("tutor_goals", "tutor_exercises", "tutor_attempts")]
    assert counts == [0, 0, 0]


def test_database_constraints_mirror_validation(isolated_postgres):
    import psycopg

    owner, goal_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    with psycopg.connect(isolated_postgres.database_url) as conn:
        conn.execute("INSERT INTO jarvis_app.tutor_goals VALUES (%s, %s, 't', 'beginner', 60, NULL, %s, 'active', %s, %s)",
                     (goal_id, owner, ["A"], now, now))
        conn.commit()
        bad = [
            ("INSERT INTO jarvis_app.tutor_goals VALUES (%s, %s, 't', 'beginner', 10, NULL, %s, 'active', %s, %s)",
             (uuid4(), owner, ["A"], now, now)),
            ("INSERT INTO jarvis_app.tutor_exercises (id, owner_id, goal_id, subtopic, prompt, kind, choices, "
             "answer_index, source, box, next_review_at, created_at) "
             "VALUES (%s, %s, %s, 'A', 'p', 'multiple_choice', %s, 2, 'owner', 1, %s, %s)",
             (uuid4(), owner, goal_id, ["a", "b"], now, now)),
            ("INSERT INTO jarvis_app.tutor_exercises (id, owner_id, goal_id, subtopic, prompt, kind, accepted_answers, "
             "source, box, next_review_at, created_at) VALUES (%s, %s, %s, 'A', 'p', 'short_answer', %s, 'owner', 6, %s, %s)",
             (uuid4(), owner, goal_id, ["a"], now, now)),
            ("INSERT INTO jarvis_app.tutor_exercises (id, owner_id, goal_id, subtopic, prompt, kind, accepted_answers, "
             "source, box, next_review_at, created_at) VALUES (%s, %s, %s, 'A', 'p', 'short_answer', %s, 'owner', 1, %s, %s)",
             (uuid4(), uuid4(), goal_id, ["a"], now, now)),  # another owner cannot attach to this goal
        ]
        for sql, params in bad:
            with pytest.raises(psycopg.errors.IntegrityError):
                conn.execute(sql, params)
            conn.rollback()
