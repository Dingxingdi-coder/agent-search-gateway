"""Resolved runtime assembly for the foreground daemon."""

import logging
from collections.abc import Callable, Mapping
from typing import cast

import httpx

from .concurrency import ProviderQuotaManager
from .config import ResolvedConfig, ResolvedWebProviderConfig
from .errors import ConfigFailure, ErrorCode
from .llm.stages import LLMStages
from .observability import log_event
from .orchestrators.fetch import FetchOrchestrator
from .orchestrators.search import SearchOrchestrator
from .paths import RuntimePaths
from .providers.contracts import KeywordSearchProvider, LLMClient, URLFetchProvider
from .providers.defaults import build_default_registry
from .providers.http import HttpJsonExecutor
from .providers.openai_chat import OpenAIChatCompletionsClient
from .providers.registry import ProviderRegistry
from .result_writer import ResultWriter
from .scheduler.fetch import FetchScheduler
from .url_store import URLStore

HttpClientFactory = Callable[[], httpx.AsyncClient]
_RESERVED_WEB_ADAPTER_KWARGS = frozenset({"name", "http_executor", "secret"})


class Runtime:
    """Owns one in-memory gateway runtime and its transport clients."""

    def __init__(
        self,
        *,
        quotas: ProviderQuotaManager,
        web_search_providers: tuple[KeywordSearchProvider, ...],
        web_fetch_providers: tuple[URLFetchProvider, ...],
        llm_clients: Mapping[str, LLMClient],
        store: URLStore,
        search_orchestrator: SearchOrchestrator,
        fetch_orchestrator: FetchOrchestrator,
        web_http_executors: tuple[HttpJsonExecutor, ...],
    ) -> None:
        self.quotas = quotas
        self.web_search_providers = web_search_providers
        self.web_fetch_providers = web_fetch_providers
        self.llm_clients = dict(llm_clients)
        self.store = store
        self.search_orchestrator = search_orchestrator
        self.fetch_orchestrator = fetch_orchestrator
        self._web_http_executors = web_http_executors
        self._closed = False

    @classmethod
    def build(
        cls,
        config: ResolvedConfig,
        paths: RuntimePaths,
        *,
        registry: ProviderRegistry | None = None,
        http_client_factory: HttpClientFactory = httpx.AsyncClient,
    ) -> "Runtime":
        provider_registry = registry or build_default_registry()
        enabled_web = tuple(
            item for item in config.web.providers if item.enable_search or item.enable_fetch
        )
        quotas = ProviderQuotaManager(
            web_limits={item.name: item.max_concurrency for item in enabled_web},
            llm_limits={item.name: item.max_concurrency for item in config.llm.providers},
        )
        web_search, web_fetch, web_executors = cls._build_web_providers(
            enabled_web,
            provider_registry,
            config,
            http_client_factory,
        )
        llm_clients = cls._build_llm_clients(config, quotas, http_client_factory)
        stages = LLMStages(
            llm_clients,
            judge=config.llm.judge,
            safety=config.llm.safety,
            content_clean=config.llm.content_clean,
            focus_summary=config.llm.focus_summary,
        )
        store = URLStore()
        search_orchestrator = SearchOrchestrator(
            keyword_providers=web_search,
            llm_invocations=config.llm.search_invocations,
            quotas=quotas,
            stages=stages,
            store=store,
            result_writer=ResultWriter(paths.results_dir),
        )
        fetch_orchestrator = FetchOrchestrator(
            store=store,
            scheduler=FetchScheduler(web_fetch, quotas, stages),
            stages=stages,
        )
        runtime = cls(
            quotas=quotas,
            web_search_providers=web_search,
            web_fetch_providers=web_fetch,
            llm_clients=llm_clients,
            store=store,
            search_orchestrator=search_orchestrator,
            fetch_orchestrator=fetch_orchestrator,
            web_http_executors=web_executors,
        )
        log_event(
            logging.getLogger(__name__),
            logging.DEBUG,
            "runtime_built",
            web_providers=",".join(item.name for item in enabled_web) or "-",
            web_provider_count=len(enabled_web),
            llm_providers=",".join(item.name for item in config.llm.providers) or "-",
            llm_provider_count=len(config.llm.providers),
            web_limits=",".join(
                f"{item.name}:{item.max_concurrency}" for item in enabled_web
            )
            or "-",
            llm_limits=",".join(
                f"{item.name}:{item.max_concurrency}" for item in config.llm.providers
            )
            or "-",
        )
        return runtime

    @classmethod
    def _build_web_providers(
        cls,
        providers: tuple[ResolvedWebProviderConfig, ...],
        registry: ProviderRegistry,
        config: ResolvedConfig,
        http_client_factory: HttpClientFactory,
    ) -> tuple[
        tuple[KeywordSearchProvider, ...],
        tuple[URLFetchProvider, ...],
        tuple[HttpJsonExecutor, ...],
    ]:
        search: list[KeywordSearchProvider] = []
        fetch: list[URLFetchProvider] = []
        executors: list[HttpJsonExecutor] = []
        for provider_config in providers:
            registration = registry.get(provider_config.name)
            private_value = provider_config.secret
            if registration is None or private_value is None:
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Invalid enabled web provider: {provider_config.name}",
                )
            reserved = set(provider_config.options) & _RESERVED_WEB_ADAPTER_KWARGS
            if reserved:
                names = ", ".join(sorted(reserved))
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Reserved config key(s) for web provider {provider_config.name}: {names}",
                )
            executor = HttpJsonExecutor(
                http_client_factory(),
                config.retry,
                provider_name=provider_config.name,
            )
            kwargs: dict[str, object] = {
                "name": provider_config.name,
                "http_executor": executor,
                "secret": private_value,
            }
            kwargs.update(provider_config.options)
            try:
                adapter = registration.factory(**kwargs)
            except TypeError as exc:
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Invalid configuration for web provider {provider_config.name}",
                ) from exc
            executors.append(executor)
            if provider_config.enable_search:
                search.append(cast(KeywordSearchProvider, adapter))
            if provider_config.enable_fetch:
                fetch.append(cast(URLFetchProvider, adapter))
        return tuple(search), tuple(fetch), tuple(executors)

    @staticmethod
    def _build_llm_clients(
        config: ResolvedConfig,
        quotas: ProviderQuotaManager,
        http_client_factory: HttpClientFactory,
    ) -> dict[str, LLMClient]:
        clients: dict[str, LLMClient] = {}
        for provider_config in config.llm.providers:
            private_value = provider_config.secret
            executor = HttpJsonExecutor(
                http_client_factory(),
                config.retry,
                provider_name=provider_config.name,
            )
            clients[provider_config.name] = OpenAIChatCompletionsClient(
                name=provider_config.name,
                api_url=provider_config.api_url,
                secret=private_value,
                executor=executor,
                quota=quotas.get_llm(provider_config.name),
                retry_policy=config.retry,
            )
        return clients

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for executor in self._web_http_executors:
            await executor.aclose()
        for client in self.llm_clients.values():
            await client.aclose()

    def __repr__(self) -> str:
        return (
            "Runtime("
            f"web_search={len(self.web_search_providers)}, "
            f"web_fetch={len(self.web_fetch_providers)}, "
            f"llm_clients={len(self.llm_clients)}"
            ")"
        )
