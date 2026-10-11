"""Validated, data-only expert and tool registries."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from jarvis.orchestration.models import IntentName


CONFIG_DIR = Path(__file__).resolve().parent / "config"


class RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpertDefinition(RegistryModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    persona: Literal[
        "RecommendationExpert", "AnalysisExpert", "ValidationExpert", "RiskExpert",
        "ComplianceExpert", "ArchitectureExpert", "KnowledgeExpert",
    ]
    description: str = Field(min_length=1, max_length=240)
    enabled: StrictBool = True
    tool_ids: tuple[str, ...] = ()


class ExpertRegistryConfig(RegistryModel):
    version: Literal[1]
    experts: tuple[ExpertDefinition, ...] = Field(min_length=1, max_length=20)
    intent_experts: dict[IntentName, tuple[str, ...]]
    parallel_intents: tuple[IntentName, ...] = ()


class ToolDefinition(RegistryModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_.-]+$")
    version: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=240)
    read_only: StrictBool
    executable: StrictBool
    risk: Literal["low", "medium", "high"]
    input_schema: dict[str, object]
    output_schema: dict[str, object]


class ToolRegistryConfig(RegistryModel):
    version: Literal[1]
    tools: tuple[ToolDefinition, ...] = Field(max_length=50)


def _read_config(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load registry configuration: {path.name}.") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Registry configuration {path.name} must contain a JSON object.")
    return data


class ExpertRegistry:
    def __init__(self, config: ExpertRegistryConfig):
        ids = [expert.id for expert in config.experts]
        if len(ids) != len(set(ids)):
            raise ValueError("Expert identifiers must be unique.")
        enabled = {expert.id for expert in config.experts if expert.enabled}
        required_intents = {
            "general_inquiry", "recommendation", "analysis", "validation",
            "risk_assessment", "compliance", "architecture",
        }
        if set(config.intent_experts) != required_intents:
            raise ValueError("Expert registry must define every supported intent exactly once.")
        if set(config.parallel_intents) - required_intents:
            raise ValueError("Parallel execution references an unsupported intent.")
        for intent, expert_ids in config.intent_experts.items():
            if not expert_ids or len(expert_ids) > 5:
                raise ValueError(f"Intent {intent} must select between one and five experts.")
            unknown = set(expert_ids) - enabled
            if unknown:
                raise ValueError(f"Intent {intent} references an unavailable expert.")
        self.config = config
        self._by_id = {expert.id: expert for expert in config.experts}

    @classmethod
    def load(cls, path: Path = CONFIG_DIR / "experts.json") -> "ExpertRegistry":
        try:
            return cls(ExpertRegistryConfig.model_validate(_read_config(path)))
        except ValidationError as exc:
            raise ValueError(f"Invalid expert registry configuration: {path.name}.") from exc

    def select(self, intent: IntentName) -> tuple[ExpertDefinition, ...]:
        try:
            return tuple(self._by_id[expert_id] for expert_id in self.config.intent_experts[intent])
        except KeyError as exc:
            raise ValueError("No experts are configured for this intent.") from exc


class ToolRegistry:
    """Stores validated tool contracts; it deliberately provides no executor."""

    def __init__(self, config: ToolRegistryConfig):
        ids = [tool.id for tool in config.tools]
        if len(ids) != len(set(ids)):
            raise ValueError("Tool identifiers must be unique.")
        for tool in config.tools:
            if not tool.read_only or tool.executable or tool.risk != "low":
                raise ValueError("This milestone permits only non-executable, low-risk read-only tool definitions.")
        self.config = config
        self._by_id = {tool.id: tool for tool in config.tools}

    @classmethod
    def load(cls, path: Path = CONFIG_DIR / "tools.json") -> "ToolRegistry":
        try:
            return cls(ToolRegistryConfig.model_validate(_read_config(path)))
        except ValidationError as exc:
            raise ValueError(f"Invalid tool registry configuration: {path.name}.") from exc

    def get(self, tool_id: str) -> ToolDefinition | None:
        return self._by_id.get(tool_id)

    @property
    def execution_enabled(self) -> bool:
        return False
