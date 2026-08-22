"""Post-deduplication open-access enrichment for paper records."""

from __future__ import annotations

import logging
from dataclasses import replace

from ..errors import GatewayError
from ..models import OAResolution, PaperRecord
from ..observability import log_event
from ..providers.contracts import OAResolver

_LOGGER = logging.getLogger(__name__)


async def enrich_paper_records(
    records: list[PaperRecord],
    resolver: OAResolver | None,
) -> list[PaperRecord]:
    """Enrich DOI-bearing papers without allowing resolver failures to drop records."""

    if resolver is None:
        return list(records)
    cache: dict[str, OAResolution | None] = {}
    enriched: list[PaperRecord] = []
    for record in records:
        doi = record.identifiers.doi
        if not doi:
            enriched.append(record)
            continue
        if doi not in cache:
            try:
                cache[doi] = await resolver.resolve(doi)
            except GatewayError as exc:
                log_event(
                    _LOGGER,
                    logging.DEBUG,
                    "paper_enrichment_failed",
                    resolver=resolver.name,
                    stage="oa_resolve",
                    error_type=type(exc).__name__,
                )
                cache[doi] = None
        resolution = cache[doi]
        enriched.append(_apply_resolution(record, resolution))
    return enriched


def _apply_resolution(record: PaperRecord, resolution: OAResolution | None) -> PaperRecord:
    if resolution is None:
        return record
    return replace(
        record,
        pdf_url=record.pdf_url or resolution.pdf_url,
        is_open_access=(
            record.is_open_access
            if record.is_open_access is not None
            else resolution.is_open_access
        ),
        oa_status=record.oa_status or resolution.oa_status,
        license=record.license or resolution.license,
    )
