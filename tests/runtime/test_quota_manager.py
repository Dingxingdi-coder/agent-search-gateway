import asyncio

from agent_search_gateway.concurrency import ProviderQuotaManager


async def test_web_search_and_fetch_share_capacity_while_llm_capacity_is_separate() -> None:
    quotas = ProviderQuotaManager(web_limits={"shared": 1}, llm_limits={"shared": 1})
    web_gate = quotas.get_web("shared")
    llm_gate = quotas.get_llm("shared")

    first_web_entered = asyncio.Event()
    release_first_web = asyncio.Event()
    second_web_entered = asyncio.Event()
    llm_entered = asyncio.Event()

    async def hold_web() -> None:
        async with web_gate.lease():
            first_web_entered.set()
            await release_first_web.wait()

    async def wait_for_web() -> None:
        async with web_gate.lease():
            second_web_entered.set()

    async def use_llm() -> None:
        async with llm_gate.lease():
            llm_entered.set()

    first_task = asyncio.create_task(hold_web())
    await first_web_entered.wait()
    second_task = asyncio.create_task(wait_for_web())
    llm_task = asyncio.create_task(use_llm())
    await llm_entered.wait()
    await asyncio.sleep(0)

    assert not second_web_entered.is_set()
    assert web_gate.in_use == 1
    assert llm_gate.in_use == 0

    release_first_web.set()
    await second_web_entered.wait()
    await asyncio.gather(first_task, second_task, llm_task)

    assert web_gate.in_use == 0
    assert llm_gate.in_use == 0
    assert web_gate.max_observed_in_use == 1
    assert llm_gate.max_observed_in_use == 1
