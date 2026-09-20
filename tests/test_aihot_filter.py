import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import aihot_filter
import pytest


@pytest.mark.parametrize('host', ['aihot.virxact.com', 'aihot.news', 'www.aihot.news'])
def test_aihot_domain_migration_preserves_original_url_and_identity(host):
    feed = source('AI HOT', f'https://{host}/feed/all.xml')
    entry = {'link': f'https://{host}/items/story',
             'summary': '<a href="https://aihot.news/items/story">AI HOT</a> '
                        '<a href="https://x.com/example/status/123">阅读原文</a>'}
    assert aihot_filter.aihot_feed_kind(feed) == 'all'
    assert aihot_filter.extract_aihot_original_url(entry) == 'https://x.com/example/status/123'
    assert aihot_filter.decide_aihot_entry(entry, [], source=feed).action == 'allow'
    patched = aihot_filter.entry_for_ingest(entry, source=feed)
    assert patched['guid'] == patched['link'] == 'https://x.com/example/status/123'


def test_aihot_new_domain_selected_still_excludes_covered_sources():
    feed = source('AI HOT 精选', 'https://aihot.news/feed')
    entry = {'link': 'https://aihot.news/items/story',
             'summary': '阅读原文：https://techcrunch.com/example'}
    decision = aihot_filter.decide_aihot_entry(
        entry, [source('TechCrunch', 'https://techcrunch.com/feed')], source=feed)
    assert decision.reason == 'covered_by_enabled_source'


def source(name, url, enabled=True):
    return {
        "record_id": name,
        "name": name,
        "feed_url": url,
        "enabled": enabled,
    }


def test_aihot_allows_entry_matching_disabled_local_rsshub_twitter_source():
    sources = [
        source("Twitter @MiniMax_AI", "http://192.168.1.76:1200/twitter/user/MiniMax_AI", enabled=False),
        source("AI HOT", "https://aihot.virxact.com/feed/all.xml", enabled=True),
    ]
    decision = aihot_filter.decide_aihot_entry(
        {"link": "https://x.com/MiniMax_AI/status/123", "author": "X：MiniMax (@MiniMax_AI)"},
        sources,
    )

    assert decision.action == "allow"
    assert decision.reason == "twitter_entry"


def test_aihot_allows_embedded_original_x_url_from_aihot_item_link():
    sources = [
        source("AI HOT", "https://aihot.virxact.com/feed/all.xml", enabled=True),
    ]
    decision = aihot_filter.decide_aihot_entry(
        {
            "link": "https://aihot.virxact.com/items/cmqowdwfa04sbslx6f185qwnr",
            "summary": "AI HOT 摘要\n\n阅读原文：https://x.com/MiniMax_AI/status/123",
            "author": "X：MiniMax (@MiniMax_AI)",
        },
        sources,
    )

    assert decision.action == "allow"
    assert decision.reason == "twitter_entry"


def test_aihot_selected_skips_entry_when_enabled_original_source_covers_domain():
    sources = [
        source("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", enabled=True),
        source("AI HOT 精选", "https://aihot.virxact.com/feed", enabled=True),
    ]
    decision = aihot_filter.decide_aihot_entry(
        {"link": "https://techcrunch.com/2026/06/04/example-ai-news"},
        sources,
        source=sources[1],
    )

    assert decision.action == "skip"
    assert decision.reason == "covered_by_enabled_source"
    assert "domain:techcrunch.com" in decision.matched_enabled_keys


def test_aihot_all_skips_non_twitter_entry():
    sources = [
        source("Twitter @MiniMax_AI", "http://192.168.1.76:1200/twitter/user/MiniMax_AI", enabled=False),
        source("AI HOT", "https://aihot.virxact.com/feed/all.xml", enabled=True),
    ]
    decision = aihot_filter.decide_aihot_entry(
        {"link": "https://example.com/not-in-source-table"},
        sources,
    )

    assert decision.action == "skip"
    assert decision.reason == "not_twitter_or_selected"


def test_aihot_all_skips_reuters_item_even_when_disabled_local_rsshub_reuters_route_matches():
    sources = [
        source("Tech News | Reuters", "http://192.168.1.76:1200/reuters/technology", enabled=False),
        source("AI HOT", "https://aihot.virxact.com/feed/all.xml", enabled=True),
    ]
    decision = aihot_filter.decide_aihot_entry(
        {"link": "https://www.reuters.com/technology/artificial-intelligence-example"},
        sources,
    )

    assert decision.action == "skip"
    assert decision.reason == "disabled_local_rsshub_non_twitter"
    assert "domain:reuters.com" in decision.matched_disabled_keys


def test_aihot_selected_allows_entry_not_covered_by_enabled_source():
    sources = [
        source("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", enabled=True),
        source("AI HOT 精选", "https://aihot.virxact.com/feed", enabled=True),
    ]
    decision = aihot_filter.decide_aihot_entry(
        {"link": "https://example.com/selected-ai-news"},
        sources,
        source=sources[1],
    )

    assert decision.action == "allow"
    assert decision.reason == "selected_uncovered_by_enabled_source"


def test_aihot_recognizes_selected_and_all_feed_urls():
    assert aihot_filter.aihot_feed_kind("https://aihot.virxact.com/feed/all.xml") == "all"
    assert aihot_filter.aihot_feed_kind("https://aihot.virxact.com/feed") == "selected"
    assert aihot_filter.aihot_feed_kind("https://aihot.virxact.com/feed.xml") == "selected"
    assert aihot_filter.aihot_feed_kind("https://aihot.virxact.com/rss") == "selected"
