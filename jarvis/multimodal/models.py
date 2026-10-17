"""Multimodal content parts, descriptors and results."""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

ImageMediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
InputModality = Literal["text", "image", "audio_transcript", "visual_observation"]

MAX_PARTS = 8
MAX_TEXT_CHARS = 20_000
MAX_BASE64_CHARS = 7_000_000  # ~5 MiB decoded


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TextPart(Frozen):
    kind: Literal["text"] = "text"
    text: StrictStr = Field(min_length=1, max_length=MAX_TEXT_CHARS)


class ImagePart(Frozen):
    kind: Literal["image"] = "image"
    media_type: ImageMediaType
    data_base64: StrictStr = Field(min_length=4, max_length=MAX_BASE64_CHARS, repr=False)
    source: Literal["upload", "screenshot", "browser_capture"]
    label: StrictStr | None = Field(default=None, max_length=200)


class TranscriptPart(Frozen):
    kind: Literal["audio_transcript"] = "audio_transcript"
    text: StrictStr = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    source: Literal["browser_speech", "provided_transcript"]
    language: StrictStr | None = Field(default=None, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


class ObservationPart(Frozen):
    kind: Literal["visual_observation"] = "visual_observation"
    observations: tuple[StrictStr, ...] = Field(min_length=1, max_length=50)
    source: Literal["browser_observation"]
    reference: StrictStr | None = Field(default=None, max_length=300)


ContentPart = Annotated[Union[TextPart, ImagePart, TranscriptPart, ObservationPart], Field(discriminator="kind")]


class MultimodalRequest(Frozen):
    prompt: StrictStr = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    parts: tuple[ContentPart, ...] = Field(default=(), max_length=MAX_PARTS)
    # Media and transcripts may only reach a remote provider when the owner
    # explicitly allows it for this request; the default keeps them local.
    allow_external_disclosure: StrictBool = False
    policy: StrictStr | None = None


class ModalityDescriptor(Frozen):
    part_index: int = Field(ge=0)
    modality: InputModality
    source: StrictStr
    media_type: StrictStr | None = None
    byte_size: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    sha256: StrictStr | None = None
    char_count: int | None = Field(default=None, ge=0)
    label: StrictStr | None = None


class NormalizedPart(Frozen):
    descriptor: ModalityDescriptor
    text: StrictStr | None = None
    data: bytes | None = Field(default=None, repr=False, exclude=True)


class Interpretation(Frozen):
    model_key: StrictStr
    text: StrictStr
    simulated: StrictBool
    kind: Literal["model_interpretation"] = "model_interpretation"


class InspectionResult(Frozen):
    descriptors: tuple[ModalityDescriptor, ...]
    modalities: tuple[InputModality, ...]
    eligible_models: tuple[StrictStr, ...]
    excluded: dict[str, tuple[StrictStr, ...]]
    external_disclosure: Literal["not_allowed", "allowed_by_owner"]
    note: StrictStr


class MultimodalResult(Frozen):
    status: Literal["succeeded", "unsupported", "failed"]
    observations: tuple[ModalityDescriptor, ...]
    interpretation: Interpretation | None = None
    model_key: StrictStr | None = None
    decision_id: StrictStr | None = None
    reasons: tuple[StrictStr, ...] = ()
    note: StrictStr
