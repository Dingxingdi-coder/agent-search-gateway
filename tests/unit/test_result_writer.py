import json
from pathlib import Path

import pytest

from agent_search_gateway.models import SearchRecord
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import normalize_url


def test_result_writer_creates_unique_compact_jsonl_with_only_public_fields(tmp_path: Path) -> None:
    results_dir = tmp_path / "nested" / "results"
    writer = ResultWriter(results_dir)
    records = [
        SearchRecord(normalize_url("https://EXAMPLE.com/a"), "Résumé 日本語"),
        SearchRecord(normalize_url("https://example.com/b"), "Second"),
    ]

    first = writer.write_results("keyword", records)
    second = writer.write_results("keyword", records)
    empty = writer.write_results("llm", [])

    assert first.is_absolute()
    assert second.is_absolute()
    assert empty.is_absolute()
    assert first != second
    assert first.name.startswith("keyword-")
    assert second.name.startswith("keyword-")
    assert empty.name.startswith("llm-")
    assert empty.read_text(encoding="utf-8") == ""

    lines = first.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"url":"https://example.com/a","abstract":"Résumé 日本語"}'
    assert [set(json.loads(line)) for line in lines] == [{"url", "abstract"}, {"url", "abstract"}]

    with pytest.raises(ValueError):
        writer.write_results("keyword", [SearchRecord(normalize_url("https://example.com"), " ")])
