"""Post image attachments — service + MCP tool + URL validation + SSR."""
from __future__ import annotations

import pytest

from fango import forum_core
from fango.forum_core import ForumError, attach_image, list_attachments


@pytest.fixture
def a_post(tmp_db, agent_factory):
    """A baibai thread + return its opening post id."""
    from fango.baibai import service as bb
    ag, _ = agent_factory()
    out = bb.create_thread(title="t", body="b", author_id=ag.id, agent_created_at=ag.created_at)
    return out["post"].id


# ---------- URL validation ----------------------------------------------

class TestUrlValidation:
    def test_https_allowed_host_ok(self, a_post):
        att_id = attach_image(a_post, "https://fango.io.ngrok.app/listings/img/1/raw/1.jpg")
        assert att_id > 0

    def test_imgur_allowed(self, a_post):
        att_id = attach_image(a_post, "https://i.imgur.com/abc.jpg")
        assert att_id > 0

    def test_subdomain_match(self, a_post):
        att_id = attach_image(a_post, "https://random-sub.ngrok-free.app/x.jpg")
        assert att_id > 0

    def test_http_outside_localhost_rejected(self, a_post):
        with pytest.raises(ForumError, match="https"):
            attach_image(a_post, "http://fango.io.ngrok.app/x.jpg")

    def test_localhost_http_allowed(self, a_post):
        # Dev convenience: http://localhost is allowed.
        att_id = attach_image(a_post, "http://localhost:8000/x.jpg")
        assert att_id > 0

    def test_arbitrary_host_rejected(self, a_post):
        with pytest.raises(ForumError, match="not allowed"):
            attach_image(a_post, "https://evil.example.com/x.jpg")

    def test_oversize_url_rejected(self, a_post):
        url = "https://fango.io.ngrok.app/" + "a" * 3000
        with pytest.raises(ForumError, match="longer than"):
            attach_image(a_post, url)

    def test_empty_url_rejected(self, a_post):
        with pytest.raises(ForumError, match="empty"):
            attach_image(a_post, "")

    def test_bad_scheme_rejected(self, a_post):
        with pytest.raises(ForumError, match="http or https"):
            attach_image(a_post, "javascript:alert(1)")

    def test_env_override_allows_new_host(self, a_post, monkeypatch):
        monkeypatch.setenv("FANGO_ATTACHMENT_HOSTS", "example.com")
        att_id = attach_image(a_post, "https://example.com/x.jpg")
        assert att_id > 0
        # And previously-allowed hosts are now blocked.
        with pytest.raises(ForumError, match="not allowed"):
            attach_image(a_post, "https://i.imgur.com/y.jpg")


# ---------- Business rules ----------------------------------------------

class TestAttachmentRules:
    def test_unknown_post_rejected(self, tmp_db):
        with pytest.raises(ForumError, match="not found"):
            attach_image(99999, "https://i.imgur.com/x.jpg")

    def test_duplicate_url_idempotent(self, a_post):
        a1 = attach_image(a_post, "https://i.imgur.com/dupe.jpg")
        a2 = attach_image(a_post, "https://i.imgur.com/dupe.jpg")
        assert a1 == a2
        rows = list_attachments(a_post)
        assert len(rows) == 1

    def test_max_per_post_enforced(self, a_post):
        for i in range(forum_core.MAX_ATTACHMENTS_PER_POST):
            attach_image(a_post, f"https://i.imgur.com/img{i}.jpg")
        with pytest.raises(ForumError, match="max"):
            attach_image(a_post, "https://i.imgur.com/one-too-many.jpg")

    def test_sort_order_preserved(self, a_post):
        attach_image(a_post, "https://i.imgur.com/a.jpg", label="A")
        attach_image(a_post, "https://i.imgur.com/b.jpg", label="B")
        attach_image(a_post, "https://i.imgur.com/c.jpg", label="C")
        rows = list_attachments(a_post)
        assert [r["label"] for r in rows] == ["A", "B", "C"]


# ---------- MCP tools ---------------------------------------------------

@pytest.fixture
def tools(tmp_db):
    """Capture decorated tool callables."""
    from fango.forum_post_tools import register
    captured = {}
    class _M:
        def tool(self_):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco
    register(_M())
    return captured


@pytest.mark.skip(
    reason="Direct image-attach MCP tools suspended "
           "(forum_post_tools.DIRECT_POSTING_ENABLED). Core attach logic stays "
           "covered by the service + SSR tests in this file."
)
class TestMcpTools:
    def test_attach_image_requires_auth(self, tools, a_post):
        from fango.auth import AuthError
        with pytest.raises(AuthError):
            tools["fango_attach_image"](a_post, "https://i.imgur.com/x.jpg")

    def test_attach_image_happy_path(self, tools, a_post, agent_and_key, with_current_agent):
        agent, _ = agent_and_key
        with_current_agent(agent)
        out = tools["fango_attach_image"](a_post, "https://i.imgur.com/x.jpg", label="cover")
        assert isinstance(out["attachment_id"], int) and out["attachment_id"] > 0

    def test_list_post_attachments(self, tools, a_post, agent_and_key, with_current_agent):
        agent, _ = agent_and_key
        with_current_agent(agent)
        tools["fango_attach_image"](a_post, "https://i.imgur.com/a.jpg", label="A")
        tools["fango_attach_image"](a_post, "https://i.imgur.com/b.jpg", label="B")
        rows = tools["fango_list_post_attachments"](a_post)
        labels = [r["label"] for r in rows]
        assert labels == ["A", "B"]


# ---------- SSR rendering -----------------------------------------------

class TestSsrRendering:
    def test_attachment_renders_in_thread_view(self, client, agent_factory, listing_factory):
        from fango.baibai import service as bb
        ag, _ = agent_factory()
        out = bb.create_thread(title="t", body="b", author_id=ag.id, agent_created_at=ag.created_at)
        attach_image(out["post"].id, "https://i.imgur.com/example.jpg", label="街景")

        thread_id = out["thread"].id
        r = client.get(
            f"/baibai/t/{thread_id}",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/120.0"},
        )
        assert r.status_code == 200
        assert "i.imgur.com/example.jpg" in r.text
        assert "街景" in r.text
