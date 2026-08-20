import json
from pathlib import Path

import pytest

from agent_search_gateway.models import SearchRecord
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import normalize_url


def test_result_writer_uses_exact_request_id_and_public_jsonl_schema(tmp_path: Path) -> None:
    results_dir = tmp_path / "nested" / "results"
    writer = ResultWriter(results_dir)
    records = [
        SearchRecord(normalize_url("https://EXAMPLE.com/a"), "Résumé 日本語"),
        SearchRecord(normalize_url("https://example.com/b"), "Second"),
    ]

    keyword = writer.write_results("keyword", records, request_id="a1b2c3d4")
    empty = writer.write_results("llm", [], request_id="11111111")

    assert keyword.is_absolute()
    assert empty.is_absolute()
    assert keyword.name == "keyword-a1b2c3d4.jsonl"
    assert empty.name == "llm-11111111.jsonl"
    assert empty.read_text(encoding="utf-8") == ""

    lines = keyword.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"url":"https://example.com/a","abstract":"Résumé 日本語"}'
    assert [set(json.loads(line)) for line in lines] == [{"url", "abstract"}, {"url", "abstract"}]
    assert all("request_id" not in json.loads(line) for line in lines)


def test_result_writer_never_switches_request_id_on_collision(tmp_path: Path) -> None:
    writer = ResultWriter(tmp_path)
    target = tmp_path / "keyword-a1b2c3d4.jsonl"
    target.write_text("original\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        writer.write_results("keyword", [], request_id="a1b2c3d4")

    assert target.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.glob("keyword-*.jsonl")) == [target]


def test_result_writer_serializes_before_creating_target(tmp_path: Path) -> None:
    writer = ResultWriter(tmp_path)

    with pytest.raises(ValueError):
        writer.write_results(
            "keyword",
            [SearchRecord(normalize_url("https://example.com"), " ")],
            request_id="a1b2c3d4",
        )

    assert not (tmp_path / "keyword-a1b2c3d4.jsonl").exists()


def test_result_writer_rejects_invalid_request_id_without_creating_directory(
    tmp_path: Path,
) -> None:
    results_dir = tmp_path / "missing" / "results"
    writer = ResultWriter(results_dir)

    with pytest.raises(ValueError, match="request ID"):
        writer.write_results("keyword", [], request_id="bad")

    assert not results_dir.exists()
