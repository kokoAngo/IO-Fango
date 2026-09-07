"""Read-only S3 client for the upstream photo bucket.

Listing photos live in a Garage (S3-compatible) bucket on the office LAN.
Two things make this awkward and both are handled here:

* **Garage has no anonymous access**, so every GET must be SigV4-signed.
* **The URLs recorded upstream are stale.** ``baibai.images.storage_url``
  still points at ``http://localhost:9000/fango/...`` from a previous MinIO
  deployment that no longer exists — wrong host *and* wrong bucket. The
  durable identifier is ``storage_key`` (e.g.
  ``details/<bukken>/images/07_11.洗面室.jpg``), which is what this module
  takes; the endpoint and bucket come from config.

SigV4 is implemented here rather than pulling in boto3: this needs exactly
one operation (GET an object), the signing is ~40 lines of hmac, and the repo
already hand-rolls its dotenv parser rather than adding a dependency for a
small, stable thing.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import logging
from urllib.parse import quote

from ..config import load_s3_settings

log = logging.getLogger(__name__)

_ALGORITHM = "AWS4-HMAC-SHA256"
# sha256 of b"" — the payload hash for every GET.
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class ObjectStoreError(Exception):
    """A fetch failed. Carries ``status`` when the server answered."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k = _sign(f"AWS4{secret}".encode("utf-8"), datestamp)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


def _encode_path(path: str) -> str:
    """Percent-encode a path for the canonical request.

    S3 leaves the unreserved set (A-Z a-z 0-9 - _ . ~) alone and encodes
    everything else, per segment — which matters here because keys carry
    Japanese (``07_11.洗面室.jpg``). ``quote`` with an explicit safe of "/"
    gives exactly that, UTF-8 encoded.
    """
    return quote(path, safe="/~")


def build_auth_headers(
    *, method: str, host: str, path: str, access_key: str, secret_key: str,
    region: str, now: _dt.datetime, service: str = "s3",
) -> dict[str, str]:
    """SigV4 headers for an unsigned-payload request. Separated from the HTTP
    call so the signature is testable without a server."""
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{_EMPTY_SHA256}\n"
        f"x-amz-date:{amzdate}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join([
        method,
        _encode_path(path),
        "",                      # no query string
        canonical_headers,
        signed_headers,
        _EMPTY_SHA256,
    ])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        _ALGORITHM,
        amzdate,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(secret_key, datestamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "x-amz-date": amzdate,
        "x-amz-content-sha256": _EMPTY_SHA256,
        "Authorization": (
            f"{_ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


class ObjectStore:
    """Fetch objects by key. Read-only by construction — there is no put()."""

    def __init__(self, settings=None):
        self.settings = settings or load_s3_settings()

    def is_configured(self) -> bool:
        return self.settings.is_configured()

    def get(self, key: str, *, timeout: float = 30.0) -> bytes:
        """Fetch one object. Raises :class:`ObjectStoreError` on any failure."""
        import httpx

        st = self.settings
        if not st.is_configured():
            raise ObjectStoreError("object store not configured (FANGO_S3_* unset)")
        # Path-style addressing: Garage is reached by IP on the LAN, where
        # virtual-host style (bucket.host) has no DNS to resolve.
        path = f"/{st.bucket}/{key.lstrip('/')}"
        url = f"{st.endpoint.rstrip('/')}{_encode_path(path)}"
        host = st.endpoint.split("://", 1)[-1].rstrip("/")
        headers = build_auth_headers(
            method="GET", host=host, path=path,
            access_key=st.access_key_id, secret_key=st.secret_access_key,
            region=st.region, now=_dt.datetime.now(_dt.timezone.utc),
        )
        try:
            resp = httpx.get(url, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ObjectStoreError(f"{key}: {exc}") from exc
        if resp.status_code != 200:
            # The body carries Garage's XML error code; first 200 chars is
            # enough to tell AccessDenied from NoSuchKey without spamming logs.
            raise ObjectStoreError(
                f"{key}: HTTP {resp.status_code} {resp.text[:200]}",
                status=resp.status_code,
            )
        return resp.content
