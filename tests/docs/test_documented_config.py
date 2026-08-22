import argparse
import re
from pathlib import Path

from agent_search_gateway.cli import build_parser
from agent_search_gateway.config import load_toml, resolve_config
from agent_search_gateway.providers.academic.defaults import (
    build_default_academic_registry,
    build_default_oa_resolver_registry,
)
from agent_search_gateway.providers.defaults import build_default_registry

_ROOT = Path(__file__).parents[2]


def _stub_environment(data: dict[str, object]) -> dict[str, str]:
    names: set[str] = set()
    web = data.get("web_providers")
    if isinstance(web, dict):
        for value in web.values():
            if isinstance(value, dict):
                name = value.get("api_key_env")
                if isinstance(name, str):
                    names.add(name)
    llm = data.get("llm_providers")
    if isinstance(llm, dict):
        for value in llm.values():
            if isinstance(value, dict):
                name = value.get("api_key_env")
                if isinstance(name, str):
                    names.add(name)
    academic = data.get("academic_providers")
    if isinstance(academic, dict):
        for value in academic.values():
            if not isinstance(value, dict):
                continue
            for key in ("api_key_env", "contact_email_env"):
                name = value.get(key)
                if isinstance(name, str):
                    names.add(name)
    resolvers = data.get("oa_resolvers")
    if isinstance(resolvers, dict):
        for value in resolvers.values():
            if not isinstance(value, dict):
                continue
            for key in ("api_key_env", "contact_email_env"):
                name = value.get(key)
                if isinstance(name, str):
                    names.add(name)
    return {name: "x" for name in names}


def test_example_config_loads_with_stub_secrets_and_readme_commands_match_cli_help() -> None:
    config_path = _ROOT / "config.example.toml"
    readme_path = _ROOT / "README.md"
    data = load_toml(config_path)
    registry = build_default_registry()
    academic_registry = build_default_academic_registry()
    resolver_registry = build_default_oa_resolver_registry()
    resolved = resolve_config(
        data,
        registry,
        _stub_environment(data),
        academic_registry=academic_registry,
        oa_resolver_registry=resolver_registry,
    )

    assert resolved.web.providers
    for configured in resolved.web.providers:
        registration = registry.get(configured.name)
        assert registration is not None
        if configured.enable_search:
            assert registration.capabilities.search
        if configured.enable_fetch:
            assert registration.capabilities.fetch

    parallel = next(item for item in resolved.web.providers if item.name == "parallel")
    assert parallel.enable_search is True
    assert parallel.enable_fetch is True
    assert dict(parallel.options) == {
        "api_url": "https://api.parallel.ai",
        "mode": "turbo",
        "search_fetch_policy": {
            "max_age_seconds": 3600,
            "timeout_seconds": 15,
            "disable_cache_fallback": False,
        },
        "extract_fetch_policy": {
            "max_age_seconds": 600,
            "timeout_seconds": 30,
            "disable_cache_fallback": True,
        },
    }

    readme = readme_path.read_text(encoding="utf-8")
    documented = set(
        re.findall(
            (
                r"^agent-search-gateway "
                r"(start|stop|doctor|keyword-search|paper-search|llm-search|url-fetch)\b"
            ),
            readme,
            flags=re.MULTILINE,
        )
    )
    assert documented == {
        "start",
        "stop",
        "doctor",
        "keyword-search",
        "paper-search",
        "llm-search",
        "url-fetch",
    }

    parser = build_parser()
    action = next(item for item in parser._actions if isinstance(item, argparse._SubParsersAction))
    assert set(action.choices) == documented
