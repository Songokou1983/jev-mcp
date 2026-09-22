"""Cache: sha256 key derivation + LRU eviction.

TDD — these tests define the behavior before implementation.
"""
import pytest

from jev_mcp.cache import LRUCache, compute_cache_key


class TestComputeCacheKey:
    """Cache key = sha256(canonical_json(state) + canonical_json(questions) + model_version)."""

    def test_returns_hex_sha256(self):
        key = compute_cache_key(state="hello", questions={"q1": {"type": "noul"}}, model_version="jev-1.13.0")
        # 64 hex chars
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_same_inputs_same_key(self):
        k1 = compute_cache_key(state="hi", questions={"q": {"type": "noul"}}, model_version="jev-1.13.0")
        k2 = compute_cache_key(state="hi", questions={"q": {"type": "noul"}}, model_version="jev-1.13.0")
        assert k1 == k2

    def test_dict_state_serializes_canonically(self):
        # Key ordering in dict shouldn't matter (sorted_keys)
        k1 = compute_cache_key(
            state={"a": 1, "b": 2},
            questions={"q": {"type": "noul"}},
            model_version="jev-1.13.0",
        )
        k2 = compute_cache_key(
            state={"b": 2, "a": 1},
            questions={"q": {"type": "noul"}},
            model_version="jev-1.13.0",
        )
        assert k1 == k2

    def test_dict_questions_serializes_canonically(self):
        k1 = compute_cache_key(
            state="x",
            questions={"q1": {"type": "noul"}, "q2": {"type": "choice"}},
            model_version="jev-1.13.0",
        )
        k2 = compute_cache_key(
            state="x",
            questions={"q2": {"type": "choice"}, "q1": {"type": "noul"}},
            model_version="jev-1.13.0",
        )
        assert k1 == k2

    def test_different_model_different_key(self):
        k1 = compute_cache_key(state="x", questions={"q": {"type": "noul"}}, model_version="jev-1.13.0")
        k2 = compute_cache_key(state="x", questions={"q": {"type": "noul"}}, model_version="jev-1.14.0")
        assert k1 != k2

    def test_different_state_different_key(self):
        k1 = compute_cache_key(state="x", questions={"q": {"type": "noul"}}, model_version="m")
        k2 = compute_cache_key(state="y", questions={"q": {"type": "noul"}}, model_version="m")
        assert k1 != k2

    def test_unicode_state_preserved(self):
        # Chinese chars in state shouldn't break — ensure_ascii=False
        k = compute_cache_key(state="你好", questions={"q": {"type": "noul"}}, model_version="m")
        assert len(k) == 64


class TestLRUCache:
    """LRU with cap, hit/miss tracking."""

    def test_get_miss_returns_none(self):
        c = LRUCache(capacity=3)
        assert c.get("nope") is None
        assert c.hits == 0
        assert c.misses == 1

    def test_set_and_get(self):
        c = LRUCache(capacity=3)
        c.set("k1", "v1")
        assert c.get("k1") == "v1"
        assert c.hits == 1
        assert c.misses == 0

    def test_lru_evicts_oldest(self):
        c = LRUCache(capacity=2)
        c.set("a", 1)
        c.set("b", 2)
        c.set("c", 3)  # evicts "a"
        assert c.get("a") is None
        assert c.get("b") == 2
        assert c.get("c") == 3

    def test_get_promotes_recency(self):
        c = LRUCache(capacity=2)
        c.set("a", 1)
        c.set("b", 2)
        # Touch "a" so it's newest
        assert c.get("a") == 1
        c.set("c", 3)  # should evict "b" now, not "a"
        assert c.get("a") == 1
        assert c.get("b") is None
        assert c.get("c") == 3

    def test_set_existing_promotes(self):
        c = LRUCache(capacity=2)
        c.set("a", 1)
        c.set("b", 2)
        c.set("a", 99)  # update existing, promote
        c.set("c", 3)  # should evict "b"
        assert c.get("a") == 99
        assert c.get("b") is None
        assert c.get("c") == 3

    def test_hit_rate(self):
        c = LRUCache(capacity=3)
        c.set("k", "v")
        c.get("k")  # hit
        c.get("k")  # hit
        c.get("nope")  # miss
        assert c.hits == 2
        assert c.misses == 1
        assert c.hit_rate() == pytest.approx(2 / 3)

    def test_hit_rate_empty(self):
        c = LRUCache(capacity=3)
        assert c.hit_rate() == 0.0

    def test_size(self):
        c = LRUCache(capacity=10)
        c.set("a", 1)
        c.set("b", 2)
        assert c.size == 2