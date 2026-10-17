"""v5.4 multimodal validation, capability checks, privacy and mock workflows.

Only deterministic mock adapters are used; nothing here measures real image
understanding.
"""

import asyncio
import base64
import struct
import zlib

import pytest
from pydantic import ValidationError

from jarvis.intelligence.mock_provider import MockProvider
from jarvis.multimodal.models import MultimodalRequest
from jarvis.multimodal.normalize import MAX_IMAGE_BYTES, MultimodalInputError, normalize
from jarvis.multimodal.service import MockMultimodalAdapter, MultimodalService
from jarvis.routing.models import ModelDescriptor, RegistryConfig
from jarvis.routing.registry import ModelRegistry, load_policies
from jarvis.routing.router import ModelRouter


def png(width=4, height=3):
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg(width=10, height=6):
    return (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00" + b"\xff\xd9")


def gif(width=2, height=2):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00;"


def webp(width=20, height=30):
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def image(data, media="image/png", **extra):
    return {"kind": "image", "media_type": media, "data_base64": base64.b64encode(data).decode(), "source": "upload", **extra}


def descriptor(key, provider, modalities=("text",), locality="local", tasks=("general",)):
    return ModelDescriptor(model_key=key, provider=provider, model_id=key, display_name=key, task_types=tasks,
                           input_modalities=modalities, locality=locality, capability_source="operator_declared")


def service(*, local_vision=True, remote_vision=True):
    models = [descriptor("text-local", "textonly")]
    if local_vision:
        models.append(descriptor("vision-local", "mockvision", ("text", "image", "audio_transcript"), tasks=("vision",)))
    if remote_vision:
        models.append(descriptor("vision-remote", "remotevision", ("text", "image"), locality="remote", tasks=("vision",)))
    router = ModelRouter(ModelRegistry(RegistryConfig(version=1, models=tuple(models))), load_policies(),
                         {"textonly": MockProvider(), "mockvision": object(), "remotevision": object()})
    adapters = {"mockvision": MockMultimodalAdapter("mockvision"), "remotevision": MockMultimodalAdapter("remotevision")}
    return MultimodalService(router, adapters), adapters


# --------------------------------------------------------------- validation

@pytest.mark.parametrize("data, media, size", [
    (png(4, 3), "image/png", (4, 3)), (jpeg(10, 6), "image/jpeg", (10, 6)), (gif(2, 2), "image/gif", (2, 2)),
    (webp(20, 30), "image/webp", (20, 30)),
])
def test_images_are_sniffed_and_described(data, media, size):
    parts = normalize(MultimodalRequest(prompt="describe", parts=(image(data, media),)))
    item = parts[0].descriptor
    assert (item.media_type, item.width, item.height, item.byte_size) == (media, *size, len(data))
    assert len(item.sha256) == 64 and "data" not in parts[0].model_dump()


@pytest.mark.parametrize("data, media, message", [
    (png(), "image/jpeg", "declared image/jpeg but the data is image/png"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/png", "unsupported or corrupt"),
    (png(9000, 1), "image/png", "outside the allowed limits"),
    (png(7000, 7000)[:33] + b"\x00" * 10, "image/png", "outside the allowed limits"),
])
def test_invalid_images_are_rejected(data, media, message):
    with pytest.raises(MultimodalInputError) as raised:
        normalize(MultimodalRequest(prompt="x", parts=(image(data, media),)))
    assert message in raised.value.public_message


def test_size_base64_and_part_limits():
    with pytest.raises(MultimodalInputError):
        normalize(MultimodalRequest(prompt="x", parts=({"kind": "image", "media_type": "image/png",
                                                         "data_base64": "not base64!", "source": "upload"},)))
    big = png() + b"\x00" * (MAX_IMAGE_BYTES + 1)
    with pytest.raises((MultimodalInputError, ValidationError)):
        normalize(MultimodalRequest(prompt="x", parts=(image(big),)))
    with pytest.raises(ValidationError):
        MultimodalRequest(prompt="x", parts=tuple({"kind": "text", "text": "t"} for _ in range(9)))
    with pytest.raises(ValidationError):
        MultimodalRequest(prompt="x", parts=({"kind": "image", "media_type": "image/svg+xml",
                                               "data_base64": "AAAA", "source": "upload"},))
    with pytest.raises(ValidationError):  # no file paths or URLs are accepted
        MultimodalRequest(prompt="x", parts=({"kind": "image", "media_type": "image/png", "path": "C:/secret.png",
                                               "data_base64": "AAAA", "source": "upload"},))


def test_transcripts_and_observations_are_redacted_text():
    parts = normalize(MultimodalRequest(prompt="x", parts=(
        {"kind": "audio_transcript", "text": "my key is sk-ant-" + "a" * 40, "source": "browser_speech", "language": "en-US"},
        {"kind": "visual_observation", "observations": ["Button: Pay now"], "source": "browser_observation"},
    )))
    assert "sk-ant-" + "a" * 40 not in parts[0].text and parts[0].descriptor.label == "en-US"
    assert parts[1].descriptor.modality == "visual_observation" and parts[1].text == "Button: Pay now"


# --------------------------------------------------- capability and privacy

def test_media_stay_local_unless_owner_allows_external_disclosure():
    mm, adapters = service()
    request = MultimodalRequest(prompt="What is shown?", parts=(image(png()),))
    inspection = mm.inspect(request)
    assert inspection.eligible_models == ("vision-local",) and inspection.external_disclosure == "not_allowed"
    assert "remote execution is not permitted for this request" in inspection.excluded["vision-remote"]
    assert "unsupported input modality: image" in inspection.excluded["text-local"]
    allowed = mm.inspect(request.model_copy(update={"allow_external_disclosure": True}))
    assert set(allowed.eligible_models) == {"vision-local", "vision-remote"}
    only_remote, remote_adapters = service(local_vision=False)
    result = asyncio.run(only_remote.analyze(request))
    assert result.status == "unsupported" and remote_adapters["remotevision"].calls == []
    assert any("remote execution is not permitted" in reason for reason in result.reasons)


def test_unsupported_modalities_are_reported_not_faked():
    mm, adapters = service(local_vision=False, remote_vision=False)
    result = asyncio.run(mm.analyze(MultimodalRequest(prompt="x", parts=(image(png()),), allow_external_disclosure=True)))
    assert result.status == "unsupported" and result.interpretation is None
    assert result.observations[0].width == 4 and all(adapter.calls == [] for adapter in adapters.values())
    assert mm.inspect(MultimodalRequest(prompt="x", parts=(image(png()),))).eligible_models == ()


def test_mock_workflow_separates_observations_from_simulated_interpretation():
    mm, adapters = service()
    request = MultimodalRequest(prompt="Read the diagram", parts=(
        image(jpeg(), "image/jpeg", label="architecture"), {"kind": "text", "text": "Focus on the database"},
        {"kind": "audio_transcript", "text": "explain the arrows", "source": "browser_speech"}))
    result = asyncio.run(mm.analyze(request))
    assert result.status == "succeeded" and result.model_key == "vision-local"
    assert [item.modality for item in result.observations] == ["image", "text", "audio_transcript"]
    assert result.interpretation.simulated is True and "Simulated interpretation" in result.interpretation.text
    assert "untrusted" in result.note and "simulated" in result.note
    assert adapters["mockvision"].calls == [("Read the diagram", 3)]


def test_adapter_failures_and_timeouts_are_sanitized():
    mm, adapters = service()

    async def boom(prompt, parts):
        raise RuntimeError("secret internals sk-test")

    async def slow(prompt, parts):
        await asyncio.sleep(1)

    request = MultimodalRequest(prompt="x", parts=(image(png()),))
    adapters["mockvision"].analyze = boom
    failed = asyncio.run(mm.analyze(request))
    assert failed.status == "failed" and "sk-test" not in failed.model_dump_json()
    adapters["mockvision"].analyze = slow
    mm.timeout_seconds = 0.05
    assert asyncio.run(mm.analyze(request)).reasons == ("The multimodal model timed out.",)


def test_text_only_requests_use_ordinary_text_models():
    mm, _ = service(local_vision=False, remote_vision=False)
    inspection = mm.inspect(MultimodalRequest(prompt="just text"))
    assert inspection.eligible_models == ("text-local",) and inspection.modalities == ("text",)
