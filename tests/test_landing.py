"""Matrix landing page (/intro) + first-visit redirect script in base.html."""
from __future__ import annotations


def test_intro_renders(client):
    r = client.get("/intro")
    assert r.status_code == 200
    body = r.text
    assert 'id="matrix"' in body
    assert 'id="typewriter"' in body
    assert 'id="corpus"' in body
    assert "入場" in body
    assert "人間は投稿しない" in body
    assert "requestAnimationFrame(draw)" in body


def test_intro_corpus_has_real_estate_phrases(client):
    """The matrix corpus must contain phrases extracted from the docx (or fallback)."""
    r = client.get("/intro")
    body = r.text
    # The corpus block embeds JSON-encoded phrases
    import re, json
    m = re.search(r'<script id="corpus"[^>]*>(.*?)</script>', body, re.S)
    assert m is not None
    phrases = json.loads(m.group(1))
    assert isinstance(phrases, list)
    assert len(phrases) >= 20
    # Should include atmospheric strings
    assert "IO.Fango" in phrases


def test_intel_corpus_extracts_phrases():
    from fango.intel import load_phrases
    p = load_phrases()
    assert len(p) >= 20
    assert "IO.Fango" in p


def test_intro_is_standalone(client):
    """Landing must NOT include base.html's app shell (nav / rail)."""
    r = client.get("/intro")
    assert r.status_code == 200
    body = r.text
    assert "col-nav" not in body
    assert "col-rail" not in body
    assert "viewer-note" not in body


def test_intro_can_be_skipped_via_root_link(client):
    r = client.get("/intro")
    assert 'href="/"' in r.text
    assert "btn-enter" in r.text


def test_home_includes_first_visit_redirect_script(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "/intro" in body
    assert "fango-entered" in body


def test_deep_link_does_not_redirect_server_side(client):
    """A direct request to a content page must return that page (200), not bounce to /intro."""
    r = client.get("/baibai/")
    assert r.status_code == 200
    # The redirect script is *client side*. Server returns the page directly.
    assert "IO.Fango" in r.text


def test_intro_has_no_links_to_locked_areas(client):
    r = client.get("/intro")
    # No outgoing links besides / (the enter button + skip link)
    assert "/fangobook/onboard" not in r.text
