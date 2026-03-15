"""Tests for tag normalization and suggestion."""

from agent_memory.tags import CONTROLLED_TAGS, normalize_tags, suggest_extensions


def test_normalize_lowercases():
    assert normalize_tags(["Auth", "API", "Bug"]) == ["auth", "api", "bug"]


def test_normalize_strips_whitespace():
    assert normalize_tags(["  auth ", " api"]) == ["auth", "api"]


def test_normalize_deduplicates():
    assert normalize_tags(["auth", "Auth", " auth"]) == ["auth"]


def test_normalize_preserves_order():
    assert normalize_tags(["bug", "auth", "api"]) == ["bug", "auth", "api"]


def test_normalize_drops_empty():
    assert normalize_tags(["", "  ", "auth", ""]) == ["auth"]


def test_normalize_unknown_tags_preserved():
    result = normalize_tags(["auth", "unknown-custom-tag"])
    assert result == ["auth", "unknown-custom-tag"]


def test_suggest_extensions_auth():
    suggestions = suggest_extensions(["auth"])
    assert "security" in suggestions
    assert "authentication" in suggestions


def test_suggest_extensions_no_duplicates():
    suggestions = suggest_extensions(["auth", "security"])
    # security is already in input, should not appear in suggestions
    assert "security" not in suggestions
    assert "authentication" in suggestions


def test_suggest_extensions_empty_input():
    assert suggest_extensions([]) == []


def test_suggest_extensions_unknown_tag():
    assert suggest_extensions(["totally-unknown"]) == []


def test_suggest_extensions_database():
    suggestions = suggest_extensions(["database"])
    assert "migration" in suggestions
    assert "schema" in suggestions
    assert "query" in suggestions


def test_controlled_tags_nonempty():
    assert len(CONTROLLED_TAGS) > 30
