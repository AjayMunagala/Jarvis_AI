"""Contracts for the v6.5 tutor: learning goals, study plans, exercises, attempts and progress.

Exercise read models (``ExerciseView``) never carry the answer or the explanation. Those are
returned only in an ``AttemptResult``, after the owner has answered.
"""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, field_validator, model_validator

Level = Literal["beginner", "intermediate", "advanced"]
GoalStatus = Literal["active", "completed", "archived"]
ExerciseKind = Literal["multiple_choice", "short_answer"]
ExerciseSource = Literal["owner", "model"]
SessionKind = Literal["learn", "practice", "review"]

Topic = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Subtopic = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Prompt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Choice = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
AcceptedAnswer = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Explanation = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]

MAX_SUBTOPICS = 30
MAX_CHOICES = 6
MAX_ACCEPTED_ANSWERS = 10
LEITNER_MAX_BOX = 5


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _unique_casefold(values: list[str] | None, what: str) -> list[str] | None:
    if values is None:
        return None
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key in seen:
            raise ValueError(f"{what} must be unique (ignoring case).")
        seen.add(key)
    return values


# ------------------------------------------------------------------ goals


class LearningGoal(StrictSchema):
    id: UUID
    owner_id: UUID
    topic: Topic
    level: Level
    weekly_minutes: StrictInt = Field(ge=15, le=1200)
    target_date: date | None = None
    subtopics: list[Subtopic] = Field(min_length=1, max_length=MAX_SUBTOPICS)
    status: GoalStatus = "active"
    created_at: datetime
    updated_at: datetime

    @field_validator("subtopics")
    @classmethod
    def _subtopics_unique(cls, value: list[str]) -> list[str]:
        return _unique_casefold(value, "Subtopics")


class GoalCreateRequest(StrictSchema):
    topic: Topic
    level: Level = "beginner"
    weekly_minutes: StrictInt = Field(default=120, ge=15, le=1200)
    target_date: date | None = None
    subtopics: list[Subtopic] = Field(min_length=1, max_length=MAX_SUBTOPICS)

    @field_validator("subtopics")
    @classmethod
    def _subtopics_unique(cls, value: list[str]) -> list[str]:
        return _unique_casefold(value, "Subtopics")


