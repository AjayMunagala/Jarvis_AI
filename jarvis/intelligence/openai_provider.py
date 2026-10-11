"""Minimal asynchronous adapter for the OpenAI Chat Completions endpoint."""

import httpx
from pydantic import AnyHttpUrl, SecretStr, ValidationError

from .base import ProviderError, TransientProviderError
from .schemas import ProviderReply, _ChatCompletion


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
    ):
        self._api_key = api_key.get_secret_value()
        self._endpoint = f"{str(base_url).rstrip('/')}/chat/completions"
        self._model = model
        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=connect_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._transport = transport

    async def generate_reply(self, message: str, *, system_instruction: str | None = None) -> str:
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        else:
            messages.append({
                "role": "system",
                "content": "You are JARVIS, an advisory assistant. Do not claim to perform actions or access information you do not have.",
            })
        messages.append({"role": "user", "content": message})
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self._model, "messages": messages, "max_completion_tokens": 1024},
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
            content = completion.choices[0].message.content
            if content is None:
                raise ProviderError()
            return ProviderReply(content=content).content
        except (ValueError, ValidationError) as exc:
            raise ProviderError() from exc
