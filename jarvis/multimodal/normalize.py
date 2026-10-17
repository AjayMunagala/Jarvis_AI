"""Bounded validation and normalization of multimodal inputs.

Images are accepted only as base64 data (never file paths or URLs). The
declared media type must match the file signature, and dimensions are read
from the header without decoding pixels, with limits that also guard against
decompression bombs. SVG and other formats are rejected.
"""

import base64
import binascii
import hashlib
import struct

from jarvis.browser.redaction import redact_text
from jarvis.multimodal.models import (
    ImagePart, ModalityDescriptor, MultimodalRequest, NormalizedPart, ObservationPart, TextPart, TranscriptPart,
)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MAX_DIMENSION = 8192
MAX_PIXELS = 40_000_000


class MultimodalInputError(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.public_message = message


def _png(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR" or len(data) < 24:
        return None
    return struct.unpack(">II", data[16:24])


def _gif(data: bytes):
    if data[:6] not in (b"GIF87a", b"GIF89a") or len(data) < 10:
        return None
    return struct.unpack("<HH", data[6:10])


def _jpeg(data: bytes):
    if data[:3] != b"\xff\xd8\xff":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = struct.unpack(">H", data[index + 2:index + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5:index + 9])
            return width, height
        index += 2 + length
    return None


def _webp(data: bytes):
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP" or len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8L" and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    return None


_SNIFFERS = {"image/png": _png, "image/gif": _gif, "image/jpeg": _jpeg, "image/webp": _webp}


def _image(index: int, part: ImagePart) -> NormalizedPart:
    try:
        data = base64.b64decode(part.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MultimodalInputError(f"Part {index}: the image is not valid base64.") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise MultimodalInputError(f"Part {index}: the image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MiB.")
    detected = next((media for media, sniff in _SNIFFERS.items() if sniff(data) is not None), None)
    if detected is None:
        raise MultimodalInputError(f"Part {index}: unsupported or corrupt image (PNG, JPEG, GIF and WebP are accepted).")
    if detected != part.media_type:
        raise MultimodalInputError(f"Part {index}: declared {part.media_type} but the data is {detected}.")
    width, height = _SNIFFERS[detected](data)
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION) or width * height > MAX_PIXELS:
        raise MultimodalInputError(f"Part {index}: image dimensions {width}x{height} are outside the allowed limits.")
    descriptor = ModalityDescriptor(part_index=index, modality="image", source=part.source, media_type=detected,
                                    byte_size=len(data), width=width, height=height,
                                    sha256=hashlib.sha256(data).hexdigest(), label=part.label)
    return NormalizedPart(descriptor=descriptor, data=data)


def normalize(request: MultimodalRequest) -> tuple[NormalizedPart, ...]:
    parts: list[NormalizedPart] = []
    total = 0
    for index, part in enumerate(request.parts):
        if isinstance(part, ImagePart):
            normalized = _image(index, part)
            total += normalized.descriptor.byte_size
            if total > MAX_TOTAL_BYTES:
                raise MultimodalInputError("The combined media size exceeds the request limit.")
        elif isinstance(part, TranscriptPart):
            text = redact_text(part.text)[0]
            normalized = NormalizedPart(descriptor=ModalityDescriptor(
                part_index=index, modality="audio_transcript", source=part.source, char_count=len(text),
                label=part.language), text=text)
        elif isinstance(part, ObservationPart):
            text = "\n".join(redact_text(item)[0][:1000] for item in part.observations)
            normalized = NormalizedPart(descriptor=ModalityDescriptor(
                part_index=index, modality="visual_observation", source=part.source, char_count=len(text),
                label=part.reference), text=text)
        elif isinstance(part, TextPart):
            normalized = NormalizedPart(descriptor=ModalityDescriptor(
                part_index=index, modality="text", source="user", char_count=len(part.text)), text=part.text)
        else:  # pragma: no cover - the discriminated union prevents this
            raise MultimodalInputError(f"Part {index}: unsupported content part.")
        parts.append(normalized)
    return tuple(parts)
