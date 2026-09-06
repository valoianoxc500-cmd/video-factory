"""Subject-aware reframing of source photos to the video's aspect ratio.

Why this module exists
----------------------
Reframing used to happen twice, and the first pass destroyed the information
the second one needed. `image_sourcer` center-cropped every download to the
target size, so by the time `processor._fit_to_canvas` ran, the aspect ratio
already matched and its face-aware crop never fired. Every sourced photo was
effectively a blind center crop: a footballer standing left of frame got
cropped to the empty grass beside them.

Framing is decided once, here, at the moment a photo is normalized, and
`processor` reuses the same helper so the two passes cannot disagree.

What "subject aware" means here
-------------------------------
Centering the crop on a detected face is not enough. A 16:9 photo cropped to
9:16 keeps ~32% of its width; centering that window on a face fills the frame
with a head and slices the body off at the chin. Portrait framing wants the
subject's eyeline high in the frame with the body below it, so this module:

  * finds the subject (faces via YuNet first, then a saliency fallback that
    works on crowds, stadiums and documents),
  * centers the crop horizontally on the subject,
  * places the subject's center near `_EYELINE` from the top rather than at
    the middle, leaving headroom above and body below,
  * clamps the window inside the source so no edge padding is introduced.

Everything degrades safely: with no detection at all the result is the old
center crop, which is what the previous code always did.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger("video_factory")

# Where the subject's center should sit vertically in a portrait crop, as a
# fraction from the top. Classic headroom framing puts the eyeline near the
# upper third; 0.38 is slightly below that, which keeps a standing player's
# torso in frame instead of cropping at the neck.
_EYELINE = 0.38

# Below this share of the source retained, a cover crop discards so much of the
# frame that the subject is likely lost and a blurred surround is the better
# trade. A 16:9 photo into 9:16 retains ~0.32, so it still crops.
MIN_COVER_RETENTION = 0.25

# YuNet, the DNN face detector bundled with OpenCV's model zoo (~230 KB).
#
# It replaces the Haar cascades this code used to call. OpenCV 5 removed
# cv2.CascadeClassifier entirely, so those calls raised AttributeError on
# every photo -- and because the old detector swallowed bare Exceptions, the
# failure was invisible and every "face-aware" crop silently fell back to a
# center crop. Detection failures here are logged once rather than swallowed.
_FACE_MODEL = (
    Path(__file__).resolve().parent.parent
    / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
)
_FACE_SCORE_THRESHOLD = 0.6

_detector = None
_detector_failed = False


def _faces(bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Detect faces with YuNet. Returns (x, y, w, h) boxes in `bgr` pixels."""
    global _detector, _detector_failed
    if _detector_failed:
        return []
    h, w = bgr.shape[:2]
    if h < 20 or w < 20:
        return []
    try:
        if _detector is None:
            if not _FACE_MODEL.exists():
                _detector_failed = True
                logger.warning(
                    f"face model missing at {_FACE_MODEL}; framing will fall back "
                    "to saliency. Subjects may be less well centered."
                )
                return []
            _detector = cv2.FaceDetectorYN.create(
                str(_FACE_MODEL), "", (w, h), _FACE_SCORE_THRESHOLD
            )
        _detector.setInputSize((w, h))
        _, faces = _detector.detect(bgr)
        if faces is None:
            return []
        return [
            (int(f[0]), int(f[1]), int(f[2]), int(f[3]))
            for f in faces
            if f[2] > 0 and f[3] > 0
        ]
    except Exception as exc:
        _detector_failed = True
        logger.warning(f"face detection unavailable, using saliency: {exc}")
        return []


# A face smaller than this share of the largest one is a bystander, not the
# subject. Without this cut, a stadium crowd's many small faces outvote the
# player in the foreground and drag the crop off them entirely.
_SUBJECT_FACE_AREA_RATIO = 0.45


