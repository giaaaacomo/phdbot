"""Tests for discovery reply parsing."""

from __future__ import annotations

from phd_searcher.pipeline.discovery import _parse_reply

ALLOWED = {"https://a.example/phd", "https://b.example/vacancies"}


def test_parse_reply_filters_hallucinations():
    reply = '["https://a.example/phd", "https://evil.example/x"]'
    assert _parse_reply(reply, ALLOWED) == ["https://a.example/phd"]


def test_parse_reply_handles_fenced_json():
    reply = '```json\n["https://b.example/vacancies"]\n```'
    assert _parse_reply(reply, ALLOWED) == ["https://b.example/vacancies"]


def test_parse_reply_garbage_is_empty():
    assert _parse_reply("NONE", ALLOWED) == []


def test_parse_reply_non_list_json_is_empty():
    assert _parse_reply('{"a": 1}', ALLOWED) == []


def test_parse_reply_empty_array():
    assert _parse_reply("[]", ALLOWED) == []
