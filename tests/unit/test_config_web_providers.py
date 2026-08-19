import pytest

from agent_search_gateway.config import resolve_web_provider_config
from agent_search_gateway.errors import ConfigFailure, ErrorCode
from agent_search_gateway.providers.contracts import ProviderCapabilities
from agent_search_gateway.providers.registry import ProviderRegistry, WebProviderRegistration


def _factory() -> object:
    return object()


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        WebProviderRegistration(
            name="dual",
            capabilities=ProviderCapabilities(search=True, fetch=True),
            factory=_factory,
            allowed_config_keys=frozenset({"api_url", "mode"}),
        )
    )
    registry.register(
        WebProviderRegistration(
            name="search_only",
            capabilities=ProviderCapabilities(search=True, fetch=False),
            factory=_factory,
            allowed_config_keys=frozenset({"api_url"}),
        )
    )
    return registry


def test_resolve_web_provider_config_or_fail_startup() -> None:
    data = {
        "web_providers": {
            "default_max_concurrency": 3,
            "dual": {
                "enable_search": True,
                "enable_fetch": True,
                "api_key_env": "DUAL_KEY",
                "max_concurrency": 5,
                "api_url": "https://api.example.test",
                "mode": "fast",
            },
            "search_only": {
                "enable_search": False,
                "enable_fetch": False,
                "api_url": "https://disabled.example.test",
            },
        }
    }

    resolved = resolve_web_provider_config(data, _registry(), {"DUAL_KEY": "super-secret"})
    assert resolved.default_max_concurrency == 3
    dual = resolved.providers[0]
    assert dual.name == "dual"
    assert dual.enable_search is True
    assert dual.enable_fetch is True
    assert dual.max_concurrency == 5
    assert dual.api_key_env == "DUAL_KEY"
    assert dual.secret is not None
    assert dual.secret.reveal() == "super-secret"
    assert "super-secret" not in repr(dual.secret)
    assert dict(dual.options) == {
        "api_url": "https://api.example.test",
        "mode": "fast",
    }

    disabled = resolved.providers[1]
    assert disabled.name == "search_only"
    assert disabled.enable_search is False
    assert disabled.secret is None


@pytest.mark.parametrize(
    ("provider_name", "provider_data", "environment"),
    [
        ("missing", {"enable_search": True, "api_key_env": "KEY"}, {"KEY": "x"}),
        (
            "search_only",
            {"enable_fetch": True, "api_key_env": "KEY", "api_url": "https://x"},
            {"KEY": "x"},
        ),
        ("dual", {"enable_search": True, "api_key_env": "MISSING"}, {}),
        ("dual", {"enable_search": True, "api_key_env": "KEY", "max_concurrency": 0}, {"KEY": "x"}),
        (
            "dual",
            {"enable_search": True, "api_key_env": "KEY", "unknown": True},
            {"KEY": "x"},
        ),
    ],
)
def test_resolve_web_provider_config_rejects_invalid_enabled_provider(
    provider_name: str,
    provider_data: dict[str, object],
    environment: dict[str, str],
) -> None:
    data = {"web_providers": {"default_max_concurrency": 3, provider_name: provider_data}}
    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(data, _registry(), environment)
    assert caught.value.code is ErrorCode.CONFIG_ERROR
