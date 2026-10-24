"""v6.5 tutor: deterministic planning and grading, Leitner review, owner isolation and the model-assisted routes."""

from datetime import date, datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from database import runner
from jarvis.core.config import Settings
from jarvis.intelligence.base import ProviderError
from jarvis.intelligence.mock_provider import MockProvider
from jarvis.intelligence.openai_provider import _strict_response_schema
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy
from jarvis.tutor.grading import grade, next_box, next_review, normalize_answer, short_answer_matches
from jarvis.tutor.models import (
    AttemptRequest, ExerciseCreateRequest, GeneratedExerciseBatch, GoalCreateRequest, LearningGoal,
)
from jarvis.tutor.planning import build_study_plan
from jarvis.tutor.service import TutorNotFound, TutorService
from jarvis.tutor.stores import MemoryTutorStore

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TOKEN = "v65-tutor-token-with-at-least-32-chars"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
START = date(2026, 10, 26)


def goal(subtopics=("A", "B", "C"), weekly=60, target=None) -> LearningGoal:
    return LearningGoal(id=uuid4(), owner_id=OWNER, topic="Algebra", level="beginner", weekly_minutes=weekly,
                        target_date=target, subtopics=list(subtopics), status="active", created_at=NOW,
                        updated_at=NOW)


class FakeProvider:
    name = "fake"
    model = "fake-model"
    is_demo = False

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    async def generate_reply(self, message, *, system_instruction=None, response_model=None):
        self.calls.append({"message": message, "system": system_instruction, "response_model": response_model})
        if self.error is not None:
            raise self.error
        return self.reply


class DenyPolicy(CapabilityPolicy):
    def __init__(self, denied):
        super().__init__(OWNER)
        self.denied = denied

    def allows(self, owner_id, capability, **kwargs):
        return capability not in self.denied and super().allows(owner_id, capability, **kwargs)


def make_client(provider=None):
    app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                       local_auth_token=SecretStr(TOKEN), owner_id=OWNER), provider=provider)
    return app, TestClient(app, headers=AUTH)


def mc_batch(count=2):
    return json.dumps({"exercises": [
        {"prompt": f"What is {n} + {n}?", "choices": [str(n), str(2 * n), str(3 * n)], "answer_index": 1,
         "accepted_answers": [], "explanation": "Doubling."} for n in range(1, count + 1)]})


# ------------------------------------------------------------------ planning


def test_plan_without_target_gives_each_subtopic_learn_and_practice_then_review():
    g = goal(weekly=60)
    plan = build_study_plan(g, START)  # 2 sessions a week, 7 sessions -> 4 weeks
    assert plan.feasible is True and plan.note is None and plan.session_minutes == 30 and plan.sessions_per_week == 2
    assert [(s.subtopic, s.kind) for s in plan.sessions] == [
        ("A", "learn"), ("A", "practice"), ("B", "learn"), ("B", "practice"), ("C", "learn"), ("C", "practice"),
        (None, "review")]
    assert plan.sessions[0].date == START and plan.end_date < START + timedelta(days=28)
    for week in range(4):
        in_week = [s for s in plan.sessions if 0 <= (s.date - START).days - 7 * week < 7]
        assert len(in_week) <= 2
    assert plan.total_minutes == 7 * 30
    assert build_study_plan(g, START) == plan  # deterministic


def test_small_weekly_minutes_shorten_the_session():
    plan = build_study_plan(goal(weekly=20), START)
    assert plan.session_minutes == 20 and all(s.minutes == 20 for s in plan.sessions)


def test_plan_spreads_sessions_up_to_a_reachable_target():
    plan = build_study_plan(goal(weekly=120, target=START + timedelta(days=59)), START)
    assert plan.feasible is True and plan.sessions[-1].date <= START + timedelta(days=59)
    assert plan.sessions[-1].kind == "review" and (plan.sessions[-1].date - START).days > 40


def test_infeasible_target_returns_a_compressed_plan_with_a_note():
    plan = build_study_plan(goal(subtopics=[f"S{i}" for i in range(10)], weekly=30, target=START + timedelta(days=6)),
                            START)
    assert plan.feasible is False and "squeezes" in plan.note and len(plan.sessions) == 21
    assert all(START <= s.date <= START + timedelta(days=6) for s in plan.sessions)
    assert plan.weekly_minutes_needed > plan.weekly_minutes


