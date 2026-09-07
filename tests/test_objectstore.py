"""SigV4 signing for the upstream photo bucket.

The live proof is that a real object fetches; these lock the parts that fail
silently rather than loudly — path encoding (keys carry Japanese), the
canonical header set, and the unsigned-payload hash.
"""
from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from fango.listings import objectstore as osx

NOW = dt.datetime(2026, 9, 4, 8, 30, 0, tzinfo=dt.timezone.utc)
ARGS = dict(
    method="GET", host="172.31.212.36:3900", access_key="GKtest",
    secret_key="secrettest", region="garage", now=NOW,
)


def test_empty_payload_hash_is_sha256_of_nothing():
    assert osx._EMPTY_SHA256 == hashlib.sha256(b"").hexdigest()


def test_headers_shape():
    h = osx.build_auth_headers(path="/bucket/key.jpg", **ARGS)
    assert h["x-amz-date"] == "20260904T083000Z"
    assert h["x-amz-content-sha256"] == osx._EMPTY_SHA256
    assert h["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=GKtest/20260904/garage/s3/aws4_request")
    # The three headers we send are exactly the three we sign.
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in h["Authorization"]


def test_signature_is_deterministic_and_input_sensitive():
    base = osx.build_auth_headers(path="/bucket/key.jpg", **ARGS)["Authorization"]
    assert osx.build_auth_headers(path="/bucket/key.jpg", **ARGS)["Authorization"] == base
    # Any signed input changing must change the signature.
    assert osx.build_auth_headers(path="/bucket/other.jpg", **ARGS)["Authorization"] != base
    assert osx.build_auth_headers(
        path="/bucket/key.jpg", **{**ARGS, "host": "elsewhere:3900"}
    )["Authorization"] != base
    assert osx.build_auth_headers(
        path="/bucket/key.jpg", **{**ARGS, "secret_key": "other"}
    )["Authorization"] != base


def test_path_encoding_handles_japanese_keys():
    """Real keys look like details/<id>/images/07_11.洗面室.jpg — an unencoded
    multibyte path is a signature mismatch, i.e. a 403 on every such photo."""
    enc = osx._encode_path("/fango-baibai/details/1/images/07_11.洗面室.jpg")
    assert enc.startswith("/fango-baibai/details/1/images/07_11.")
    assert "洗面室" not in enc
    assert "%E6%B4%97%E9%9D%A2%E5%AE%A4" in enc
    # Separators survive; unreserved characters are left alone.
    assert enc.count("/") == 5
    assert "07_11." in enc


def test_path_encoding_leaves_unreserved_alone():
    assert osx._encode_path("/b/a-_.~z/A9.jpg") == "/b/a-_.~z/A9.jpg"
    assert osx._encode_path("/b/a b.jpg") == "/b/a%20b.jpg"


def test_unconfigured_store_refuses_to_fetch(monkeypatch):
    for var in ("FANGO_S3_ENDPOINT", "FANGO_S3_BUCKET",
                "FANGO_S3_ACCESS_KEY_ID", "FANGO_S3_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    store = osx.ObjectStore()
    assert store.is_configured() is False
    with pytest.raises(osx.ObjectStoreError):
        store.get("details/1/images/a.jpg")


def test_get_surfaces_the_server_status(monkeypatch):
    import httpx
    from fango.config import S3Settings

    st = S3Settings(endpoint="http://host:3900", bucket="b", region="garage",
                    access_key_id="GKx", secret_access_key="s")
    store = osx.ObjectStore(st)

    captured = {}

    class _Resp:
        status_code = 403
        text = "<Error><Code>AccessDenied</Code></Error>"
        content = b""

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(osx.ObjectStoreError) as exc:
        store.get("details/1/images/a.jpg")
    assert exc.value.status == 403
    assert "AccessDenied" in str(exc.value)
    # Path-style addressing: the bucket is in the path, not the hostname —
    # Garage is reached by IP here, so vhost style has no DNS to resolve.
    assert captured["url"] == "http://host:3900/b/details/1/images/a.jpg"
    assert "Authorization" in captured["headers"]
