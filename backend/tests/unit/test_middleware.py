"""TC-U-28 — If-None-Match weak comparison (RFC 9110 §8.8.3/§13.1.2) — EC-34, CF-04.

The deployed origin sits behind Cloudflare, which downgrades our strong
``"…"`` ETag to the weak ``W/"…"`` whenever it gzip-encodes the response
(brotli passes it through untouched). A client that echoes the weak form back
must still be answered 304, otherwise every gzip-only client on the deployed
URL re-downloads the body and CF-04's "ETag → 304" check fails there while
passing locally."""

from __future__ import annotations

import pytest

from app.core.middleware import _matches, _opaque

ETAG = '"1d2c5a1ed4104374e6f34d8fafe4155eb66e0df2e8eea3e355d52568ccc176bd"'
WEAK = f"W/{ETAG}"
OTHER = '"c7ae3e5727bf05c84c53c496a8aaf40e640b69782895f84ae8ff219e93f0daaa"'


def test_strong_form_matches_itself() -> None:
    assert _matches(ETAG, ETAG) is True


def test_weak_form_of_same_entity_tag_matches() -> None:
    assert _matches(WEAK, ETAG) is True


def test_strong_form_matches_weak_emitted_etag() -> None:
    assert _matches(ETAG, WEAK) is True


def test_wildcard_matches() -> None:
    assert _matches("*", ETAG) is True


def test_list_with_other_tags_still_matches() -> None:
    assert _matches(f"{OTHER}, {WEAK}", ETAG) is True


def test_different_entity_tag_never_matches() -> None:
    assert _matches(WEAK, OTHER) is False


def test_empty_header_matches_nothing() -> None:
    assert _matches("", ETAG) is False
    assert _matches("   ", ETAG) is False


def test_partial_or_unquoted_tag_is_not_a_match() -> None:
    assert _matches(ETAG[:10], ETAG) is False
    assert _matches("W/1d2c5a1e", ETAG) is False


@pytest.mark.parametrize(
    ("tag", "opaque"),
    [
        ('"abc"', "abc"),
        ('W/"abc"', "abc"),
        ('  W/"abc"  ', "abc"),
        ("abc", "abc"),
        ("W/", ""),
    ],
)
def test_opaque_strips_weakness_and_quotes(tag: str, opaque: str) -> None:
    assert _opaque(tag) == opaque
