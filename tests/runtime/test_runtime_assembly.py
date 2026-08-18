from pathlib import Path

import httpx
import pytest

from agent_search_gateway.config import resolve_config
from agent_search_gateway.errors import ConfigFailure
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.runtime import Runtime


class _CountingAsyncClient(httpx.AsyncClient):
    def __init__(self) -> None:
        super().__init__(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request))
        )
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


def _config() -> dict[str, object]:
    return {
        "web_providers": {
            "default_max_concurrency": 3,
            "tavily": {
                "enable_search": True,
                "enable_fetch": True,
                "api_key_env": "ENV_A",
                "api_url": "https://tavily.example.test",
            },
            "brave": {
                "enable_search": True,
                "enable_fetch": False,
                "api_key_env": "ENV_B",
                "api_url": "https://brave.example.test",
            },
            "tinyfish": {
                "enable_search": False,
                "enable_fetch": True,
                "api_key_env": "ENV_C",
                "search_api_url": "https://search.tiny.example.test",
                "fetch_api_url": "https://fetch.tiny.example.test",
                "max_concurrency": 4,
            },
            "firecrawl": {
                "enable_search": False,
                "enable_fetch": False,
                "api_url": "https://disabled.example.test",
            },
        },
        "llm_providers": {
            "default_max_concurrency": 2,
            "primary": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "api_url": "https://llm-primary.example.test",
                "api_key_env": "ENV_D",
            },
            "secondary": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "api_url": "https://llm-secondary.example.test",
                "api_key_env": "ENV_E",
                "max_concurrency": 5,
            },
        },
        "global_default_llm": {"provider": "primary", "model": "global"},
        "search_llm": {"providers": [{"provider": "secondary", "model": "search"}]},
        "fetch_llm": {
            "judge": {},
            "safety": {},
            "content_clean": {},
            "focus_summary": {},
        },
        "retry": {"max_attempts": 2},
    }


async def test_runtime_assembly_builds_enabled_adapters_with_shared_quotas_and_closes_clients(
    tmp_path: Path,
) -> None:
    registry = build_default_registry()
    assert [
        (item.name, item.capabilities.search, item.capabilities.fetch)
        for item in registry.list_in_registration_order()
    ] == [
        ("tavily", True, True),
        ("firecrawl", True, True),
        ("exa", True, True),
        ("linkup", True, True),
        ("brave", True, False),
        ("anysearch", True, False),
        ("tinyfish", True, True),
    ]
    environment = {
        "ENV_A": "x",
        "ENV_B": "x",
        "ENV_C": "x",
        "ENV_D": "x",
        "ENV_E": "x",
    }
    resolved = resolve_config(_config(), registry, environment)
    clients: list[_CountingAsyncClient] = []

    def client_factory() -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    runtime = Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=client_factory,
    )
    assert [provider.name for provider in runtime.web_search_providers] == [
        "tavily",
        "brave",
    ]
    assert [provider.name for provider in runtime.web_fetch_providers] == [
        "tavily",
        "tinyfish",
    ]
    assert id(runtime.web_search_providers[0]) == id(runtime.web_fetch_providers[0])
    assert set(runtime.llm_clients) == {"primary", "secondary"}
    assert runtime.quotas.get_web("tavily").limit == 3
    assert runtime.quotas.get_web("tinyfish").limit == 4
    assert runtime.quotas.get_llm("primary").limit == 2
    assert runtime.quotas.get_llm("secondary").limit == 5
    assert "x" not in repr(runtime)

    await runtime.aclose()
    assert len(clients) == 5
    assert all(client.close_calls == 1 for client in clients)

    bad = _config()
    web = bad["web_providers"]
    assert isinstance(web, dict)
    brave = web["brave"]
    assert isinstance(brave, dict)
    brave["enable_fetch"] = True
    with pytest.raises(ConfigFailure):
        resolve_config(bad, registry, environment)
