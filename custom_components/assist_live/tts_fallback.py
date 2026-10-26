import re
import struct
from collections.abc import AsyncGenerator

from assist_live_client.models import TTSFormat
from homeassistant.components.tts import TTSAudioResponse

_ABBREVIATIONS = frozenset(
    "mr. mrs. ms. dr. prof. sr. jr. st. vs. etc. e.g. i.e. a.m. p.m.".split()
)


def remaining_text(text: str, completed: int) -> str | None:
    """Match sentence boundaries from the pinned server provider."""
    boundaries = []
    boundary = 0
    period = False
    start = 0
    for offset, character in enumerate(text):
        if boundary and character.isspace():
            token = text[start:offset].split()[-1].lstrip("\"'“‘([").lower()
            if not period or not (
                token in _ABBREVIATIONS or re.fullmatch(r"(?:[a-z]\.)+", token)
            ):
                boundaries.append(offset)
                start = offset
            boundary = 0
        elif character in ".!?":
            period = boundary != 1 and character == "."
            boundary = 1
        elif boundary and character in "\"'”’)]":
            boundary = 2
            period = False
        else:
            boundary = 0
    if text[start:].strip():
        boundaries.append(len(text))
    if not 0 < completed <= len(boundaries):
        return None
    return text[boundaries[completed - 1] :].lstrip()


async def compatible_pcm(
    response: TTSAudioResponse, expected: TTSFormat
) -> AsyncGenerator[bytes]:
    """Append PCM only after the complete WAV format agrees."""
    source = response.data_gen
    buffer = bytearray()
    header_size = 0

    async def take(size: int) -> bytes:
        nonlocal header_size
        header_size += size
        if header_size > 65536:
            raise ValueError("fallback WAV header limit")
        while len(buffer) < size:
            buffer.extend(await anext(source))
        result = bytes(buffer[:size])
        del buffer[:size]
        return result

    try:
        if response.extension.lower() != "wav":
            raise ValueError("fallback format unavailable")
        header = await take(12)
        if header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError("invalid fallback WAV")
        audio_format = None
        while True:
            kind, size = struct.unpack("<4sI", await take(8))
            if kind == b"data":
                if audio_format != expected:
                    raise ValueError("fallback audio format mismatch")
                remaining = size
                break
            data = await take(size + (size % 2))
            if kind == b"fmt ":
                if audio_format is not None or size < 16:
                    raise ValueError("invalid fallback WAV format")
                codec, channels, rate, byte_rate, align, bits = struct.unpack(
                    "<HHIIHH", data[:16]
                )
                if (
                    codec != 1
                    or bits != 16
                    or align != channels * 2
                    or byte_rate != rate * align
                ):
                    raise ValueError("unsupported fallback WAV format")
                audio_format = TTSFormat("pcm_s16le", rate, channels, bits // 8)
        carry = b""
        streaming = remaining == 0xFFFFFFFF
        emitted = False
        while remaining:
            chunk = bytes(buffer[:remaining])
            del buffer[: len(chunk)]
            if not chunk:
                try:
                    chunk = (await anext(source))[:remaining]
                except StopAsyncIteration:
                    if not streaming:
                        raise ValueError("incomplete fallback WAV") from None
                    break
            if not streaming:
                remaining -= len(chunk)
            carry += chunk
            aligned = len(carry) - len(carry) % (expected.width * expected.channels)
            if aligned:
                emitted = True
                yield carry[:aligned]
                carry = carry[aligned:]
        if carry or not emitted:
            raise ValueError("incomplete fallback PCM")
    except StopAsyncIteration:
        raise ValueError("incomplete fallback WAV") from None
    finally:
        await source.aclose()
