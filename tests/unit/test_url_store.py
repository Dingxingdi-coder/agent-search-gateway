from dataclasses import fields

import pytest

from agent_search_gateway.models import URLRecord
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore


def test_url_store_preserves_public_shape_and_first_write_state_machine() -> None:
    assert [field.name for field in fields(URLRecord)] == [
        "url",
        "raw_content",
        "content",
        "abstract",
        "available",
    ]

    store = URLStore()
    url = normalize_url("https://EXAMPLE.com/page")

    with pytest.raises(ValueError):
        store.admit(url, "   ")

    first = store.admit(url, "First abstract", raw_content="raw-1")
    assert first == URLRecord(
        url=url,
        raw_content="raw-1",
        content="",
        abstract="First abstract",
        available=True,
    )

    merged = store.admit(
        url,
        "Second abstract",
        raw_content="raw-2",
        content="clean-1",
    )
    assert merged.abstract == "First abstract"
    assert merged.raw_content == "raw-1"
    assert merged.content == "clean-1"

    after_body = store.merge_body(url, raw_content="raw-3", content="clean-2")
    assert after_body.raw_content == "raw-1"
    assert after_body.content == "clean-1"

    unavailable = store.mark_unavailable(url)
    assert unavailable.available is False
    assert store.mark_unavailable(url).available is False

    snapshot = store.get(url)
    assert snapshot is not None
    assert snapshot.available is False
    with pytest.raises((AttributeError, TypeError)):
        snapshot.content = "mutated"  # type: ignore[misc]

    assert "focus" not in {field.name for field in fields(URLRecord)}
