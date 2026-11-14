"""Shared test isolation: feed caches never touch the real data directory."""
import pytest


@pytest.fixture(autouse=True)
def isolated_feed_cache(tmp_path, monkeypatch):
    from terra import feeds
    monkeypatch.setattr(feeds, "_disk", lambda name: tmp_path / f"feed-{name}.json")
    saved = dict(feeds._cache)
    feeds._cache.clear()
    yield
    feeds._cache.clear()
    feeds._cache.update(saved)
