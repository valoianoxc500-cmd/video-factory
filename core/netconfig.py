"""Outbound TLS bootstrap for machines behind an inspecting proxy.

Python's `ssl` module and gRPC both ship their own CA bundles (certifi /
BoringSSL roots) and therefore fail with CERTIFICATE_VERIFY_FAILED on hosts
where an endpoint-security product re-signs TLS with a locally trusted root.
That breaks every provider call in the pipeline: Serper, Pexels, image
downloads (httpx) and Speech-to-Text (gRPC).

This module makes both stacks trust the operating system's certificate store:

* `ssl` / httpx  - via the optional `truststore` package.
* gRPC           - via GRPC_DEFAULT_SSL_ROOTS_FILE_PATH pointing at a PEM
                   bundle exported from the Windows root stores.

Both steps are best-effort and no-ops on machines that don't need them, so
importing this module is always safe. It is imported from `settings`, which
every entry point already loads.
"""

import logging
import os
import ssl
import sys
from pathlib import Path

logger = logging.getLogger("video_factory")

_ROOT_BUNDLE_NAME = "system_roots.pem"
_ROOT_BUNDLE_MAX_AGE_DAYS = 7

_configured = False


def _install_system_trust_for_ssl() -> bool:
    """Route Python's ssl module at the OS certificate store."""
    try:
        import truststore
    except ImportError:
        logger.debug("truststore not installed; using bundled CA roots")
        return False
    try:
        truststore.inject_into_ssl()
        return True
    except Exception as e:
        logger.debug(f"truststore injection skipped: {e}")
        return False


def _export_windows_root_bundle(destination: Path) -> bool:
    """Write every trusted Windows root/intermediate CA to a PEM bundle."""
    try:
        pem_chunks: list[str] = []
        seen: set[bytes] = set()
        for store in ("ROOT", "CA"):
            for cert_bytes, encoding, _trust in ssl.enum_certificates(store):
                if encoding != "x509_asn" or cert_bytes in seen:
                    continue
                seen.add(cert_bytes)
                pem_chunks.append(ssl.DER_cert_to_PEM_cert(cert_bytes))
        if not pem_chunks:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("".join(pem_chunks), encoding="ascii")
        logger.debug(f"Exported {len(pem_chunks)} system CA certs to {destination}")
        return True
    except Exception as e:
        logger.debug(f"System CA export skipped: {e}")
        return False


def _bundle_is_fresh(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    import time
    age_days = (time.time() - path.stat().st_mtime) / 86400
    return age_days < _ROOT_BUNDLE_MAX_AGE_DAYS


def _install_system_trust_for_grpc(cache_dir: Path) -> bool:
    """Point gRPC at an OS-derived CA bundle (Windows only).

    Respects an operator-provided GRPC_DEFAULT_SSL_ROOTS_FILE_PATH.
    """
    if os.environ.get("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"):
        return True
    if sys.platform != "win32" or not hasattr(ssl, "enum_certificates"):
        return False

    bundle = cache_dir / _ROOT_BUNDLE_NAME
    if not _bundle_is_fresh(bundle) and not _export_windows_root_bundle(bundle):
        return False
    if not bundle.exists():
        return False

    os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = str(bundle)
    return True


def configure_outbound_tls(cache_dir: Path) -> dict[str, bool]:
    """Idempotently configure OS trust for the ssl module and gRPC."""
    global _configured
    if _configured:
        return {"ssl": True, "grpc": True}
    result = {
        "ssl": _install_system_trust_for_ssl(),
        "grpc": _install_system_trust_for_grpc(cache_dir),
    }
    _configured = True
    return result
