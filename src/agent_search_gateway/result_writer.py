"""Unique compact JSONL result files for search commands."""

import json
import secrets
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from .models import SearchRecord

ResultKind = Literal["keyword", "llm"]


def _serialize_record(record: SearchRecord) -> str:
    abstract = record.abstract.strip()
    if not abstract:
        raise ValueError("search result abstract must be non-empty")
    return json.dumps(
        {"url": str(record.url), "abstract": abstract},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class ResultWriter:
    def __init__(self, results_dir: Path) -> None:
        self._results_dir = results_dir

    def write_results(self, kind: ResultKind, records: Iterable[SearchRecord]) -> Path:
        self._results_dir.mkdir(parents=True, exist_ok=True)
        serialized = tuple(_serialize_record(record) for record in records)
        while True:
            target = self._results_dir / f"{kind}-{secrets.token_hex(4)}.jsonl"
            try:
                with target.open("x", encoding="utf-8", newline="\n") as handle:
                    for line in serialized:
                        handle.write(line)
                        handle.write("\n")
                return target.resolve()
            except FileExistsError:
                continue
