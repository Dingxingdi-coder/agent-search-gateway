import asyncio
from collections.abc import Mapping, Sequence

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.providers.contracts import ChatMessage, URLFetchCandidate
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_normalization import NormalizedURL, normalize_url


class _AlwaysAcceptJudge:
    name = "judge"

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        return {"ok": True}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        return "unused"

    async def aclose(self) -> None:
        return None


class _ControlledFetchProvider:
    def __init__(self, name: str, *, hold: bool = False) -> None:
        self.name = name
        self.hold = hold
        self.calls: list[NormalizedURL] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        self.calls.append(url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if self.hold:
                await self.release.wait()
            return URLFetchCandidate(raw_content=f"raw:{self.name}:{url}")
        finally:
            self.active -= 1


def _scheduler(
    providers: list[_ControlledFetchProvider],
    quotas: ProviderQuotaManager,
) -> FetchScheduler:
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": _AlwaysAcceptJudge()},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    return FetchScheduler(providers, quotas, stages)


async def test_fetch_scheduler_uses_available_provider_without_parallel_attempts_for_one_job() -> (
    None
):
    url = normalize_url("https://example.com/one")

    first = _ControlledFetchProvider("first")
    second = _ControlledFetchProvider("second")
    quotas = ProviderQuotaManager(web_limits={"first": 1, "second": 1}, llm_limits={})
    scheduler = _scheduler([first, second], quotas)

    held_first = await quotas.get_web("first").try_lease()
    assert held_first is not None
    try:
        result = await scheduler.fetch_until_accepted(url)
    finally:
        await held_first.release()
    assert result.kind == "accepted"
    assert first.calls == []
    assert second.calls == [url]

    wait_first = _ControlledFetchProvider("first")
    wait_second = _ControlledFetchProvider("second")
    wait_quotas = ProviderQuotaManager(web_limits={"first": 1, "second": 1}, llm_limits={})
    wait_scheduler = _scheduler([wait_first, wait_second], wait_quotas)
    held_a = await wait_quotas.get_web("first").try_lease()
    held_b = await wait_quotas.get_web("second").try_lease()
    assert held_a is not None and held_b is not None
    waiting_task = asyncio.create_task(wait_scheduler.fetch_until_accepted(url))
    await asyncio.sleep(0)
    assert wait_first.calls == [] and wait_second.calls == []
    await held_b.release()
    waited_result = await waiting_task
    await held_a.release()
    assert waited_result.kind == "accepted"
    assert wait_first.calls == []
    assert wait_second.calls == [url]

    order_first = _ControlledFetchProvider("first")
    order_second = _ControlledFetchProvider("second")
    order_quotas = ProviderQuotaManager(web_limits={"first": 1, "second": 1}, llm_limits={})
    order_result = await _scheduler(
        [order_first, order_second],
        order_quotas,
    ).fetch_until_accepted(url)
    assert order_result.kind == "accepted"
    assert order_first.calls == [url]
    assert order_second.calls == []

    parallel_first = _ControlledFetchProvider("first", hold=True)
    parallel_second = _ControlledFetchProvider("second", hold=True)
    parallel_quotas = ProviderQuotaManager(web_limits={"first": 1, "second": 1}, llm_limits={})
    parallel_scheduler = _scheduler([parallel_first, parallel_second], parallel_quotas)
    url_two = normalize_url("https://example.com/two")
    task_one = asyncio.create_task(parallel_scheduler.fetch_until_accepted(url))
    await parallel_first.entered.wait()
    task_two = asyncio.create_task(parallel_scheduler.fetch_until_accepted(url_two))
    await parallel_second.entered.wait()
    assert parallel_first.active == 1
    assert parallel_second.active == 1
    assert parallel_first.max_active == 1
    assert parallel_second.max_active == 1
    parallel_first.release.set()
    parallel_second.release.set()
    results = await asyncio.gather(task_one, task_two)
    assert [result.kind for result in results] == ["accepted", "accepted"]
