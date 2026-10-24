"""Unified interface configuration (v3.0).

``GET /interface/config`` is public, like ``/ready``: it returns only fixed,
non-sensitive feature flags so the frontend can show which workspace views
are enabled and whether optional voice controls are offered. It contains no
URLs, identifiers, tokens or provider details. Voice is browser-native: the
server never receives audio.
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, StrictBool

router = APIRouter(tags=["interface"])


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InterfaceFeatures(_Frozen):
    chat: StrictBool
    memory: StrictBool
    coding: StrictBool
    browser: StrictBool
    tools: StrictBool
    chat_tools: StrictBool
    activity: StrictBool
    approval_execution: Literal[False] = False


class VoiceConfig(_Frozen):
    enabled: StrictBool
    mode: Literal["browser_native"] = "browser_native"
    server_receives_audio: Literal[False] = False
    auto_send: Literal[False] = False
    requires_opt_in: Literal[True] = True


class InterfaceConfig(_Frozen):
    contract_version: Literal["1"] = "1"
    features: InterfaceFeatures
    voice: VoiceConfig


@router.get("/interface/config", response_model=InterfaceConfig)
def interface_config(request: Request) -> InterfaceConfig:
    settings = request.app.state.settings
    return InterfaceConfig(
        features=InterfaceFeatures(
            chat=True, memory=bool(settings.persistence_enabled), coding=True,
            browser=bool(settings.browser_enabled), tools=bool(settings.tools_enabled),
            chat_tools=bool(settings.tools_enabled and settings.chat_tools_enabled), activity=True,
        ),
        voice=VoiceConfig(enabled=bool(settings.voice_enabled)),
    )