def _dominant_faces(
    boxes: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Keep only faces comparable in size to the largest one."""
    if not boxes:
        return []
    largest = max(bw * bh for _, _, bw, bh in boxes)
    if largest <= 0:
        return []
    return [
        b for b in boxes if (b[2] * b[3]) >= largest * _SUBJECT_FACE_AREA_RATIO
    ]


def _salient_center(gray: np.ndarray) -> tuple[float, float] | None:
    """Fallback focus point: the centroid of high-frequency detail.

    Stadiums, crowds, trophies and documents have no face and no person, but
    their subject is still not usually dead center. Edge density is a cheap,
    dependency-free proxy for where the content actually is.
    """
    try:
        edges = cv2.Canny(gray, 60, 180)
        if edges.sum() == 0:
            return None
        ys, xs = np.nonzero(edges)
        # Trim outliers so a bright sideline advert cannot drag the centroid.
        x = float(np.clip(np.mean(xs), np.percentile(xs, 5), np.percentile(xs, 95)))
        y = float(np.clip(np.mean(ys), np.percentile(ys, 5), np.percentile(ys, 95)))
        return x, y
    except Exception:
        return None


def find_subject(img: Image.Image) -> tuple[tuple[float, float], str]:
    """Locate the subject. Returns ((x, y) in pixels, detector name).

    Falls back to the geometric center, which reproduces the old behaviour.
    """
    w, h = img.size
    default = ((w / 2.0, h / 2.0), "center")
    try:
        arr = np.array(img)
        # Detection runs on a downscaled copy; full-resolution news photos are
        # slow to scan and the extra precision does not change the crop.
        scale = max(1, min(arr.shape[:2]) // 500)
        small = cv2.resize(arr, None, fx=1 / scale, fy=1 / scale) if scale > 1 else arr
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)

        boxes = _dominant_faces(_faces(cv2.cvtColor(small, cv2.COLOR_RGB2BGR)))
        if boxes:
            # Area-weighted across the dominant faces only, so a two-player
            # duel still frames both but a crowd behind one player does not
            # pull the crop into the stands.
            total = sum(bw * bh for _, _, bw, bh in boxes) or 1
            cx = sum((bx + bw / 2) * bw * bh for bx, _, bw, bh in boxes) / total
            cy = sum((by + bh / 2) * bw * bh for _, by, bw, bh in boxes) / total
            return (cx * scale, cy * scale), "face"

        salient = _salient_center(gray)
        if salient is not None:
            return (salient[0] * scale, salient[1] * scale), "saliency"
    except Exception as exc:
        logger.debug(f"subject detection failed, using center: {exc}")
    return default


def subject_aware_fit(
    img: Image.Image,
    target_size: tuple[int, int],
) -> Image.Image:
    """Fill `target_size` by cropping toward the subject, with headroom.

    Sources too extreme to crop without losing the subject (wide panoramas)
    keep the whole photo over a blurred surround, as before.
    """
    target_w, target_h = target_size
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return img.resize(target_size, Image.Resampling.LANCZOS)

    src_ratio = src_w / src_h
    target_ratio = target_w / target_h

    if abs(src_ratio - target_ratio) < 0.01:
        return img.resize(target_size, Image.Resampling.LANCZOS)

    retention = min(src_ratio, target_ratio) / max(src_ratio, target_ratio)
    if retention < MIN_COVER_RETENTION:
        return _blurred_surround(img, target_size)

    (sx, sy), detector = find_subject(img)

    # Largest window of the target ratio that fits inside the source.
    if src_ratio > target_ratio:
        crop_w = int(round(src_h * target_ratio))
        crop_h = src_h
    else:
        crop_w = src_w
        crop_h = int(round(src_w / target_ratio))
    crop_w = max(1, min(crop_w, src_w))
    crop_h = max(1, min(crop_h, src_h))

    left = sx - crop_w / 2.0

    # Vertical placement: put the subject at the eyeline rather than the middle,
    # so a standing figure keeps headroom above and body below. Only meaningful
    # when there is vertical slack and we actually found a subject.
    if detector == "face" and crop_h < src_h:
        top = sy - crop_h * _EYELINE
    else:
        top = sy - crop_h / 2.0

    left = int(round(max(0.0, min(left, src_w - crop_w))))
    top = int(round(max(0.0, min(top, src_h - crop_h))))

    cropped = img.crop((left, top, left + crop_w, top + crop_h))
    return cropped.resize(target_size, Image.Resampling.LANCZOS)


def _blurred_surround(
    img: Image.Image,
    target_size: tuple[int, int],
) -> Image.Image:
    target_w, target_h = target_size
    background = ImageOps.fit(
        img, (target_w, target_h), method=Image.Resampling.LANCZOS
    ).filter(ImageFilter.GaussianBlur(24))
    foreground = ImageOps.contain(
        img, (target_w, target_h), method=Image.Resampling.LANCZOS
    )
    background.paste(
        foreground,
        ((target_w - foreground.width) // 2, (target_h - foreground.height) // 2),
    )
    return background


def verticality_score(width: int, height: int, target_size: tuple[int, int]) -> float:
    """0..1 rating of how little of a source is thrown away to reach target.

    Used to rank search candidates: a portrait source scores near 1.0, a 16:9
    source about 0.32, a panorama near 0. Ranking on this makes the sourcer
    prefer photos that survive the crop with the subject intact.
    """
    if width <= 0 or height <= 0:
        return 0.0
    src_ratio = width / height
    target_ratio = target_size[0] / target_size[1]
    return min(src_ratio, target_ratio) / max(src_ratio, target_ratio)
