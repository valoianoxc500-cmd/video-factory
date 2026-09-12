"""Telling clips apart, and telling when two of them are the same shot.

Shared by AI Video Maker and Police Chase Studio. Both have the same failure:
a finished video that shows the same drone-over-a-city shot three times, or
the same crash from two moments a second apart. Neither is caught by comparing
URLs -- stock libraries carry the same footage under different ids, and two
moments of one source file are different byte ranges of an identical scene.

So similarity is judged on what the frames *look like*:

  * a perceptual hash per sampled frame (difference hash -- robust to
    re-encoding, resolution and compression, which is exactly what separates
    it from an md5 of the bytes);
  * a clip signature that is the set of its frame hashes;
  * Hamming distance between signatures, with a threshold tuned so
    "same shot, different crop" collides and "two different sunsets" does not.

Everything here is local, deterministic and costs nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("medialab")

__all__ = [
    "ClipSignature", "sample_frames", "frames_at", "signature_for",
    "signature_between", "is_near_duplicate", "DuplicateGuard", "HASH_SIZE",
    "NEAR_DUPLICATE_BITS",
]

#: 8x8 difference hash -> 64 bits. Big enough to separate real scenes, small
#: enough that a re-encode does not move it.
HASH_SIZE = 8

#: Hamming distance at or below which two frames are "the same shot". Measured
#: on stock footage: re-encodes of one clip land at 0-4 bits, different takes
#: of a similar subject at 12-20, unrelated scenes above 25.
NEAR_DUPLICATE_BITS = 10

#: Frames sampled per clip. Three is enough to catch a clip that opens like
#: another and then diverges, without paying to decode the whole file.
FRAMES_PER_CLIP = 3


@dataclass
class ClipSignature:
    """What one clip looks like, as a handful of perceptual hashes."""

    path: Path
    hashes: list[int] = field(default_factory=list)
    width: int = 0
    height: int = 0
    duration: float = 0.0

    @property
    def usable(self) -> bool:
        return bool(self.hashes)


def _dhash(image, size: int = HASH_SIZE) -> int:
    """Difference hash, as an int. Falls back to a local implementation.

    `imagehash` is the reference, but a missing optional dependency must not
    take dedup out entirely -- without it every clip would look unique and the
    repeats this module exists to stop would come straight back.
    """
    try:
        import imagehash

        return int(str(imagehash.dhash(image, hash_size=size)), 16)
    except Exception:
        grey = image.convert("L").resize((size + 1, size), 3)
        pixels = list(grey.getdata())
        bits = 0
        for row in range(size):
            offset = row * (size + 1)
            for col in range(size):
                bits = (bits << 1) | int(
                    pixels[offset + col] > pixels[offset + col + 1]
                )
        return bits


def sample_frames(video: Path, count: int = FRAMES_PER_CLIP) -> list:
    """Evenly spaced PIL frames from a video, skipping the very edges.

    The first and last frames are avoided: stock clips often open or close on
    a fade, and a black frame hashes the same for every clip in the library.
    """
    try:
        import cv2
        from PIL import Image
    except Exception:
        return []

    capture = cv2.VideoCapture(str(video))
    try:
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            return []
        frames = []
        for index in range(count):
            position = int(total * (index + 1) / (count + 1))
            capture.set(cv2.CAP_PROP_POS_FRAMES, position)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            frames.append(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        return frames
    except Exception as exc:
        logger.debug(f"[medialab] frame sampling failed for {video.name}: {exc}")
        return []
    finally:
        capture.release()


def frames_at(video: Path, seconds: list[float]) -> list:
    """PIL frames at specific timestamps.

    `sample_frames` covers a whole file, which is the right question for a
    stock clip. Police Chase Studio asks a different one: two candidate
    moments are two ranges of the *same* file, so comparing them means
    hashing where they actually are.
    """
    try:
        import cv2
        from PIL import Image
    except Exception:
        return []

    capture = cv2.VideoCapture(str(video))
    try:
        frames = []
        for at in seconds:
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(at)) * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            frames.append(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        return frames
    except Exception as exc:
        logger.debug(f"[medialab] timed frame grab failed for {video.name}: {exc}")
        return []
    finally:
        capture.release()


def _signature(video: Path, frames: list, duration: float = 0.0) -> ClipSignature:
    if not frames:
        return ClipSignature(path=video, duration=duration)
    width, height = frames[0].size
    return ClipSignature(
        path=video,
        hashes=[_dhash(frame) for frame in frames],
        width=width,
        height=height,
        duration=duration,
    )


def signature_for(video: Path, count: int = FRAMES_PER_CLIP) -> ClipSignature:
    return _signature(video, sample_frames(video, count))


def signature_between(
    video: Path, start: float, end: float, count: int = FRAMES_PER_CLIP
) -> ClipSignature:
    """The signature of one span of a longer video."""
    span = max(0.0, float(end) - float(start))
    stamps = [float(start) + span * (i + 1) / (count + 1) for i in range(count)]
    return _signature(video, frames_at(video, stamps), duration=span)


def _distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_near_duplicate(
    left: ClipSignature, right: ClipSignature, *, bits: int = NEAR_DUPLICATE_BITS
) -> bool:
    """Whether two clips would read as the same shot on screen.

    Any frame pair matching is enough. Two clips that share one identical
    frame are almost always the same footage trimmed differently, and showing
    both is the repeat a viewer notices.
    """
    if not left.usable or not right.usable:
        return False
    return any(
        _distance(a, b) <= bits for a in left.hashes for b in right.hashes
    )


class DuplicateGuard:
    """Remembers what has already been chosen, and refuses more of the same.

    Two rejections, because they catch different things: the exact source
    (same provider asset reused for two beats) and the visual signature (the
    same stock shot sold under two ids, or two near-identical takes).
    """

    def __init__(self, *, bits: int = NEAR_DUPLICATE_BITS) -> None:
        self.bits = bits
        self._signatures: list[ClipSignature] = []
        self._sources: set[str] = set()

    def seen_source(self, source: str) -> bool:
        key = (source or "").strip().lower()
        return bool(key) and key in self._sources

    def rejects(self, signature: ClipSignature) -> str:
        """Why this clip cannot be used, or "" if it can."""
        for existing in self._signatures:
            if is_near_duplicate(existing, signature, bits=self.bits):
                return f"visually near-identical to {existing.path.name}"
        return ""

    def accept(self, signature: ClipSignature, source: str = "") -> None:
        self._signatures.append(signature)
        key = (source or "").strip().lower()
        if key:
            self._sources.add(key)

    def __len__(self) -> int:
        return len(self._signatures)
