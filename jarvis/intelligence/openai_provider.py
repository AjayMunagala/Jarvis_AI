"""Minimal asynchronous adapter for the OpenAI Chat Completions endpoint."""

from time import perf_counter
from typing import Any, Callable

import anyio.to_thread
import httpx
from pydantic import AnyHttpUrl, BaseModel, SecretStr, ValidationError

from .base import ProviderError, TransientProviderError
from .schemas import ProviderReply, _ChatCompletion


# OpenAI's strict subset omits these constraints for some model classes. They
# remain enforced by Pydantic after the response is received. maxItems/minItems
# are supported for standard models, but omitted here to also support configured
# fine-tuned models without depending on model-specific schema behavior.
_DROPPED_STRICT_SCHEMA_KEYWORDS = frozenset({
    "minLength", "maxLength", "minItems", "maxItems",
    "allOf", "not", "dependentRequired", "dependentSchemas", "if", "then", "else",
})
MAX_COMPLETION_TOKENS = 1024
MAX_STRUCTURED_COMPLETION_TOKENS = 8192


def _copy_for_strict_outputs(value):
    """Copy a JSON Schema and prune only schema keywords, never field names/data."""
    object_maps = {"properties", "patternProperties", "$defs", "definitions"}
    schema_values = {
        "items", "additionalProperties", "unevaluatedProperties", "contains",
        "propertyNames", "contentSchema",
    }
    schema_lists = {"anyOf", "oneOf", "prefixItems"}

    def copy_data(item):
        if isinstance(item, dict):
            return {key: copy_data(child) for key, child in item.items()}
        if isinstance(item, list):
            return [copy_data(child) for child in item]
        return item

    def copy_schema(node):
        if isinstance(node, dict):
            copied = {}
            for key, child in node.items():
                if key in _DROPPED_STRICT_SCHEMA_KEYWORDS:
                    continue
                if key in object_maps and isinstance(child, dict):
                    copied[key] = {field: copy_schema(subschema) for field, subschema in child.items()}
                elif key in schema_values:
                    copied[key] = copy_schema(child)
                elif key in schema_lists and isinstance(child, list):
                    copied[key] = [copy_schema(subschema) for subschema in child]
                else:
                    copied[key] = copy_data(child)
            return copied
        if isinstance(node, list):
            return [copy_schema(child) for child in node]
        return node

    return copy_schema(value)


