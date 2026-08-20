"""URL fetch workflow orchestration and URL-state mutation."""

import logging
import time
from collections.abc import Callable

from ..concurrency import PerKeyLockPool, SingleflightGroup
from ..errors import UNAVAILABLE_MESSAGE, ErrorCode, ExecutionFailure, InputFailure
from ..llm.stages import LLMStages
from ..models import URLRecord
from ..observability import log_event, normalize_log_reason
from ..scheduler.fetch import FetchScheduler
from ..url_normalization import NormalizedURL, normalize_url
from ..url_store import URLStore


class FetchOrchestrator:
    def __init__(
        self,
        *,
        store: URLStore,
        scheduler: FetchScheduler,
        stages: LLMStages,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._stages = stages
        self._logger = logger or logging.getLogger(__name__)
        self._monotonic = monotonic
        self._url_locks: PerKeyLockPool[NormalizedURL] = PerKeyLockPool()
        self._request_singleflight: SingleflightGroup[tuple[NormalizedURL, str | None], str] = (
            SingleflightGroup()
        )

    async def url_fetch(self, url: str, focus: str | None = None) -> str:
        normalized_url = normalize_url(url)
        normalized_focus = focus.strip() if focus is not None and focus.strip() else None
        key = (normalized_url, normalized_focus)
        focus_present = normalized_focus is not None
        focus_chars = len(normalized_focus or "")
        return await self._request_singleflight.do(
            key,
            lambda: self._serialized_url_fetch(normalized_url, normalized_focus),
            on_leader=lambda: log_event(
                self._logger,
                logging.DEBUG,
                "singleflight_leader",
                url=str(normalized_url),
                focus_present=focus_present,
                focus_chars=focus_chars,
            ),
            on_follower=lambda: log_event(
                self._logger,
                logging.DEBUG,
                "singleflight_joined",
                url=str(normalized_url),
                focus_present=focus_present,
                focus_chars=focus_chars,
            ),
        )

    async def _serialized_url_fetch(
        self,
        normalized_url: NormalizedURL,
        normalized_focus: str | None,
    ) -> str:
        lock_started = self._monotonic()
        async with self._url_locks.acquire(normalized_url):
            log_event(
                self._logger,
                logging.DEBUG,
                "url_lock_acquired",
                url=str(normalized_url),
                wait_ms=max(0, int((self._monotonic() - lock_started) * 1000)),
            )
            snapshot = self._store.get(normalized_url)
            if snapshot is None:
                self._log_state(normalized_url, "url_not_admitted")
                raise InputFailure(ErrorCode.URL_NOT_ADMITTED, "URL was not admitted by search")
            if not snapshot.available:
                self._log_state(normalized_url, "stored_unavailable")
                return UNAVAILABLE_MESSAGE

            prepared = await self._prepare_content(normalized_url)
            if not prepared:
                return UNAVAILABLE_MESSAGE

            refreshed = self._require_snapshot(normalized_url)
            if not await self._safety_check(normalized_url, refreshed.content):
                return UNAVAILABLE_MESSAGE
            if normalized_focus is None:
                self._log_final(normalized_url, refreshed.content, "content")
                return refreshed.content
            summary = await self._focus_summary(refreshed.content, normalized_focus)
            self._log_final(normalized_url, summary, "focus_summary")
            return summary

    async def _prepare_content(self, url: NormalizedURL) -> bool:
        snapshot = self._require_snapshot(url)
        if snapshot.content:
            self._log_state(url, "cached_content")
            return True

        if not snapshot.raw_content:
            self._log_state(url, "provider_fetch_required")
            if not self._scheduler.provider_names:
                raise ExecutionFailure(
                    ErrorCode.NO_URL_FETCH_PROVIDERS,
                    "No URL fetch providers are enabled",
                )
            outcome = await self._scheduler.fetch_until_accepted(url)
            if outcome.kind == "execution_failure":
                raise ExecutionFailure(
                    ErrorCode.ALL_PROVIDERS_FAILED,
                    "All URL fetch provider pipelines failed",
                )
            if outcome.kind == "semantic_failure":
                self._store.mark_unavailable(url)
                self._log_rejected(url, "fetch_semantic_failure")
                return False
            candidate = outcome.candidate
            if candidate is None:
                raise RuntimeError("accepted fetch outcome did not contain a candidate")
            self._store.merge_body(
                url,
                raw_content=candidate.raw_content,
                content=candidate.content,
            )
            log_event(
                self._logger,
                logging.DEBUG,
                "body_accepted",
                url=str(url),
                reason="provider_fetch",
                raw_chars=len(candidate.raw_content),
                content_chars=len(candidate.content),
            )
            snapshot = self._require_snapshot(url)
        else:
            self._log_state(url, "raw_content_available")

        if not snapshot.content:
            cleaned = await self._content_clean(snapshot.raw_content)
            self._store.merge_body(url, content=cleaned)
            log_event(
                self._logger,
                logging.DEBUG,
                "body_accepted",
                url=str(url),
                reason="content_cleaned",
                content_chars=len(cleaned),
            )
        return True

    async def _content_clean(self, raw_content: str) -> str:
        try:
            return await self._stages.content_clean(raw_content)
        except ExecutionFailure as exc:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                "Content-clean LLM stage failed",
            ) from exc

    async def _safety_check(self, url: NormalizedURL, content: str) -> bool:
        try:
            decision = await self._stages.safety(content)
        except ExecutionFailure as exc:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                "Safety LLM stage failed",
            ) from exc
        if decision.ok:
            return True
        self._store.mark_unavailable(url)
        self._log_rejected(
            url,
            "safety_rejected",
            decision_reason=normalize_log_reason(decision.reason),
        )
        return False

    async def _focus_summary(self, content: str, focus: str) -> str:
        try:
            return await self._stages.focus_summary(content, focus)
        except ExecutionFailure as exc:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                "Focus-summary LLM stage failed",
            ) from exc

    def _log_state(self, url: NormalizedURL, reason: str) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "body_skipped",
            url=str(url),
            reason=reason,
        )

    def _log_rejected(
        self,
        url: NormalizedURL,
        reason: str,
        *,
        decision_reason: str = "",
    ) -> None:
        if not decision_reason:
            log_event(
                self._logger,
                logging.DEBUG,
                "body_rejected",
                url=str(url),
                reason=reason,
            )
            return
        log_event(
            self._logger,
            logging.DEBUG,
            "body_rejected",
            url=str(url),
            reason=reason,
            decision_reason=decision_reason,
        )

    def _log_final(self, url: NormalizedURL, text: str, reason: str) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "body_accepted",
            url=str(url),
            reason=reason,
            output_chars=len(text),
        )

    def _require_snapshot(self, url: NormalizedURL) -> URLRecord:
        snapshot = self._store.get(url)
        if snapshot is None:
            raise RuntimeError("admitted URL disappeared from store")
        return snapshot
