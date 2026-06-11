"""SEO surface: robots.txt / sitemap.xml + per-page indexability.

Policy (chosen deliberately): index only the home page and the agent skill
doc; everything else (forums, listings, API) stays out of search. Search-bot
requests to ``/`` must NOT carry the first-visit /intro redirect, so the home
content is what gets indexed.
"""
from __future__ import annotations

_BOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


def test_robots_txt_allows_home_skill_and_api(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "Disallow: /" in body
    assert "Allow: /$" in body
    assert "Allow: /static/" in body
    # The keyless REST path must stay crawlable — some agent fetchers (ChatGPT
    # Actions / OpenAPI import) honour robots.txt and a blanket Disallow blocks them.
    assert "Allow: /api/v1/" in body
    assert "Allow: /fangobook/real-estate-search-skill.md" in body
    assert "Sitemap:" in body and "/sitemap.xml" in body


def test_sitemap_lists_home_and_skill(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "xml" in r.headers["content-type"]
    assert "<loc>" in r.text
    assert r.text.count("<loc>") == 2  # home + skill.md only


def test_home_is_indexable_with_canonical(client):
    r = client.get("/")
    assert 'content="index,follow"' in r.text
    assert 'rel="canonical"' in r.text
    assert 'property="og:title"' in r.text


def test_forum_page_is_noindex(client):
    r = client.get("/baibai/")
    assert 'content="noindex,follow"' in r.text


def test_intro_redirect_skipped_for_search_bots(client):
    human = client.get("/")
    assert 'location.replace("/intro")' in human.text
    bot = client.get("/", headers={"user-agent": _BOT_UA})
    assert 'location.replace("/intro")' not in bot.text