def _resolve_schema_pointer(root: dict[str, object], pointer: str) -> object:
    if pointer == "#":
        return root
    if not pointer.startswith("#/"):
        raise ValueError("Only local JSON Schema references are supported.")
    current: object = root
    for token in pointer[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdecimal() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise ValueError("JSON Schema contains an unresolved reference.")
    return current


def _validate_strict_schema(schema: object) -> dict[str, object]:
    """Fail closed unless the transformed contract meets strict object rules."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("Strict output schema root must be an object.")

    object_maps = {"properties", "patternProperties", "$defs", "definitions"}
    schema_values = {
        "items", "additionalProperties", "unevaluatedProperties", "contains",
        "propertyNames", "contentSchema",
    }
    schema_lists = {"anyOf", "oneOf", "prefixItems"}
    supported_schema_keywords = {
        "$defs", "$ref", "additionalProperties", "description", "enum", "items",
        "properties", "required", "title", "type", "anyOf",
    } | _DROPPED_STRICT_SCHEMA_KEYWORDS

    def visit(node: object) -> None:
        if isinstance(node, dict):
            unknown = set(node) - supported_schema_keywords
            if unknown:
                raise ValueError("Strict output schema contains an unsupported keyword.")
            if _DROPPED_STRICT_SCHEMA_KEYWORDS.intersection(node):
                raise ValueError("Unsupported strict output schema keyword remains.")
            if node.get("type") == "object":
                properties = node.get("properties")
                required = node.get("required")
                if not isinstance(properties, dict) or not isinstance(required, list):
                    raise ValueError("Strict output object must declare properties and required fields.")
                if set(required) != set(properties) or node.get("additionalProperties") is not False:
                    raise ValueError("Strict output objects must require all properties and reject extras.")
            reference = node.get("$ref")
            if reference is not None:
                if not isinstance(reference, str) or not isinstance(_resolve_schema_pointer(schema, reference), (dict, list)):
                    raise ValueError("Strict output schema contains an invalid reference.")
            for key, child in node.items():
                if key in object_maps and isinstance(child, dict):
                    for subschema in child.values():
                        visit(subschema)
                elif key in schema_values:
                    visit(child)
                elif key in schema_lists and isinstance(child, list):
                    for subschema in child:
                        visit(subschema)

    visit(schema)
    return schema


def _strict_response_schema(response_model: type[BaseModel]) -> dict[str, object]:
    try:
        copied = _copy_for_strict_outputs(response_model.model_json_schema())
        return _validate_strict_schema(copied)
    except (TypeError, ValueError) as exc:
        raise ProviderError() from exc


class OpenAIProvider:
    name = "openai"
    is_demo = False

    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: AnyHttpUrl,
        model: str,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        role: str = "primary",
    ):
        self._api_key = api_key.get_secret_value()
        self.model = model
        self.role = role
        # v6.1: set by the application to UsageService.record; called once per request, off the event loop.
        self.usage_recorder: Callable[..., Any] | None = None
        self._endpoint = f"{str(base_url).rstrip('/')}/chat/completions"
        self._model = model
        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=connect_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._transport = transport

    async def generate_reply(
        self,
        message: str,
        *,
        system_instruction: str | None = None,
        response_model: type[BaseModel] | None = None,
    ) -> str:
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        else:
            messages.append({
                "role": "system",
                "content": "You are JARVIS, an advisory assistant. Do not claim to perform actions or access information you do not have.",
            })
        messages.append({"role": "user", "content": message})
        payload: dict[str, object] = {
            "model": self._model,
            "messages": messages,
            "max_completion_tokens": (
                MAX_STRUCTURED_COMPLETION_TOKENS if response_model is not None else MAX_COMPLETION_TOKENS
            ),
        }
        if response_model is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__.lower(),
                    "strict": True,
                    "schema": _strict_response_schema(response_model),
                },
            }
        started = perf_counter()
        usage: dict[str, int | None] = {}
        outcome = "failed"
        try:
            content = await self._call(payload, response_model, usage)
            outcome = "succeeded"
            return content
        except TransientProviderError as exc:
            outcome = "timeout" if isinstance(exc.__cause__, httpx.TimeoutException) else "failed"
            raise
        finally:
            await self._record(outcome, (perf_counter() - started) * 1000, usage)

    async def _record(self, outcome: str, duration_ms: float, usage: dict[str, int | None]) -> None:
        if self.usage_recorder is None:
            return
        try:
            await anyio.to_thread.run_sync(lambda: self.usage_recorder(
                provider=self.name, model=self.model, outcome=outcome, duration_ms=duration_ms, role=self.role,
                prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens")))
        except Exception:  # noqa: BLE001 - usage tracking never affects the reply
            pass

    async def _call(self, payload: dict[str, object], response_model: type[BaseModel] | None,
                    usage: dict[str, int | None]) -> str:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise TransientProviderError() from exc
        except httpx.NetworkError as exc:
            raise TransientProviderError() from exc
        except httpx.HTTPError as exc:
            raise ProviderError() from exc

        if response.status_code == 429 or 500 <= response.status_code <= 599:
            raise TransientProviderError()
        if not 200 <= response.status_code <= 299:
            raise ProviderError()
        try:
            completion = _ChatCompletion.model_validate(response.json())
            if completion.usage is not None:
                usage.update(prompt_tokens=completion.usage.prompt_tokens,
                             completion_tokens=completion.usage.completion_tokens)
            choice = completion.choices[0]
            if choice.finish_reason not in (None, "stop", "length"):
                raise ProviderError()
            if response_model is not None and choice.finish_reason == "length":
                raise ProviderError()
            message = choice.message
            if message.refusal:
                raise ProviderError()
            content = message.content
            if content is None:
                raise ProviderError()
            return ProviderReply(content=content).content
        except (ValueError, ValidationError) as exc:
            raise ProviderError() from exc


class FallbackProvider:
    """v6.1: the primary model, with one retry on a fallback model when the primary is temporarily unavailable.

    Only transient failures (timeouts, network errors, HTTP 429 and 5xx) fall back; a request the provider
    rejects (for example invalid input) is not sent again. Both calls are recorded in the usage log, so it shows
    which model answered.
    """

    name = "openai"
    is_demo = False

    def __init__(self, primary: OpenAIProvider, fallback: OpenAIProvider):
        self.primary = primary
        self.fallback = fallback

    @property
    def usage_recorder(self):
        return self.primary.usage_recorder

    @usage_recorder.setter
    def usage_recorder(self, recorder) -> None:
        self.primary.usage_recorder = recorder
        self.fallback.usage_recorder = recorder

    async def generate_reply(self, message: str, *, system_instruction: str | None = None,
                             response_model: type[BaseModel] | None = None) -> str:
        try:
            return await self.primary.generate_reply(message, system_instruction=system_instruction,
                                                     response_model=response_model)
        except TransientProviderError:
            return await self.fallback.generate_reply(message, system_instruction=system_instruction,
                                                      response_model=response_model)

