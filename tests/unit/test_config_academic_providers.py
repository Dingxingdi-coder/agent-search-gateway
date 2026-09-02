import pytest

from agent_search_gateway.config import (
    resolve_academic_provider_config,
    resolve_oa_resolver_config,
)
from agent_search_gateway.errors import ConfigFailure, ErrorCode
from agent_search_gateway.providers.academic.registry import (
    AcademicProviderRegistration,
    AcademicProviderRegistry,
    OAResolverRegistration,
    OAResolverRegistry,
    Requirement,
)


def _factory(**_kwargs: object) -> object:
    return object()


def _academic_registry() -> AcademicProviderRegistry:
    registry = AcademicProviderRegistry()
    registrations: tuple[tuple[str, Requirement, Requirement], ...] = (
        ("no_auth", "none", "none"),
        ("optional", "optional", "optional"),
        ("required", "required", "required"),
    )
    for name, authentication, contact in registrations:
        registry.register(
            AcademicProviderRegistration(
                name=name,
                factory=_factory,
                allowed_config_keys=frozenset({"api_url"}),
                authentication=authentication,
                contact=contact,
            )
        )
    return registry


def _resolver_registry() -> OAResolverRegistry:
    registry = OAResolverRegistry()
    registry.register(
        OAResolverRegistration(
            name="unpaywall",
            factory=_factory,
            allowed_config_keys=frozenset({"api_url"}),
            authentication="none",
            contact="required",
        )
    )
    return registry


def test_academic_config_supports_none_optional_and_required_modes() -> None:
    data = {
        "academic_providers": {
            "default_max_concurrency": 4,
            "no_auth": {"enabled": True, "api_url": "https://no-auth.example/v1"},
            "optional": {"enabled": True},
            "required": {
                "enabled": True,
                "api_key_env": "AUTH_ENV",
                "contact_email_env": "CONTACT_ENV",
            },
        }
    }
    resolved = resolve_academic_provider_config(
        data,
        _academic_registry(),
        {"AUTH_ENV": "[REDACTED_SECRET]", "CONTACT_ENV": "[REDACTED_SECRET]"},
    )
    assert resolved.default_max_concurrency == 4
    assert [item.name for item in resolved.providers] == ["no_auth", "optional", "required"]
    assert resolved.providers[0].api_key is None
    assert resolved.providers[1].api_key is None
    assert resolved.providers[2].api_key is not None
    assert resolved.providers[2].contact_email is not None


@pytest.mark.parametrize(
    ("provider", "table", "environment"),
    [
        ("missing", {"enabled": True}, {}),
        ("no_auth", {"enabled": True, "api_key_env": "AUTH_ENV"}, {}),
        ("optional", {"enabled": True, "api_key_env": "MISSING_ENV"}, {}),
        ("optional", {"enabled": True, "contact_email_env": "MISSING_ENV"}, {}),
        ("required", {"enabled": True}, {}),
        ("optional", {"enabled": True, "max_concurrency": 0}, {}),
        ("optional", {"enabled": "yes"}, {}),
        ("optional", {"enabled": True, "api_url": "ftp://invalid.example"}, {}),
        ("optional", {"enabled": True, "unknown": True}, {}),
        ("optional", {"enabled": True, "executor": "reserved"}, {}),
    ],
)
def test_academic_config_rejects_invalid_configuration(
    provider: str,
    table: dict[str, object],
    environment: dict[str, str],
) -> None:
    with pytest.raises(ConfigFailure) as caught:
        resolve_academic_provider_config(
            {"academic_providers": {provider: table}},
            _academic_registry(),
            environment,
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR


def test_disabled_required_provider_does_not_require_environment_values() -> None:
    resolved = resolve_academic_provider_config(
        {"academic_providers": {"required": {"enabled": False}}},
        _academic_registry(),
        {},
    )
    assert len(resolved.providers) == 1
    assert resolved.providers[0].enabled is False
    assert resolved.providers[0].api_key is None
    assert resolved.providers[0].contact_email is None


def test_unpaywall_is_optional_but_enabled_instance_requires_contact() -> None:
    registry = _resolver_registry()
    assert resolve_oa_resolver_config({}, registry, {}) is None
    assert (
        resolve_oa_resolver_config(
            {"oa_resolvers": {"unpaywall": {"enabled": False}}}, registry, {}
        )
        is None
    )

    data = {"oa_resolvers": {"unpaywall": {"enabled": True, "contact_email_env": "CONTACT_ENV"}}}
    with pytest.raises(ConfigFailure):
        resolve_oa_resolver_config(data, registry, {})
    resolved = resolve_oa_resolver_config(
        data,
        registry,
        {"CONTACT_ENV": "[REDACTED_SECRET]"},
    )
    assert resolved is not None
    assert resolved.contact_email is not None

    registry.register(
        OAResolverRegistration(
            name="other",
            factory=_factory,
            allowed_config_keys=frozenset(),
            authentication="none",
            contact="none",
        )
    )
    with pytest.raises(ConfigFailure, match="only one OA resolver may be enabled"):
        resolve_oa_resolver_config(
            {
                "oa_resolvers": {
                    "unpaywall": {
                        "enabled": True,
                        "contact_email_env": "CONTACT_ENV",
                    },
                    "other": {"enabled": True},
                }
            },
            registry,
            {"CONTACT_ENV": "[REDACTED_SECRET]"},
        )
