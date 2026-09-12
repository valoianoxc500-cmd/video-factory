"""Shared media analysis for AI Video Maker and Police Chase Studio.

Deliberately owned by neither product. Both had the same three defects --
repeated shots, cuts landing mid-scene, and Arabic captions that rendered
backwards and disconnected -- and a fix that lived inside one product would
have left the other broken.

Nothing here imports from `aivideo/`, `viral/` or `core/`, so it cannot couple
the products to each other, and every dependency it uses (PySceneDetect,
imagehash, arabic_reshaper, python-bidi) degrades to a documented fallback
rather than raising.
"""

from medialab import arabic, fingerprint, shots  # noqa: F401

__all__ = ["arabic", "fingerprint", "shots"]
