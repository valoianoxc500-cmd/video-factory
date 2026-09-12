"""Whether an asset may actually be used, and what has to travel with it.

The rule this module exists to enforce: **publicly reachable is not reusable.**
A Commons search returns fair-use screenshots alongside public-domain
photographs; the Internet Archive hosts material uploaded with no rights
statement at all; Openverse indexes everything Creative Commons including the
non-commercial and no-derivatives variants. None of that is visible from the
image itself, and all of it looks identical once it is a frame in a video.

So an asset only reaches the renderer if its licence resolves to something on
the allow-list, and it carries source, creator, page and licence with it when
it does.

Two exclusions are deliberate and worth stating:

  **NonCommercial.** The product sells its output. NC is not usable here at
  any point, and there is no flag to turn that off in production.

  **ShareAlike.** SA would extend its terms to the finished video, which is a
  composite of a dozen sources. That is not a licence a customer can be handed
  without being told. It is rejected when commercial reuse is required, and
  the constant below is the single place to revisit if that policy changes.

  **NoDerivatives.** The pipeline crops to 9:16, cuts to length and burns
  captions over the top. Every one of those is a derivative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from medialab.media.types import MediaAsset

__all__ = [
    "LicenseTerms", "normalise", "evaluate", "filter_usable",
    "ALLOW_SHARE_ALIKE", "KNOWN",
]

#: ShareAlike would propagate to the finished video. Flip only with a decision
#: about what licence the customer's output is then under.
ALLOW_SHARE_ALIKE = False


@dataclass(frozen=True)
class LicenseTerms:
    id: str
    label: str
    url: str = ""
    commercial: bool = False
    derivatives: bool = False
    share_alike: bool = False
    attribution: bool = True


def _cc(code: str, label: str, *, commercial=True, derivatives=True, sa=False) -> LicenseTerms:
    version = "4.0"
    return LicenseTerms(
        id=code, label=label,
        url=f"https://creativecommons.org/licenses/{code[3:]}/{version}/",
        commercial=commercial, derivatives=derivatives, share_alike=sa,
        attribution=True,
    )


#: Everything the adapters can produce. An id absent from here is unknown, and
#: unknown is refused -- guessing is exactly the failure this module prevents.
KNOWN: dict[str, LicenseTerms] = {
    "cc0": LicenseTerms(
        "cc0", "CC0 1.0 Public Domain Dedication",
        "https://creativecommons.org/publicdomain/zero/1.0/",
        commercial=True, derivatives=True, attribution=False,
    ),
    "public-domain": LicenseTerms(
        "public-domain", "Public domain",
        "https://en.wikipedia.org/wiki/Public_domain",
        commercial=True, derivatives=True, attribution=False,
    ),
    "pdm": LicenseTerms(
        "pdm", "Public Domain Mark 1.0",
        "https://creativecommons.org/publicdomain/mark/1.0/",
        commercial=True, derivatives=True, attribution=False,
    ),
    "usgov": LicenseTerms(
        "usgov", "U.S. Government work, public domain",
        "https://www.usa.gov/government-works",
        commercial=True, derivatives=True, attribution=False,
    ),
    "cc-by": _cc("cc-by", "CC BY 4.0"),
    "cc-by-sa": _cc("cc-by-sa", "CC BY-SA 4.0", sa=True),
    "cc-by-nd": _cc("cc-by-nd", "CC BY-ND 4.0", derivatives=False),
    "cc-by-nc": _cc("cc-by-nc", "CC BY-NC 4.0", commercial=False),
    "cc-by-nc-sa": _cc("cc-by-nc-sa", "CC BY-NC-SA 4.0", commercial=False, sa=True),
    "cc-by-nc-nd": _cc("cc-by-nc-nd", "CC BY-NC-ND 4.0", commercial=False, derivatives=False),
    # Bespoke stock licences. Each permits commercial use and modification
    # without attribution; each forbids reselling the asset itself, which is
    # not something this pipeline does.
    "pexels": LicenseTerms(
        "pexels", "Pexels License", "https://www.pexels.com/license/",
        commercial=True, derivatives=True, attribution=False,
    ),
    "pixabay": LicenseTerms(
        "pixabay", "Pixabay Content License",
        "https://pixabay.com/service/license-summary/",
        commercial=True, derivatives=True, attribution=False,
    ),
    "unsplash": LicenseTerms(
        "unsplash", "Unsplash License", "https://unsplash.com/license",
        commercial=True, derivatives=True, attribution=False,
    ),
    # Explicitly not usable, but named so the reason is reportable rather
    # than "unknown".
    "rights-reserved": LicenseTerms(
        "rights-reserved", "All rights reserved", "", commercial=False,
        derivatives=False,
    ),
}

#: Raw licence strings seen in the wild -> our ids. Matched after lowercasing
#: and stripping, longest key first so "cc-by-nc-sa" wins over "cc-by".
_ALIASES = {
    "cc0": "cc0", "zero": "cc0", "cc-zero": "cc0", "cc0-1.0": "cc0",
    "publicdomain": "public-domain", "public domain": "public-domain",
    "pd": "public-domain", "pdm": "pdm", "public domain mark": "pdm",
    "no known copyright": "pdm", "nokc": "pdm",
    "usgov": "usgov", "us government work": "usgov",
    "pd-usgov": "usgov", "pd-usgov-nasa": "usgov", "pd-usgov-noaa": "usgov",
    "attribution": "cc-by",
    "attribution-sharealike": "cc-by-sa",
    "attribution-noderivs": "cc-by-nd",
    "attribution-noncommercial": "cc-by-nc",
    "cc-by": "cc-by", "cc-by-sa": "cc-by-sa", "cc-by-nd": "cc-by-nd",
    "cc-by-nc": "cc-by-nc", "cc-by-nc-sa": "cc-by-nc-sa",
    "cc-by-nc-nd": "cc-by-nc-nd",
    "by": "cc-by", "by-sa": "cc-by-sa", "by-nd": "cc-by-nd",
    "by-nc": "cc-by-nc", "by-nc-sa": "cc-by-nc-sa", "by-nc-nd": "cc-by-nc-nd",
    "pexels": "pexels", "pixabay": "pixabay", "unsplash": "unsplash",
    "all rights reserved": "rights-reserved", "copyrighted": "rights-reserved",
    "fair use": "rights-reserved", "fairuse": "rights-reserved",
}


def normalise(raw: str, *, version: str = "") -> str:
    """A licence string from any provider -> one of `KNOWN`, or "".

    Returns "" for anything unrecognised. That is the safe answer: an empty id
    fails `evaluate`, so an unfamiliar licence is refused rather than assumed
    to be permissive.
    """
    text = " ".join(str(raw or "").strip().lower().split())
    if not text:
        return ""
    if text in _ALIASES:
        return _ALIASES[text]

    # A licence URL is the most reliable form; read the code out of the path.
    url_match = re.search(
        r"creativecommons\.org/(licenses|publicdomain)/([a-z\-]+)/", text
    )
    if url_match:
        kind, code = url_match.group(1), url_match.group(2)
        if kind == "publicdomain":
            return "cc0" if code == "zero" else "pdm"
        return _ALIASES.get(code, "")

    # Otherwise take the longest alias appearing in the text, so "Creative
    # Commons Attribution-NonCommercial 4.0" resolves to cc-by-nc and not to
    # the cc-by that is also a substring of it.
    hit, best = "", ""
    for alias, mapped in _ALIASES.items():
        if alias in text and len(alias) > len(hit):
            hit, best = alias, mapped
    return best


def evaluate(asset: MediaAsset, *, commercial: bool = True) -> tuple[bool, str]:
    """(usable, reason). The reason is for the log, never for the customer."""
    terms = KNOWN.get(asset.license or "")
    if terms is None:
        return False, f"no established reuse basis (licence {asset.license or 'unknown'!r})"
    if commercial and not terms.commercial:
        return False, f"{terms.label} forbids commercial use"
    if not terms.derivatives:
        return False, f"{terms.label} forbids the crop, cut and captioning this does"
    if terms.share_alike and commercial and not ALLOW_SHARE_ALIKE:
        return False, f"{terms.label} would extend share-alike to the finished video"
    if terms.attribution and not (asset.creator or asset.attribution):
        return False, f"{terms.label} requires attribution and none was supplied"
    return True, terms.label


def filter_usable(
    assets: list[MediaAsset], *, commercial: bool = True
) -> tuple[list[MediaAsset], list[tuple[MediaAsset, str]]]:
    """Split candidates into usable and refused-with-a-reason."""
    kept: list[MediaAsset] = []
    refused: list[tuple[MediaAsset, str]] = []
    for asset in assets:
        ok, reason = evaluate(asset, commercial=commercial)
        if ok:
            terms = KNOWN[asset.license]
            asset.commercial_use_allowed = terms.commercial
            if not asset.license_url:
                asset.license_url = terms.url
            if terms.attribution and not asset.attribution:
                asset.attribution = _credit(asset, terms)
            kept.append(asset)
        else:
            refused.append((asset, reason))
    return kept, refused


def _credit(asset: MediaAsset, terms: LicenseTerms) -> str:
    parts = [asset.title or asset.asset_id]
    if asset.creator:
        parts.append(f"by {asset.creator}")
    parts.append(f"({terms.label})")
    if asset.original_url:
        parts.append(asset.original_url)
    return " ".join(parts)
