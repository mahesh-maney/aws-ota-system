"""
Secure artifact downloader.
Downloads from a pre-signed S3 URL, streams to disk, verifies SHA256 and ECDSA signature.
Retries up to MAX_DOWNLOAD_RETRIES times on transient network failures, discarding
any partial file before each retry so a corrupt partial download is never installed.
"""
import hashlib
import logging
import os
import time

import requests
from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError as RequestsConnectionError,
    ReadTimeout,
    Timeout,
)

logger = logging.getLogger(__name__)

# 3 attempts total: wait 30 s then 60 s between them
MAX_DOWNLOAD_RETRIES = 3
_RETRY_DELAYS_SEC = [30, 60]

# Only these errors are retried — a 403/404 or bad signature is never retried
_RETRYABLE_ERRORS = (RequestsConnectionError, ChunkedEncodingError, ReadTimeout, Timeout)


def download_and_verify(
    presigned_url: str,
    expected_sha256: str,
    expected_signature: str,
    dest_path: str,
    public_key_path: str,
) -> None:
    """
    Downloads artifact from presigned_url to dest_path.
    Retries up to MAX_DOWNLOAD_RETRIES times on transient network failures.
    Verifies SHA256 hash and ECDSA signature — never installs a bad artifact.
    """
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    tmp_path = dest_path + ".tmp"
    _download_with_retry(presigned_url, tmp_path)

    try:
        _verify_sha256(tmp_path, expected_sha256)
        _verify_signature(expected_sha256, expected_signature, public_key_path)
        # Atomic move only after both checks pass
        os.replace(tmp_path, dest_path)
        logger.info(f"Artifact downloaded and verified: {dest_path}")
    except Exception:
        _remove_if_exists(tmp_path)
        raise


def _download_with_retry(url: str, tmp_path: str) -> None:
    """Attempt the download up to MAX_DOWNLOAD_RETRIES times.
    Any partial .tmp file is deleted before each attempt so we always
    start clean — a truncated file would fail SHA256 anyway."""
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        _remove_if_exists(tmp_path)
        try:
            _stream_download(url, tmp_path)
            return  # success
        except _RETRYABLE_ERRORS as exc:
            _remove_if_exists(tmp_path)
            if attempt == MAX_DOWNLOAD_RETRIES:
                raise RuntimeError(
                    f"Download failed after {MAX_DOWNLOAD_RETRIES} attempts. "
                    f"Last error: {type(exc).__name__}: {exc}"
                ) from exc
            delay = _RETRY_DELAYS_SEC[attempt - 1]
            logger.warning(
                f"Download attempt {attempt}/{MAX_DOWNLOAD_RETRIES} failed "
                f"({type(exc).__name__}). Retrying in {delay}s..."
            )
            time.sleep(delay)
        except Exception:
            # Non-retryable (e.g. HTTP 403 expired URL, 404) — fail immediately
            _remove_if_exists(tmp_path)
            raise


def _remove_if_exists(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _stream_download(url: str, dest: str) -> None:
    logger.debug("Opening HTTP stream for artifact download")
    response = requests.get(url, stream=True, timeout=300)
    response.raise_for_status()

    total      = int(response.headers.get("content-length", 0))
    downloaded = 0
    last_logged_pct = -1
    t0 = time.time()

    logger.info(f"Download started — expected size: {total} bytes ({total // 1024 // 1024} MB)")

    with open(dest, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = int(downloaded / total * 100)
                # Log every 10% so large downloads are visible without flooding logs
                if pct >= last_logged_pct + 10:
                    elapsed = time.time() - t0
                    rate_kb = int(downloaded / 1024 / max(elapsed, 0.1))
                    logger.info(f"Download progress: {pct}% ({downloaded // 1024} KB at {rate_kb} KB/s)")
                    last_logged_pct = pct

    elapsed = time.time() - t0
    rate_kb = int(downloaded / 1024 / max(elapsed, 0.1))
    logger.info(f"Download complete — {downloaded} bytes in {elapsed:.1f}s ({rate_kb} KB/s), saved to {dest}")


def _verify_sha256(file_path: str, expected: str) -> None:
    logger.info(f"Verifying SHA256 of {file_path}")
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)
    actual = sha256.hexdigest()
    if actual != expected:
        logger.error(f"SHA256 MISMATCH — expected={expected} got={actual}")
        raise ValueError(f"SHA256 mismatch — expected: {expected}, got: {actual}")
    logger.info(f"SHA256 verified OK: {actual}")


def _verify_signature(sha256_hex: str, signature_b64: str, public_key_path: str) -> None:
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.exceptions import InvalidSignature

    logger.info(f"Verifying ECDSA signature using public key at {public_key_path}")
    logger.debug(f"SHA256 being verified: {sha256_hex}")

    with open(public_key_path, "rb") as f:
        public_key = serialization.load_pem_public_key(f.read())

    signature_bytes = base64.b64decode(signature_b64)
    try:
        public_key.verify(signature_bytes, sha256_hex.encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        logger.error("ECDSA signature verification FAILED — artifact may be tampered or key mismatch")
        raise ValueError("ECDSA signature verification failed — artifact may be tampered")

    logger.info("ECDSA signature verified OK")
