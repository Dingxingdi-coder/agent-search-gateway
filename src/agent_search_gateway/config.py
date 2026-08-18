"""TOML and environment configuration resolution."""

import copy
import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .errors import ConfigFailure, ErrorCode
from .models import LLMInvocation, RetryPolicy
from .observability import SecretValue
from .providers.registry import ProviderRegistry, WebProviderRegistration

_WEB_SHARED_KEYS = frozenset({"enable_search", "enable_fetch", "api_key_env", "max_concurrency"})


@dataclass(frozen=True, slots=True)
class ResolvedWebProviderConfig:
    name: str
    enable_search: bool
    enable_fetch: bool
    max_concurrency: int
    api_key_env: str | None
    secret: SecretValue | None
    options: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ResolvedWebProviderGroup:
    default_max_concurrency: int
    providers: tuple[ResolvedWebProviderConfig, ...]


def _config_error(message: str) -> ConfigFailure:
    return ConfigFailure(ErrorCode.CONFIG_ERROR, message)


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _config_error(f"{label} must be a table")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _config_error(f"{label} must be a positive integer")
    return value


def _optional_bool(table: Mapping[str, object], key: str) -> bool:
    value = table.get(key, False)
    if not isinstance(value, bool):
        raise _config_error(f"{key} must be a boolean")
    return value


def _validate_options(
    registration: WebProviderRegistration,
    options: Mapping[str, object],
) -> None:
    unknown = set(options) - registration.allowed_config_keys
    if unknown:
        names = ", ".join(sorted(unknown))
        raise _config_error(f"unknown config key(s) for {registration.name}: {names}")


def _resolve_one_web_provider(
    name: str,
    table: Mapping[str, object],
    default_max_concurrency: int,
    registry: ProviderRegistry,
    environ: Mapping[str, str],
) -> ResolvedWebProviderConfig:
    enable_search = _optional_bool(table, "enable_search")
    enable_fetch = _optional_bool(table, "enable_fetch")
    options = {key: value for key, value in table.items() if key not in _WEB_SHARED_KEYS}
    registration = registry.get(name)

    if registration is not None:
        _validate_options(registration, options)

    max_concurrency = _positive_int(
        table.get("max_concurrency", default_max_concurrency),
        f"web provider {name} max_concurrency",
    )

    if not enable_search and not enable_fetch:
        return ResolvedWebProviderConfig(
            name=name,
            enable_search=False,
            enable_fetch=False,
            max_concurrency=max_concurrency,
            api_key_env=None,
            secret=None,
            options=MappingProxyType(dict(options)),
        )

    if registration is None:
        raise _config_error(f"unknown enabled web provider: {name}")
    if enable_search and not registration.capabilities.search:
        raise _config_error(f"web provider {name} does not support search")
    if enable_fetch and not registration.capabilities.fetch:
        raise _config_error(f"web provider {name} does not support fetch")

    api_key_env = table.get("api_key_env")
    if not isinstance(api_key_env, str) or not api_key_env.strip():
        raise _config_error(f"web provider {name} requires api_key_env")
    secret_text = environ.get(api_key_env)
    if not secret_text:
        raise _config_error(f"environment variable {api_key_env} is required")

    return ResolvedWebProviderConfig(
        name=name,
        enable_search=enable_search,
        enable_fetch=enable_fetch,
        max_concurrency=max_concurrency,
        api_key_env=api_key_env,
        secret=SecretValue(secret_text),
        options=MappingProxyType(dict(options)),
    )


def resolve_web_provider_config(
    data: Mapping[str, object],
    registry: ProviderRegistry,
    environ: Mapping[str, str],
) -> ResolvedWebProviderGroup:
    web_table = _require_mapping(data.get("web_providers", {}), "web_providers")
    default_max_concurrency = _positive_int(
        web_table.get("default_max_concurrency", 3),
        "web_providers.default_max_concurrency",
    )

    providers: list[ResolvedWebProviderConfig] = []
    for name, value in web_table.items():
        if name == "default_max_concurrency":
            continue
        provider_table = _require_mapping(value, f"web_providers.{name}")
        providers.append(
            _resolve_one_web_provider(
                name,
                provider_table,
                default_max_concurrency,
                registry,
                environ,
            )
        )
    return ResolvedWebProviderGroup(default_max_concurrency, tuple(providers))