def test_target_before_start_is_reported_and_planned_at_weekly_pace():
    plan = build_study_plan(goal(target=START - timedelta(days=1)), START)
    assert plan.feasible is False and "before the start date" in plan.note and plan.sessions[0].date == START


def test_plan_is_bounded():
    big = goal(subtopics=[f"Subtopic {i}" for i in range(30)], weekly=15)
    assert len(build_study_plan(big, START).sessions) == 61
    bounded = build_study_plan(big, START, max_sessions=10)
    assert len(bounded.sessions) == 10 and bounded.truncated is True


# ------------------------------------------------------------------ grading


def test_short_answer_normalization():
    assert normalize_answer("  Ｐａｒｉｓ.  ") == "paris"
    assert normalize_answer("The   Big\tApple!?") == "the big apple"
    assert short_answer_matches("STRASSE", ["straße"])  # casefold
    assert short_answer_matches("new  york", ["New York."])
    assert not short_answer_matches("four", ["4"])


def test_short_answer_numeric_equality():
    assert short_answer_matches("0.5", ["1e-1", ".50"])
    assert short_answer_matches("3.0000000000001", ["3"])
    assert not short_answer_matches("3.001", ["3"])
    assert short_answer_matches("nan", ["NaN"])  # not a number: compared as text only
    assert not short_answer_matches("inf", ["1e400"])


def test_multiple_choice_grading_and_leitner_transitions():
    service = TutorService(MemoryTutorStore())
    g = service.create_goal(OWNER, GoalCreateRequest(topic="T", subtopics=["A"]))
    view = service.create_exercise(OWNER, g.id, ExerciseCreateRequest(
        subtopic="a", prompt="Pick b", kind="multiple_choice", choices=["a", "b"], answer_index=1))
    assert view.subtopic == "A"  # canonical spelling
    exercise = service.store.get_exercise(OWNER, view.id)
    assert grade(exercise, 1) is True and grade(exercise, 0) is False
    with pytest.raises(ValueError):
        grade(exercise, "b")
    boxes = []
    for answer in (1, 1, 1, 1, 1, 0):
        result = service.record_attempt(OWNER, view.id, AttemptRequest(answer=answer), now=NOW)
        boxes.append(result.box)
        assert result.next_review_at == NOW + timedelta(days={1: 1, 2: 2, 3: 4, 4: 8, 5: 16}[result.box])
    assert boxes == [2, 3, 4, 5, 5, 1]
    assert next_box(5, True) == 5 and next_box(3, False) == 1 and next_review(3, NOW) == NOW + timedelta(days=4)


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize("payload", [
    {"subtopic": "A", "prompt": "x", "kind": "multiple_choice", "choices": ["a"], "answer_index": 0},
    {"subtopic": "A", "prompt": "x", "kind": "multiple_choice", "choices": ["a", "b"], "answer_index": 2},
    {"subtopic": "A", "prompt": "x", "kind": "multiple_choice", "choices": ["a", "b"]},
    {"subtopic": "A", "prompt": "x", "kind": "multiple_choice", "choices": ["a", "A"], "answer_index": 0},
    {"subtopic": "A", "prompt": "x", "kind": "multiple_choice", "choices": ["a", "b"], "answer_index": 0,
     "accepted_answers": ["a"]},
    {"subtopic": "A", "prompt": "x", "kind": "short_answer", "accepted_answers": []},
    {"subtopic": "A", "prompt": "x", "kind": "short_answer", "accepted_answers": ["a"], "choices": ["a", "b"]},
    {"subtopic": "A", "prompt": "", "kind": "short_answer", "accepted_answers": ["a"]},
    {"subtopic": "A", "prompt": "x", "kind": "short_answer", "accepted_answers": ["a"] * 11},
    {"subtopic": "A", "prompt": "x", "kind": "essay", "accepted_answers": ["a"]},
])
def test_invalid_exercises_are_rejected(payload):
    with pytest.raises(ValidationError):
        ExerciseCreateRequest.model_validate(payload)


@pytest.mark.parametrize("payload", [
    {"topic": "T", "subtopics": []},
    {"topic": "T", "subtopics": ["A", "a"]},
    {"topic": "T", "subtopics": ["x" * 121]},
    {"topic": "T", "subtopics": [f"s{i}" for i in range(31)]},
    {"topic": "", "subtopics": ["A"]},
    {"topic": "T", "subtopics": ["A"], "weekly_minutes": 10},
    {"topic": "T", "subtopics": ["A"], "weekly_minutes": 1201},
    {"topic": "T", "subtopics": ["A"], "level": "expert"},
    {"topic": "T", "subtopics": ["A"], "unexpected": 1},
])
def test_invalid_goals_are_rejected(payload):
    with pytest.raises(ValidationError):
        GoalCreateRequest.model_validate(payload)


