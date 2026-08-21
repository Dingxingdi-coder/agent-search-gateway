"""Compact JSONL persistence for search command results."""

import json
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path

from .models import SearchRecord
from .request_ids import ResultKind, result_filename


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

    def write_results(
        self,
        kind: ResultKind,
        records: Iterable[SearchRecord],
        *,
        request_id: str,
    ) -> Path:
        filename = result_filename(kind, request_id)
        serialized = tuple(_serialize_record(record) for record in records)
        self._results_dir.mkdir(parents=True, exist_ok=True)
        target = self._results_dir / filename
        created = False
        try:
            with target.open("x", encoding="utf-8", newline="\n") as handle:
                created = True
                for line in serialized:
                    handle.write(line)
                    handle.write("\n")
            return target.resolve()
        except BaseException:
            if created:
                with suppress(OSError):
                    target.unlink()
            raise