class GoalUpdateRequest(StrictSchema):
    """Fields left out are unchanged; ``target_date: null`` clears the target date."""

    topic: Topic | None = None
    level: Level | None = None
    weekly_minutes: StrictInt | None = Field(default=None, ge=15, le=1200)
    target_date: date | None = None
    subtopics: list[Subtopic] | None = Field(default=None, min_length=1, max_length=MAX_SUBTOPICS)
    status: GoalStatus | None = None

    @field_validator("subtopics")
    @classmethod
    def _subtopics_unique(cls, value: list[str] | None) -> list[str] | None:
        return _unique_casefold(value, "Subtopics")

    @model_validator(mode="after")
    def _no_null_for_required(self) -> "GoalUpdateRequest":
        for name in ("topic", "level", "weekly_minutes", "subtopics", "status"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null.")
        return self


class LearningGoalPage(StrictSchema):
    items: list[LearningGoal]
    total: int = Field(ge=0)


# ------------------------------------------------------------------ study plan


class StudySession(StrictSchema):
    date: date
    subtopic: str | None
    minutes: int = Field(ge=1)
    kind: SessionKind


class StudyPlan(StrictSchema):
    goal_id: UUID
    start_date: date
    end_date: date
    target_date: date | None
    session_minutes: int
    sessions_per_week: int
    weekly_minutes: int
    weekly_minutes_needed: int
    total_minutes: int
    feasible: bool
    note: str | None = None
    truncated: bool = False
    sessions: list[StudySession]


# ------------------------------------------------------------------ exercises


class ExerciseCreateRequest(StrictSchema):
    """``choices`` and ``answer_index`` for multiple choice; ``accepted_answers`` for short answer."""

    subtopic: Subtopic
    prompt: Prompt
    kind: ExerciseKind
    choices: list[Choice] | None = Field(default=None, min_length=2, max_length=MAX_CHOICES)
    answer_index: StrictInt | None = None
    accepted_answers: list[AcceptedAnswer] | None = Field(default=None, min_length=1, max_length=MAX_ACCEPTED_ANSWERS)
    explanation: Explanation | None = None

    @model_validator(mode="after")
    def _answer_matches_kind(self) -> "ExerciseCreateRequest":
        if self.kind == "multiple_choice":
            if self.choices is None:
                raise ValueError("Multiple-choice exercises need 2 to 6 choices.")
            if self.answer_index is None or not 0 <= self.answer_index < len(self.choices):
                raise ValueError("answer_index must point at one of the choices.")
            if self.accepted_answers is not None:
                raise ValueError("Multiple-choice exercises take answer_index, not accepted_answers.")
            _unique_casefold(self.choices, "Choices")
        else:
            if self.choices is not None or self.answer_index is not None:
                raise ValueError("Short-answer exercises take accepted_answers, not choices or answer_index.")
            if self.accepted_answers is None:
                raise ValueError("Short-answer exercises need 1 to 10 accepted answers.")
            from jarvis.tutor.grading import normalize_answer

            if any(not normalize_answer(item) for item in self.accepted_answers):
                raise ValueError("An accepted answer must contain more than punctuation.")
        return self


class Exercise(StrictSchema):
    """Stored exercise, including the answer. Never returned by the API before an attempt."""

    id: UUID
    owner_id: UUID
    goal_id: UUID
    subtopic: Subtopic
    prompt: Prompt
    kind: ExerciseKind
    choices: list[Choice] | None = None
    answer_index: StrictInt | None = None
    accepted_answers: list[AcceptedAnswer] | None = None
    explanation: Explanation | None = None
    source: ExerciseSource
    box: StrictInt = Field(ge=1, le=LEITNER_MAX_BOX)
    next_review_at: datetime
    created_at: datetime

    def view(self) -> "ExerciseView":
        return ExerciseView(id=self.id, goal_id=self.goal_id, subtopic=self.subtopic, prompt=self.prompt,
                            kind=self.kind, choices=self.choices, source=self.source, box=self.box,
                            next_review_at=self.next_review_at, created_at=self.created_at)


class ExerciseView(StrictSchema):
    """Exercise as shown before it is answered: no answer, no explanation."""

    id: UUID
    goal_id: UUID
    subtopic: str
    prompt: str
    kind: ExerciseKind
    choices: list[str] | None = None
    source: ExerciseSource
    box: int
    next_review_at: datetime
    created_at: datetime


class ExerciseViewPage(StrictSchema):
    items: list[ExerciseView]
    total: int = Field(ge=0)


class ReviewQueue(StrictSchema):
    goal_id: UUID
    as_of: datetime
    items: list[ExerciseView]


# ------------------------------------------------------------------ attempts


class AttemptRequest(StrictSchema):
    """A choice index for multiple choice, or text for short answer."""

    answer: StrictInt | Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class Attempt(StrictSchema):
    id: UUID
    owner_id: UUID
    exercise_id: UUID
    goal_id: UUID
    given_answer: str = Field(max_length=500)
    correct: bool
    created_at: datetime


class AttemptResult(StrictSchema):
    attempt_id: UUID
    correct: bool
    correct_answer: str
    correct_choice_index: int | None = None
    accepted_answers: list[str] | None = None
    explanation: str | None = None
    box: int
    next_review_at: datetime


# ------------------------------------------------------------------ progress


class SubtopicProgress(StrictSchema):
    subtopic: str
    exercises: int = Field(ge=0)
    attempts: int = Field(ge=0)
    accuracy: float | None = None


class GoalProgress(StrictSchema):
    goal_id: UUID
    as_of: datetime
    exercises_total: int = Field(ge=0)
    exercises_attempted: int = Field(ge=0)
    attempts_total: int = Field(ge=0)
    recent_accuracy: float | None = None
    recent_attempts: int = Field(ge=0)
    due_for_review: int = Field(ge=0)
    mastered: int = Field(ge=0)
    per_subtopic: list[SubtopicProgress]
    note: str = ("recent_accuracy covers the last 20 attempts; an exercise is mastered at Leitner box 4 or 5.")


# ------------------------------------------------------------------ model-assisted


class ExplainRequest(StrictSchema):
    topic: Topic
    question: Question
    level: Level = "beginner"


class ExplainResponse(StrictSchema):
    text: str
    model: str
    demo: bool
    truncated: bool = False


class GenerateExercisesRequest(StrictSchema):
    subtopic: Subtopic
    count: StrictInt = Field(default=3, ge=1, le=5)
    kind: ExerciseKind = "multiple_choice"


class GeneratedExercise(BaseModel):
    """Wire schema the model must fill (strict JSON). Every field is required; validation happens afterwards.

    For short answer, ``choices`` is empty and ``answer_index`` is -1. For multiple choice,
    ``accepted_answers`` is empty. ``explanation`` may be empty.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(max_length=2000)
    choices: list[str] = Field(max_length=MAX_CHOICES)
    answer_index: int
    accepted_answers: list[str] = Field(max_length=MAX_ACCEPTED_ANSWERS)
    explanation: str = Field(max_length=2000)


class GeneratedExerciseBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    exercises: list[GeneratedExercise] = Field(max_length=5)


class GeneratedExercises(StrictSchema):
    goal_id: UUID
    model: str
    items: list[ExerciseView]