# ------------------------------------------------------------------ API


def create_goal(client, **extra):
    body = {"topic": "Fractions", "level": "beginner", "weekly_minutes": 60, "subtopics": ["Adding", "Halves"], **extra}
    response = client.post("/tutor/goals", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_api_requires_authentication_and_capabilities():
    app, client = make_client()
    with client:
        anonymous = TestClient(app)
        assert anonymous.get("/tutor/goals").status_code == 401
        assert anonymous.post("/tutor/explain", json={"topic": "t", "question": "q"}).status_code == 401
        app.state.authorization_policy = DenyPolicy({Capability.TUTOR_WRITE})
        assert client.get("/tutor/goals").status_code == 200
        assert client.post("/tutor/goals", json={"topic": "T", "subtopics": ["A"]}).status_code == 403
        app.state.authorization_policy = DenyPolicy({Capability.TUTOR_READ})
        assert client.get("/tutor/goals").status_code == 403
        assert client.post("/tutor/explain", json={"topic": "t", "question": "q"}).status_code == 403
    assert CapabilityPolicy(OWNER).allows(OWNER, Capability.TUTOR_READ)
    assert CapabilityPolicy(OWNER).allows(OWNER, Capability.TUTOR_WRITE)


def test_goal_crud_filters_and_validation_errors():
    _, client = make_client()
    with client:
        first = create_goal(client)
        second = create_goal(client, topic="Geometry")
        assert client.post("/tutor/goals", json={"topic": "T", "subtopics": ["A", "a"]}).status_code == 422
        assert client.post("/tutor/goals", json={"topic": "T", "subtopics": ["A"], "weekly_minutes": 5}).status_code == 422
        patched = client.patch(f"/tutor/goals/{first['id']}", json={"status": "completed", "target_date": "2027-01-01"})
        assert patched.status_code == 200 and patched.json()["status"] == "completed"
        assert client.patch(f"/tutor/goals/{first['id']}", json={"topic": None}).status_code == 422
        cleared = client.patch(f"/tutor/goals/{first['id']}", json={"target_date": None}).json()
        assert cleared["target_date"] is None and cleared["status"] == "completed"
        completed = client.get("/tutor/goals", params={"status": "completed"}).json()
        assert [item["id"] for item in completed["items"]] == [first["id"]] and completed["total"] == 1
        page = client.get("/tutor/goals", params={"limit": 1, "offset": 1}).json()
        assert page["total"] == 2 and len(page["items"]) == 1
        assert client.get(f"/tutor/goals/{second['id']}").json()["topic"] == "Geometry"
        assert client.delete(f"/tutor/goals/{second['id']}").status_code == 204
        assert client.get(f"/tutor/goals/{second['id']}").status_code == 404
        plan = client.get(f"/tutor/goals/{first['id']}/plan", params={"start": "2026-11-02"}).json()
        assert plan["start_date"] == "2026-11-02" and len(plan["sessions"]) == 5
        assert client.get(f"/tutor/goals/{first['id']}/plan", params={"start": "not-a-date"}).status_code == 422


def test_answers_are_hidden_until_an_attempt():
    _, client = make_client()
    with client:
        g = create_goal(client)
        created = client.post(f"/tutor/goals/{g['id']}/exercises", json={
            "subtopic": "Halves", "prompt": "Half of 10?", "kind": "short_answer",
            "accepted_answers": ["5", "five"], "explanation": "10 / 2 = 5."})
        assert created.status_code == 201
        hidden = [created.json(), client.get(f"/tutor/exercises/{created.json()['id']}").json(),
                  client.get(f"/tutor/goals/{g['id']}/exercises").json()["items"][0],
                  client.get(f"/tutor/goals/{g['id']}/review").json()["items"][0]]
        for item in hidden:
            assert not {"answer_index", "accepted_answers", "explanation", "answer"} & set(item)
            assert "5" not in json.dumps(item).replace(item["id"], "").replace(item["goal_id"], "").replace(
                item["created_at"], "").replace(item["next_review_at"], "")
        result = client.post(f"/tutor/exercises/{created.json()['id']}/attempts", json={"answer": " Five! "}).json()
        assert result["correct"] is True and result["correct_answer"] == "5" and result["explanation"] == "10 / 2 = 5."
        assert result["box"] == 2
        assert client.post(f"/tutor/goals/{g['id']}/exercises", json={
            "subtopic": "Unknown", "prompt": "x", "kind": "short_answer", "accepted_answers": ["a"]}).status_code == 422
        mc = client.post(f"/tutor/goals/{g['id']}/exercises", json={
            "subtopic": "Adding", "prompt": "1+1?", "kind": "multiple_choice", "choices": ["1", "2"],
            "answer_index": 1}).json()
        assert client.post(f"/tutor/exercises/{mc['id']}/attempts", json={"answer": "2"}).status_code == 422
        assert client.post(f"/tutor/exercises/{mc['id']}/attempts", json={"answer": 7}).status_code == 422
        wrong = client.post(f"/tutor/exercises/{mc['id']}/attempts", json={"answer": 0}).json()
        assert wrong["correct"] is False and wrong["correct_choice_index"] == 1 and wrong["correct_answer"] == "2"


def test_owner_isolation():
    service = TutorService(MemoryTutorStore())
    g = service.create_goal(OWNER, GoalCreateRequest(topic="T", subtopics=["A"]))
    ex = service.create_exercise(OWNER, g.id, ExerciseCreateRequest(
        subtopic="A", prompt="p", kind="short_answer", accepted_answers=["x"]))
    for call in (lambda: service.get_goal(OTHER, g.id), lambda: service.get_exercise(OTHER, ex.id),
                 lambda: service.list_exercises(OTHER, g.id), lambda: service.review_queue(OTHER, g.id),
                 lambda: service.progress(OTHER, g.id), lambda: service.study_plan(OTHER, g.id),
                 lambda: service.delete_goal(OTHER, g.id), lambda: service.delete_exercise(OTHER, ex.id),
                 lambda: service.record_attempt(OTHER, ex.id, AttemptRequest(answer="x")),
                 lambda: service.create_exercise(OTHER, g.id, ExerciseCreateRequest(
                     subtopic="A", prompt="p", kind="short_answer", accepted_answers=["x"]))):
        with pytest.raises(TutorNotFound):
            call()
    assert service.list_goals(OTHER).total == 0 and service.get_goal(OWNER, g.id).id == g.id
    _, client = make_client()
    with client:
        assert client.get(f"/tutor/goals/{uuid4()}").status_code == 404
        assert client.get(f"/tutor/goals/{uuid4()}/progress").status_code == 404
        assert client.post(f"/tutor/exercises/{uuid4()}/attempts", json={"answer": "x"}).status_code == 404


def test_review_queue_and_progress():
    service = TutorService(MemoryTutorStore())
    g = service.create_goal(OWNER, GoalCreateRequest(topic="T", subtopics=["A", "B"]))
    a1 = service.create_exercise(OWNER, g.id, ExerciseCreateRequest(
        subtopic="A", prompt="p1", kind="short_answer", accepted_answers=["x"]))
    a2 = service.create_exercise(OWNER, g.id, ExerciseCreateRequest(
        subtopic="A", prompt="p2", kind="short_answer", accepted_answers=["y"]))
    service.create_exercise(OWNER, g.id, ExerciseCreateRequest(
        subtopic="B", prompt="p3", kind="short_answer", accepted_answers=["z"]))
    empty = service.progress(OWNER, g.id)
    assert empty.recent_accuracy is None and empty.exercises_total == 3 and empty.attempts_total == 0
    now = datetime.now(timezone.utc)
    service.record_attempt(OWNER, a1.id, AttemptRequest(answer="x"), now=now)
    service.record_attempt(OWNER, a2.id, AttemptRequest(answer="wrong"), now=now - timedelta(days=3))
    queue = service.review_queue(OWNER, g.id, limit=10)
    # a1 is due tomorrow; a2 was answered wrongly three days ago (due two days ago); p3 has never been answered.
    assert [item.prompt for item in queue.items] == ["p2", "p3"]
    assert service.review_queue(OWNER, g.id, limit=1).items[0].prompt == "p2"
    for _ in range(3):
        service.record_attempt(OWNER, a1.id, AttemptRequest(answer="x"), now=now)
    progress = service.progress(OWNER, g.id)
    assert (progress.exercises_total, progress.exercises_attempted, progress.attempts_total) == (3, 2, 5)
    assert progress.recent_accuracy == 0.8 and progress.recent_attempts == 5
    assert progress.mastered == 1 and progress.due_for_review == 2
    by_name = {item.subtopic: item for item in progress.per_subtopic}
    assert (by_name["A"].exercises, by_name["A"].attempts, by_name["A"].accuracy) == (2, 5, 0.8)
    assert (by_name["B"].exercises, by_name["B"].attempts, by_name["B"].accuracy) == (1, 0, None)
    for _ in range(25):
        service.record_attempt(OWNER, a2.id, AttemptRequest(answer="wrong"), now=now)
    assert service.progress(OWNER, g.id).recent_accuracy == 0.0  # only the last 20 attempts count
    service.delete_goal(OWNER, g.id)
    assert service.store.get_exercise(OWNER, a1.id) is None and len(service.store._attempts) == 0


def test_api_review_and_progress():
    _, client = make_client()
    with client:
        g = create_goal(client)
        ex = client.post(f"/tutor/goals/{g['id']}/exercises", json={
            "subtopic": "Adding", "prompt": "2+2?", "kind": "short_answer", "accepted_answers": ["4"]}).json()
        assert len(client.get(f"/tutor/goals/{g['id']}/review").json()["items"]) == 1
        assert client.get(f"/tutor/goals/{g['id']}/review", params={"limit": 51}).status_code == 422
        client.post(f"/tutor/exercises/{ex['id']}/attempts", json={"answer": "4.0"})
        assert client.get(f"/tutor/goals/{g['id']}/review").json()["items"] == []
        progress = client.get(f"/tutor/goals/{g['id']}/progress").json()
        assert progress["recent_accuracy"] == 1.0 and progress["exercises_attempted"] == 1
        assert client.delete(f"/tutor/exercises/{ex['id']}").status_code == 204
        assert client.get(f"/tutor/exercises/{ex['id']}").status_code == 404


# ------------------------------------------------------------------ model-assisted


def test_explain_with_the_demo_provider_is_labeled_demo():
    _, client = make_client(MockProvider())
    with client:
        response = client.post("/tutor/explain", json={"topic": "Fractions", "question": "What is a half?"})
        assert response.status_code == 200
        body = response.json()
        assert body["demo"] is True and body["model"] == "mock" and body["text"]
        assert client.post("/tutor/explain", json={"topic": "", "question": "q"}).status_code == 422


def test_explain_tailors_to_level_and_caps_length():
    provider = FakeProvider(reply="x" * 9000)
    _, client = make_client(provider)
    with client:
        body = client.post("/tutor/explain", json={"topic": "T", "question": "Q?", "level": "beginner"}).json()
        assert body["demo"] is False and body["model"] == "fake-model"
        assert len(body["text"]) == 8000 and body["truncated"] is True
        assert "one analogy" in provider.calls[0]["system"] and "jargon" in provider.calls[0]["system"]
        client.post("/tutor/explain", json={"topic": "T", "question": "Q?", "level": "advanced"})
        assert "advanced" in provider.calls[1]["system"]
        provider.error = ProviderError("secret upstream detail")
        failed = client.post("/tutor/explain", json={"topic": "T", "question": "Q?"})
        assert failed.status_code == 503 and "secret" not in failed.text


def test_generate_refuses_the_demo_provider():
    app, client = make_client(MockProvider())
    with client:
        g = create_goal(client)
        response = client.post(f"/tutor/goals/{g['id']}/exercises/generate", json={"subtopic": "Adding", "count": 2})
        assert response.status_code == 409 and "demo" in response.json()["detail"]
        assert client.get(f"/tutor/goals/{g['id']}/exercises").json()["total"] == 0


def test_generate_stores_valid_model_exercises():
    provider = FakeProvider(reply=mc_batch(2))
    _, client = make_client(provider)
    with client:
        g = create_goal(client)
        response = client.post(f"/tutor/goals/{g['id']}/exercises/generate",
                               json={"subtopic": "adding", "count": 2, "kind": "multiple_choice"})
        assert response.status_code == 201, response.text
        items = response.json()["items"]
        assert len(items) == 2 and all(item["source"] == "model" and item["subtopic"] == "Adding" for item in items)
        assert all("answer_index" not in item and "explanation" not in item for item in items)
        assert provider.calls[0]["response_model"] is GeneratedExerciseBatch
        result = client.post(f"/tutor/exercises/{items[0]['id']}/attempts", json={"answer": 1}).json()
        assert result["correct"] is True and result["explanation"] == "Doubling."
        assert client.post(f"/tutor/goals/{g['id']}/exercises/generate",
                           json={"subtopic": "Nope", "count": 1}).status_code == 422
        assert client.post(f"/tutor/goals/{g['id']}/exercises/generate",
                           json={"subtopic": "Adding", "count": 6}).status_code == 422
        assert client.post(f"/tutor/goals/{uuid4()}/exercises/generate",
                           json={"subtopic": "Adding", "count": 1}).status_code == 404


def test_generate_short_answer():
    reply = json.dumps({"exercises": [{"prompt": "Half of 8?", "choices": [], "answer_index": -1,
                                       "accepted_answers": ["4", "four"], "explanation": ""}]})
    _, client = make_client(FakeProvider(reply=reply))
    with client:
        g = create_goal(client)
        response = client.post(f"/tutor/goals/{g['id']}/exercises/generate",
                               json={"subtopic": "Halves", "count": 1, "kind": "short_answer"})
        assert response.status_code == 201, response.text
        ex_id = response.json()["items"][0]["id"]
        result = client.post(f"/tutor/exercises/{ex_id}/attempts", json={"answer": "4"}).json()
        assert result["correct"] is True and result["explanation"] is None


@pytest.mark.parametrize("reply", [
    "not json",
    json.dumps({"exercises": []}),  # wrong count
    json.dumps({"exercises": json.loads(mc_batch(2))["exercises"][:1] + [
        {"prompt": "bad", "choices": ["only one"], "answer_index": 0, "accepted_answers": [], "explanation": ""}]}),
    json.dumps({"exercises": [{"prompt": "x", "choices": ["a", "b"], "answer_index": 5, "accepted_answers": [],
                               "explanation": ""}] * 2}),
    json.dumps({"exercises": [{"prompt": "x" * 2001, "choices": ["a", "b"], "answer_index": 0,
                               "accepted_answers": [], "explanation": ""}] * 2}),
    json.dumps({"exercises": json.loads(mc_batch(2))["exercises"], "extra": "field"}),
    "x" * 70_000,
], ids=["not-json", "wrong-count", "one-choice", "bad-index", "long-prompt", "extra-field", "oversized"])
def test_generate_invalid_output_is_502_and_stores_nothing(reply):
    _, client = make_client(FakeProvider(reply=reply))
    with client:
        g = create_goal(client)
        response = client.post(f"/tutor/goals/{g['id']}/exercises/generate", json={"subtopic": "Adding", "count": 2})
        assert response.status_code == 502 and "none were stored" in response.json()["detail"]
        assert client.get(f"/tutor/goals/{g['id']}/exercises").json()["total"] == 0


def test_generate_provider_error_is_503_sanitized():
    _, client = make_client(FakeProvider(error=ProviderError("upstream said: key sk-123 invalid")))
    with client:
        g = create_goal(client)
        response = client.post(f"/tutor/goals/{g['id']}/exercises/generate", json={"subtopic": "Adding", "count": 1})
        assert response.status_code == 503 and "sk-123" not in response.text
        assert client.get(f"/tutor/goals/{g['id']}/exercises").json()["total"] == 0


def test_generation_schema_is_accepted_by_the_strict_openai_contract():
    schema = _strict_response_schema(GeneratedExerciseBatch)
    item = schema["$defs"]["GeneratedExercise"]
    assert set(item["required"]) == {"prompt", "choices", "answer_index", "accepted_answers", "explanation"}


# ------------------------------------------------------------------ wiring


def test_status_migration_and_audit_wiring():
    app, client = make_client(MockProvider())
    with client:
        g = create_goal(client)
        report = client.get("/status").json()
        tutor = next(item for item in report["subsystems"] if item["key"] == "tutor")
        assert tutor["state"] == "limited" and tutor["storage"] == "memory"
        assert any("real model provider" in item for item in tutor["limitations"])
    ids = [migration_id for migration_id, *_ in runner._migration_files(runner.MIGRATIONS_DIR)]
    assert "0068_tutor" in ids
    sql = (runner.MIGRATIONS_DIR / "0068_tutor.sql").read_text(encoding="utf-8")
    assert "ON DELETE CASCADE" in sql and "'tutor'" in sql and "tutor_exercises_owner_goal_review" in sql
    assert g["id"]