@dataclass(frozen=True, slots=True)
class LLMProviderConfig:
    name: str
    protocol: str
    api_endpoint: str
    api_url: str
    api_key_env: str
    secret: SecretValue
    max_concurrency: int


@dataclass(frozen=True, slots=True)
class ResolvedLLMConfig:
    default_max_concurrency: int
    providers: tuple[LLMProviderConfig, ...]
    search_invocations: tuple[LLMInvocation, ...]
    judge: LLMInvocation
    safety: LLMInvocation
    content_clean: LLMInvocation
    focus_summary: LLMInvocation


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    web: ResolvedWebProviderGroup
    llm: ResolvedLLMConfig
    retry: RetryPolicy


def _required_string(table: Mapping[str, object], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _config_error(f"{label}.{key} must be a non-empty string")
    return value


def _first_defined(key: str, *tables: Mapping[str, object]) -> object | None:
    for table in tables:
        if key in table:
            return table[key]
    return None


def _resolve_extra_body(*tables: Mapping[str, object]) -> dict[str, object]:
    value = _first_defined("extra_body", *tables)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _config_error("LLM extra_body must be a table")
    return copy.deepcopy(value)


def _resolve_invocation(
    label: str,
    scopes: tuple[Mapping[str, object], ...],
    *,
    require_explicit_provider: bool = False,
) -> LLMInvocation:
    provider_value = (
        scopes[0].get("provider")
        if require_explicit_provider
        else _first_defined("provider", *scopes)
    )
    model_value = _first_defined("model", *scopes)
    if not isinstance(provider_value, str) or not provider_value.strip():
        raise _config_error(f"{label} requires provider")
    if not isinstance(model_value, str) or not model_value.strip():
        raise _config_error(f"{label} requires model")
    return LLMInvocation(
        provider=provider_value,
        model=model_value,
        extra_body=_resolve_extra_body(*scopes),
    )


def _search_invocations(
    data: Mapping[str, object],
    global_table: Mapping[str, object],
) -> tuple[LLMInvocation, ...]:
    search_table = _require_mapping(data.get("search_llm", {}), "search_llm")
    raw_entries = search_table.get("providers", [])
    if not isinstance(raw_entries, list):
        raise _config_error("search_llm.providers must be an array of tables")

    resolved: list[LLMInvocation] = []
    for index, raw_entry in enumerate(raw_entries):
        entry = _require_mapping(raw_entry, f"search_llm.providers[{index}]")
        resolved.append(
            _resolve_invocation(
                f"search_llm.providers[{index}]",
                (entry, global_table),
                require_explicit_provider=True,
            )
        )
    return tuple(resolved)


def _fetch_invocations(
    data: Mapping[str, object],
    global_table: Mapping[str, object],
) -> tuple[LLMInvocation, LLMInvocation, LLMInvocation, LLMInvocation]:
    fetch_table = _require_mapping(data.get("fetch_llm", {}), "fetch_llm")
    names = ("judge", "safety", "content_clean", "focus_summary")
    invocations: list[LLMInvocation] = []
    for name in names:
        stage_table = _require_mapping(fetch_table.get(name, {}), f"fetch_llm.{name}")
        invocations.append(
            _resolve_invocation(
                f"fetch_llm.{name}",
                (stage_table, fetch_table, global_table),
            )
        )
    return invocations[0], invocations[1], invocations[2], invocations[3]


def _resolve_referenced_llm_providers(
    llm_table: Mapping[str, object],
    referenced: set[str],
    default_max_concurrency: int,
    environ: Mapping[str, str],
) -> tuple[LLMProviderConfig, ...]:
    providers: list[LLMProviderConfig] = []
    for name, raw_value in llm_table.items():
        if name == "default_max_concurrency" or name not in referenced:
            continue
        table = _require_mapping(raw_value, f"llm_providers.{name}")
        protocol = _required_string(table, "protocol", f"llm_providers.{name}")
        endpoint = _required_string(table, "api_endpoint", f"llm_providers.{name}")
        if protocol != "openai" or endpoint != "chat_completions":
            raise _config_error(f"unsupported LLM provider protocol/endpoint: {name}")
        api_url = _required_string(table, "api_url", f"llm_providers.{name}")
        api_key_env = _required_string(table, "api_key_env", f"llm_providers.{name}")
        secret_text = environ.get(api_key_env)
        if not secret_text:
            raise _config_error(f"environment variable {api_key_env} is required")
        max_concurrency = _positive_int(
            table.get("max_concurrency", default_max_concurrency),
            f"llm provider {name} max_concurrency",
        )
        providers.append(
            LLMProviderConfig(
                name=name,
                protocol=protocol,
                api_endpoint=endpoint,
                api_url=api_url,
                api_key_env=api_key_env,
                secret=SecretValue(secret_text),
                max_concurrency=max_concurrency,
            )
        )

    configured = {provider.name for provider in providers}
    missing = referenced - configured
    if missing:
        raise _config_error(f"unknown referenced LLM provider(s): {', '.join(sorted(missing))}")
    return tuple(providers)


def resolve_llm_config(
    data: Mapping[str, object],
    environ: Mapping[str, str],
) -> ResolvedLLMConfig:
    llm_table = _require_mapping(data.get("llm_providers", {}), "llm_providers")
    default_max_concurrency = _positive_int(
        llm_table.get("default_max_concurrency", 2),
        "llm_providers.default_max_concurrency",
    )
    global_table = _require_mapping(data.get("global_default_llm", {}), "global_default_llm")
    search_invocations = _search_invocations(data, global_table)
    judge, safety, content_clean, focus_summary = _fetch_invocations(data, global_table)
    all_invocations = (*search_invocations, judge, safety, content_clean, focus_summary)
    providers = _resolve_referenced_llm_providers(
        llm_table,
        {invocation.provider for invocation in all_invocations},
        default_max_concurrency,
        environ,
    )
    return ResolvedLLMConfig(
        default_max_concurrency=default_max_concurrency,
        providers=providers,
        search_invocations=search_invocations,
        judge=judge,
        safety=safety,
        content_clean=content_clean,
        focus_summary=focus_summary,
    )


def _positive_float(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise _config_error(f"{label} must be a positive number")
    return float(value)


def resolve_retry_policy(data: Mapping[str, object]) -> RetryPolicy:
    retry_table = _require_mapping(data.get("retry", {}), "retry")
    return RetryPolicy(
        max_attempts=_positive_int(retry_table.get("max_attempts", 3), "retry.max_attempts"),
        base_delay_seconds=_positive_float(
            retry_table.get("base_delay_seconds", 0.25),
            "retry.base_delay_seconds",
        ),
        max_delay_seconds=_positive_float(
            retry_table.get("max_delay_seconds", 2.0),
            "retry.max_delay_seconds",
        ),
        request_timeout_seconds=_positive_float(
            retry_table.get("request_timeout_seconds", 30.0),
            "retry.request_timeout_seconds",
        ),
    )


def resolve_config(
    data: Mapping[str, object],
    registry: ProviderRegistry,
    environ: Mapping[str, str],
) -> ResolvedConfig:
    return ResolvedConfig(
        web=resolve_web_provider_config(data, registry, environ),
        llm=resolve_llm_config(data, environ),
        retry=resolve_retry_policy(data),
    )


def load_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _config_error(f"failed to load config: {exc}") from exc
