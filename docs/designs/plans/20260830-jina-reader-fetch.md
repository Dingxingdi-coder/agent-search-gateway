# Jina Reader Fetch Provider Implementation Plan

**Goal:** Add a built-in, fetch-only `jina` web provider that uses Jina Reader's unauthenticated Reader endpoint, forces Reader-side freshness on every actual Jina request with `X-No-Cache: true`, and preserves the gateway's existing fetch scheduling, fallback, caching, storage, protocol, and error contracts.

**Architecture:** `docs/designs/architectures/20260830-jina-reader-fetch.md`

**Error handling:** `docs/designs/error-handlings/20260830-jina-reader-fetch.md`

**Testing:** `docs/designs/testings/20260830-jina-reader-fetch.md`

---

## Baseline and implementation boundaries

All paths and commands in this plan are relative to the existing task worktree:

```text
.worktrees/design-jina-reader-fetch/
```

Current worktree baseline before implementation:

```text
uv run pytest -q
1 failed, 521 passed, 4 skipped
```

The existing failure is unrelated to Jina:

```text
tests/docs/test_documented_config.py::
test_example_config_and_readme_document_all_new_provider_contracts

The test requires every listed provider's api_key_env to equal one fixed redaction
placeholder, while config.example.toml currently contains a normal environment
variable name for Decodo.
```

Task 9 must replace that brittle literal-value assertion with the real contract:
credential-required providers have a non-empty `api_key_env`, while credentialless
Jina must omit `api_key_env`. After that focused test refactor, the test must remain
RED because Jina is not yet documented, then turn GREEN when the example and README
are updated. Do not hide or broadly delete the existing credential assertions.

Static-analysis baseline:

```text
uv run ruff check .
All checks passed!

uv run mypy src tests
Success: no issues found in 164 source files
```

### Intended implementation footprint

```text
Create:
  src/agent_search_gateway/providers/web/jina.py
  tests/providers/web/test_jina.py
  docs/designs/plans/20260830-jina-reader-fetch.md

Modify:
  src/agent_search_gateway/providers/registry.py
  src/agent_search_gateway/providers/defaults.py
  src/agent_search_gateway/config.py
  src/agent_search_gateway/runtime.py

  tests/providers/test_registry.py
  tests/unit/test_config_web_providers.py
  tests/runtime/test_runtime_assembly.py
  tests/docs/test_documented_config.py

  config.example.toml
  README.md
```

No fixture file or new test double is needed. Reuse
`tests/support/http.py::RecordingTextExecutor` unchanged.

### Core files to consume, not modify

The following files define the stable pipeline that Jina must enter through normal
provider contracts. Do not modify them unless a newly failing, provider-agnostic
regression proves the approved design cannot be implemented otherwise:

```text
src/agent_search_gateway/providers/contracts.py
src/agent_search_gateway/providers/web/common.py
src/agent_search_gateway/providers/http.py
src/agent_search_gateway/scheduler/fetch.py
src/agent_search_gateway/orchestrators/fetch.py
src/agent_search_gateway/url_store.py
src/agent_search_gateway/concurrency.py
src/agent_search_gateway/errors.py
src/agent_search_gateway/models.py
src/agent_search_gateway/protocol.py
src/agent_search_gateway/daemon.py
src/agent_search_gateway/cli.py
```

The existing fetch flow remains:

```text
URLFetchRequest
  -> FetchOrchestrator.url_fetch
       normalize + singleflight + per-URL lock
       require an admitted and available URL
       if URLStore already has content:
         return the prepared gateway content after the existing safety step
         do not call any provider
       otherwise:
         -> FetchScheduler.fetch_until_accepted
              choose configured providers in order subject to web quota capacity
              call provider.fetch(normalized_url)
              validate URLFetchCandidate
              run cheap_check and the existing LLM judge
              fall back after execution or semantic failure
         merge accepted raw/content into URLStore
       run existing content-clean/safety/focus-summary behavior as needed
```

Jina therefore needs no scheduler, orchestrator, store, protocol, or request-model
branch.

### Locked adapter interface

```python
class JinaReaderAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        http_executor: TextRequester,
    ) -> None: ...

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate: ...
```

Locked request mapping:

```text
POST <configured api_url>
stage = "fetch"
headers = {"X-No-Cache": "true"}
params = None
json_body = {"url": str(normalized_url)}
Authorization = absent

successful non-whitespace response text
  -> URLFetchCandidate(raw_content=text, content=text)
```

Keep the normalized target URL in the JSON body. Do not use Jina's prefix-style GET
form, append the target to the outer endpoint, add a transport logging override, or
log the request body. This lets the existing HTTP lifecycle logger expose only the
configured Reader endpoint, not a nested target URL.

### Locked credential contract

Extend the generic registration metadata only:

```python
@dataclass(frozen=True, slots=True)
class WebProviderRegistration:
    name: str
    capabilities: ProviderCapabilities
    factory: WebProviderFactory
    allowed_config_keys: frozenset[str]
    requires_api_key: bool = True
```

All existing registrations inherit `True`. Jina alone is registered with
`requires_api_key=False` for this feature. An enabled credentialless provider must:

```text
omit api_key_env
resolve api_key_env=None
resolve secret=None
be constructed without a secret keyword argument
```

Do not make credentials optional globally, accept and ignore a Jina credential,
create a dummy `SecretValue`, or add a Jina-name conditional in config/runtime.

### Locked refresh boundary

`X-No-Cache: true` means only:

```text
when JinaReaderAdapter.fetch is actually invoked,
ask Jina Reader to bypass its own cached page result
```

It does not mean:

```text
bypass already prepared URLStore content
add force_refresh to URLFetchRequest
change singleflight keys or URL locks
clear or overwrite stored content
change provider ordering
```

The existing `FetchOrchestrator._prepare_content()` cache short-circuit remains the
gateway contract. No Jina-specific orchestrator test is needed; retain the existing
cached-content regression test in the final gate.

### Explicit non-goals

Do not add:

```text
Jina Search (s.jina.ai)
Jina API-key or optional-auth support
provider-specific engine/selector/wait/proxy/locale/format/screenshot/DNT controls
Jina-specific retry or textual error heuristics
requests-per-minute scheduling
cost-aware/free-first provider sorting
gateway-wide freshness/cache policy
new ErrorCode values or exception classes
new public CLI/daemon/protocol fields
new URLStore fields or persisted Jina metadata
live-network Jina tests
```

---

### Task 1: Implement the minimal successful Jina Reader fetch request

**Files:**
- Create: `tests/providers/web/test_jina.py`
- Create: `src/agent_search_gateway/providers/web/jina.py`
- Reference: `tests/support/http.py:7-15,65-88`
- Reference: `src/agent_search_gateway/providers/contracts.py:23-26,63-67`
- Reference: `src/agent_search_gateway/providers/web/common.py:22-31`
- Reference: `src/agent_search_gateway/providers/web/zenrows.py:11-37`

- [ ] **Step 1: Write the failing successful-fetch contract test**

Create
`test_jina_fetch_posts_normalized_target_with_no_cache_and_maps_text` using the
existing `RecordingTextExecutor`.

Use a normalized target containing a path, query, and fragment, for example:

```text
https://example.com/path/to/page?q=one&second=two#section
```

Use a response sentinel whose exact whitespace matters, such as:

```text
# Title

Body
```

Scenario and assertions:

```text
Construct JinaReaderAdapter with:
  name="jina"
  api_url="https://reader.example.test/"
  http_executor=RecordingTextExecutor([sentinel])

Call await adapter.fetch(target).

Assert the result equals URLFetchCandidate(sentinel, sentinel).
Assert exactly one HTTP request was recorded.
Assert request.method == "POST".
Assert request.url == "https://reader.example.test".
Assert request.stage == "fetch".
Assert request.headers == {"X-No-Cache": "true"}.
Assert request.params is None.
Assert request.json_body == {"url": str(target)}.
Assert request.response_mode == "text".
Assert str(target) is not embedded in request.url.
The exact headers assertion proves Authorization and deferred Jina controls are absent.
```

Do not mock `httpx`; this test is for the adapter-to-`TextRequester` boundary.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/providers/web/test_jina.py::test_jina_fetch_posts_normalized_target_with_no_cache_and_maps_text -v
```

Expected:

```text
FAIL during collection/import because
agent_search_gateway.providers.web.jina does not exist
```

- [ ] **Step 3: Implement only the successful request and mapping path**

Create `JinaReaderAdapter` with the locked constructor and `fetch()` signature.
Implement the smallest happy path needed by Step 1:

```text
store name
store api_url without a trailing slash
store the TextRequester

text = await request_text(
  "POST",
  api_url,
  stage="fetch",
  headers={"X-No-Cache": "true"},
  json_body={"url": str(url)},
)
return URLFetchCandidate(text, text)
```

At this step, do not yet add constructor validation or empty-body rejection; Tasks 2
and 3 add each behavior from a failing test. Do not add a `secret` parameter or
Authorization header.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/providers/web/test_jina.py::test_jina_fetch_posts_normalized_target_with_no_cache_and_maps_text -v
```

Expected: the exact POST/header/body/candidate contract passes.

- [ ] **Step 5: Refactor with the test green**

Keep request construction in `jina.py`; do not add a generic Jina request builder to
`common.py`.

```bash
uv run ruff check src/agent_search_gateway/providers/web/jina.py tests/providers/web/test_jina.py
uv run mypy src/agent_search_gateway/providers/web/jina.py tests/providers/web/test_jina.py
```

Expected: both pass.

---

### Task 2: Validate the configured Jina Reader endpoint

**Files:**
- Modify: `tests/providers/web/test_jina.py`
- Modify: `src/agent_search_gateway/providers/web/jina.py`
- Reference: `src/agent_search_gateway/providers/web/common.py:42-45`
- Reference: `tests/providers/web/test_scrapingant.py:61-69`

- [ ] **Step 1: Write the failing constructor-validation test**

Add a parameterized test named `test_jina_requires_non_empty_api_url` over:

```python
["", "   ", 1]
```

For each value, assert constructing `JinaReaderAdapter` raises `TypeError`. Use the
normal `# type: ignore[arg-type]` convention for the non-string test value; do not
weaken the production type annotation to satisfy the test.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/providers/web/test_jina.py::test_jina_requires_non_empty_api_url -v
```

Expected: empty/whitespace URLs are accepted by the Task 1 implementation and/or the
non-string case raises the wrong exception type.

- [ ] **Step 3: Reuse the established configuration validator**

Change the constructor to validate the endpoint with:

```text
configured_string(api_url, "api_url")
```

Then remove trailing slashes from the configured endpoint by storing the validated
value with `rstrip("/")`, matching existing web-adapter conventions. Do not add
provider-specific URL scheme/host validation not approved by the error-handling
design.

- [ ] **Step 4: Verify GREEN and retain the happy path**

```bash
uv run pytest tests/providers/web/test_jina.py -v
```

Expected: invalid endpoints raise `TypeError`, and the exact successful request still
passes.

- [ ] **Step 5: Refactor with tests green**

Keep validation in the constructor so `Runtime._build_web_providers()` can continue
mapping constructor `TypeError` to the existing startup `ConfigFailure` boundary.

```bash
uv run ruff check src/agent_search_gateway/providers/web/jina.py tests/providers/web/test_jina.py
uv run mypy src/agent_search_gateway/providers/web/jina.py tests/providers/web/test_jina.py
```

Expected: both pass.

---

### Task 3: Reject empty Jina Reader responses with the existing provider failure

**Files:**
- Modify: `tests/providers/web/test_jina.py`
- Modify: `src/agent_search_gateway/providers/web/jina.py`
- Reference: `src/agent_search_gateway/providers/web/common.py:48-52`
- Reference: `src/agent_search_gateway/providers/web/zenrows.py:25-37`

- [ ] **Step 1: Write the failing empty-body test**

Add a parameterized async test named `test_jina_fetch_rejects_empty_page_body` over:

```python
["", "   ", "\n\t"]
```

For each response:

```text
construct JinaReaderAdapter with RecordingTextExecutor([response])
call fetch(normalize_url("https://example.com/page"))
assert ExecutionFailure is raised
assert its code is ErrorCode.ALL_PROVIDERS_FAILED
assert the failure text contains "jina/fetch: page body is empty"
assert no URLFetchCandidate is returned
```

Do not add a scheduler fallback assertion here; that belongs to existing generic
scheduler tests.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/providers/web/test_jina.py::test_jina_fetch_rejects_empty_page_body -v
```

Expected: the Task 1 adapter currently returns an empty/whitespace candidate.

- [ ] **Step 3: Add only the provider-local empty-body guard**

After `request_text()` returns:

```text
if not text.strip():
  raise failure(name, "fetch", "page body is empty")
return URLFetchCandidate(text, text)
```

Use `.strip()` only to decide emptiness. Preserve the original non-empty response text
unchanged in both candidate fields. Do not parse Jina title/source wrappers or add a
list of error-like response strings.

- [ ] **Step 4: Verify GREEN and nearby text-fetch regressions**

```bash
uv run pytest tests/providers/web/test_jina.py -v
uv run pytest tests/providers/web/test_zenrows.py tests/providers/web/test_scrape_do.py -v
```

Expected: all adapter tests pass.

- [ ] **Step 5: Refactor with tests green**

Confirm the adapter remains one thin transport mapping with no retry loop, exception
translation, logging, store mutation, or cancellation handling.

```bash
uv run ruff check src/agent_search_gateway/providers/web/jina.py tests/providers/web/test_jina.py
uv run mypy src/agent_search_gateway/providers/web/jina.py tests/providers/web/test_jina.py
```

Expected: both pass.

---

### Task 4: Add generic credential-requirement metadata without changing existing defaults

**Files:**
- Modify: `tests/providers/test_registry.py:20-67`
- Modify: `src/agent_search_gateway/providers/registry.py:9-19`

- [ ] **Step 1: Write the failing registration-default test**

Add `test_web_provider_registration_requires_api_key_by_default`.

Construct `WebProviderRegistration` with the existing four positional arguments:

```python
WebProviderRegistration(
    "custom",
    ProviderCapabilities(search=True, fetch=False),
    _factory,
    frozenset({"api_url"}),
)
```

Assert:

```text
registration.requires_api_key is True
```

This simultaneously locks the security default and backward compatibility for
existing positional construction.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/providers/test_registry.py::test_web_provider_registration_requires_api_key_by_default -v
```

Expected: `WebProviderRegistration` has no `requires_api_key` attribute.

- [ ] **Step 3: Add the defaulted field at the end of the dataclass**

Implement exactly:

```python
requires_api_key: bool = True
```

Place it after `allowed_config_keys` so every existing four-argument registration and
test remains valid. Do not introduce an enum or optional-auth state; current built-ins
need only `required` and `none`.

- [ ] **Step 4: Verify GREEN and registry regressions**

```bash
uv run pytest tests/providers/test_registry.py -v
```

Expected: the new default assertion and all existing registry-order/capability tests
pass.

- [ ] **Step 5: Refactor with tests green**

Do not change `ProviderRegistry.register`, registration ordering, or `for_stage()`.

```bash
uv run ruff check src/agent_search_gateway/providers/registry.py tests/providers/test_registry.py
uv run mypy src/agent_search_gateway/providers/registry.py tests/providers/test_registry.py
```

Expected: both pass.

---

### Task 5: Register Jina as a fetch-only, credentialless built-in

**Files:**
- Modify: `src/agent_search_gateway/providers/defaults.py:3-134`
- Modify: `tests/providers/test_registry.py:1-110`
- Modify: `tests/unit/test_config_web_providers.py:166-191`
- Modify: `tests/runtime/test_runtime_assembly.py:117-142`
- Reference: `src/agent_search_gateway/providers/web/jina.py`

- [ ] **Step 1: Write failing default-registry and capability assertions**

In `tests/providers/test_registry.py`:

```text
import JinaReaderAdapter
expand the existing tail-registration expectation to include Jina
add a focused Jina registration assertion
```

The focused assertion must prove:

```python
jina.name == "jina"
jina.capabilities == ProviderCapabilities(search=False, fetch=True)
jina.factory is JinaReaderAdapter
jina.allowed_config_keys == frozenset({"api_url"})
jina.requires_api_key is False
```

Also assert a representative existing registration, such as `tavily`, still has
`requires_api_key is True`. Keep the four-field exact-contract table focused on the
existing shape instead of rewriting every row solely for the new field.

In the existing unsupported-capability parameterization in
`tests/unit/test_config_web_providers.py`:

```text
add ("jina", "enable_search")
assert the failure message identifies "does not support search",
not merely that some CONFIG_ERROR occurred
```

This ensures an absent/unknown Jina registration cannot accidentally satisfy the
capability test.

In the exact default-registry order assertion in
`tests/runtime/test_runtime_assembly.py`, append:

```python
("jina", False, True)
```

Jina should be appended after `serpapi`; this preserves all existing relative order.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
uv run pytest \
  tests/providers/test_registry.py \
  tests/unit/test_config_web_providers.py::test_new_provider_capabilities_are_enforced_by_generic_config_resolution \
  tests/runtime/test_runtime_assembly.py::test_runtime_assembly_builds_enabled_adapters_with_shared_quotas_and_closes_clients \
  -v
```

Expected: Jina is missing from the default registry/order, and the Jina capability
case reports an unknown enabled provider rather than the locked fetch-only capability
failure.

- [ ] **Step 3: Add the Jina default registration**

Import `JinaReaderAdapter` in `providers/defaults.py` and append:

```python
WebProviderRegistration(
    "jina",
    ProviderCapabilities(search=False, fetch=True),
    JinaReaderAdapter,
    frozenset({"api_url"}),
    requires_api_key=False,
)
```

Do not add Jina to a scheduler list or create automatic free-first sorting. Runtime
provider priority continues to come from the configured provider order.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest \
  tests/providers/test_registry.py \
  tests/unit/test_config_web_providers.py::test_new_provider_capabilities_are_enforced_by_generic_config_resolution \
  tests/runtime/test_runtime_assembly.py::test_runtime_assembly_builds_enabled_adapters_with_shared_quotas_and_closes_clients \
  -v
```

Expected: exact Jina metadata, unsupported search behavior, existing credential
defaults, and default registry order all pass. The main runtime test still does not
enable Jina and therefore creates no extra web executor.

- [ ] **Step 5: Refactor with tests green**

Confirm `jina` appears only as normal registration metadata. There must be no
`if name == "jina"` branch in config, runtime, scheduler, or orchestrator code.

```bash
uv run ruff check \
  src/agent_search_gateway/providers/defaults.py \
  tests/providers/test_registry.py \
  tests/unit/test_config_web_providers.py \
  tests/runtime/test_runtime_assembly.py
uv run mypy \
  src/agent_search_gateway/providers/defaults.py \
  tests/providers/test_registry.py \
  tests/unit/test_config_web_providers.py \
  tests/runtime/test_runtime_assembly.py
```

Expected: both pass.

---

### Task 6: Resolve an enabled credentialless provider without an API key

**Files:**
- Modify: `tests/unit/test_config_web_providers.py:37-147`
- Modify: `src/agent_search_gateway/config.py:81-134`
- Reference: `src/agent_search_gateway/config.py:31-39`
- Reference: `src/agent_search_gateway/providers/registry.py:13-19`

- [ ] **Step 1: Write the failing credentialless-resolution test**

Add `test_jina_resolves_without_credential` using `build_default_registry()` and an
empty environment mapping.

Configuration:

```python
{
    "web_providers": {
        "default_max_concurrency": 3,
        "jina": {
            "enable_search": False,
            "enable_fetch": True,
            "max_concurrency": 5,
            "api_url": "https://reader.example.test",
        },
    }
}
```

Assert the single resolved provider has:

```text
name == "jina"
enable_search is False
enable_fetch is True
max_concurrency == 5
api_key_env is None
secret is None
options == {"api_url": "https://reader.example.test"}
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/unit/test_config_web_providers.py::test_jina_resolves_without_credential -v
```

Expected: current generic resolution raises
`web provider jina requires api_key_env`.

- [ ] **Step 3: Generalize credential resolution through registration metadata**

After the existing enabled-provider and capability checks, branch only on
`registration.requires_api_key`:

```text
if requires_api_key:
  require api_key_env to be a non-empty string
  require the named environment value
  construct SecretValue
else:
  api_key_env = None
  secret = None
```

Return those typed local values through the unchanged
`ResolvedWebProviderConfig` fields.

For this task's minimal GREEN implementation, do not yet reject a supplied
`api_key_env` in the credentialless branch; Task 7 adds that rule from a failing test.
Keep `api_key_env` in `_WEB_SHARED_KEYS` so it never leaks into provider `options`.
Keep the disabled-provider early return unchanged: disabled providers require no
credential or constructor validation, while top-level option whitelisting still
applies.

- [ ] **Step 4: Verify GREEN and credential-required regressions**

```bash
uv run pytest \
  tests/unit/test_config_web_providers.py::test_jina_resolves_without_credential \
  tests/unit/test_config_web_providers.py::test_resolve_web_provider_config_or_fail_startup \
  tests/unit/test_config_web_providers.py::test_resolve_web_provider_config_rejects_invalid_enabled_provider \
  tests/unit/test_config_web_providers.py::test_disabled_parallel_requires_no_credential_and_preserves_allowed_options \
  -v
```

Expected: Jina resolves with `None` credential fields, enabled credential-required
providers still require a real environment value, and disabled behavior is unchanged.

- [ ] **Step 5: Refactor with tests green**

Use explicit `str | None` and `SecretValue | None` locals so mypy can verify both
branches. Do not make `ResolvedWebProviderConfig.secret` untyped or hide the branch
inside a Jina-specific helper.

```bash
uv run ruff check src/agent_search_gateway/config.py tests/unit/test_config_web_providers.py
uv run mypy src/agent_search_gateway/config.py tests/unit/test_config_web_providers.py
```

Expected: both pass.

---

### Task 7: Reject credentials supplied to a credentialless provider

**Files:**
- Modify: `tests/unit/test_config_web_providers.py`
- Modify: `src/agent_search_gateway/config.py:112-134`

- [ ] **Step 1: Write the failing no-credential contract test**

Add `test_jina_rejects_api_key_env`.

Enable Jina fetch and include:

```python
"api_key_env": "[REDACTED_SECRET]"
```

Use an empty environment mapping and assert:

```text
ConfigFailure is raised
code is ErrorCode.CONFIG_ERROR
message contains "web provider jina does not accept api_key_env"
```

Using an empty environment is important: the failure must be about the forbidden
configuration field, not a later attempt to resolve the named environment variable.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/unit/test_config_web_providers.py::test_jina_rejects_api_key_env -v
```

Expected: the Task 6 implementation ignores the field and resolves Jina successfully.

- [ ] **Step 3: Require omission, not merely an empty value**

In the credentialless branch:

```text
if "api_key_env" in table:
  raise ConfigFailure(CONFIG_ERROR, "web provider <name> does not accept api_key_env")
api_key_env = None
secret = None
```

Check key presence rather than truthiness. Values such as `""`, whitespace, `None`,
or a valid environment-variable name are all invalid because this feature supports
no Jina credential mode.

Do not read the named environment variable, create a `SecretValue`, accept and ignore
the field, or add `if name == "jina"`.

- [ ] **Step 4: Verify GREEN and the complete web-config suite**

```bash
uv run pytest tests/unit/test_config_web_providers.py -v
```

Expected: Jina's no-credential success and rejection cases pass, unsupported search
still fails at capability validation, unknown options still use `_validate_options`,
and all existing credential-required providers retain startup validation.

- [ ] **Step 5: Refactor with tests green**

Confirm failure messages contain only provider/config field names, never environment
values or secrets.

```bash
uv run ruff check src/agent_search_gateway/config.py tests/unit/test_config_web_providers.py
uv run mypy src/agent_search_gateway/config.py tests/unit/test_config_web_providers.py
```

Expected: both pass.

---

### Task 8: Assemble credentialless Jina through the generic runtime factory

**Files:**
- Modify: `tests/runtime/test_runtime_assembly.py:1-273`
- Modify: `src/agent_search_gateway/runtime.py:44-46,205-257,372-381`
- Reference: `src/agent_search_gateway/concurrency.py`
- Reference: `src/agent_search_gateway/providers/http.py:34-105`

- [ ] **Step 1: Write the failing focused Jina runtime test**

Import `JinaReaderAdapter` and add
`test_runtime_assembles_credentialless_jina_fetch_provider`.

Reuse `_config()`, but replace only its `web_providers` table with:

```python
{
    "default_max_concurrency": 3,
    "jina": {
        "enable_search": False,
        "enable_fetch": True,
        "api_url": "https://reader.example.test/",
        "max_concurrency": 5,
    },
}
```

Keep the existing LLM configuration and `_environment()` so `Runtime.build()` can
construct its normal non-web dependencies. Reuse `_CountingAsyncClient` and the
existing `client_factory` pattern.

Assert:

```text
resolved Jina config has secret is None
Runtime.build succeeds
runtime.web_search_providers == ()
runtime.web_fetch_providers contains exactly one JinaReaderAdapter
provider.name == "jina"
provider._api_url == "https://reader.example.test"
runtime.quotas.get_web("jina").limit == 5
runtime._web_http_executors contains exactly one normal HttpJsonExecutor
that executor owns clients[0]
```

After `await runtime.aclose()` assert:

```text
three clients were created: one Jina web client and two existing LLM clients
every client was closed exactly once
```

The production Jina constructor intentionally has no `secret` parameter. Therefore,
a runtime that merely removes the `secret is None` rejection but still passes
`secret=None` must fail this test through the existing constructor-error boundary.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/runtime/test_runtime_assembly.py::test_runtime_assembles_credentialless_jina_fetch_provider -v
```

Expected: current `_build_web_providers()` raises
`Invalid enabled web provider: jina` because `secret is None`.

- [ ] **Step 3: Make runtime credential injection metadata-driven**

Refactor `_build_web_providers()` generically:

```text
registration = registry.get(name)
if registration is None:
  raise existing Invalid enabled web provider ConfigFailure

kwargs = {
  "name": provider_config.name,
  "http_executor": executor,
}

if registration.requires_api_key:
  if provider_config.secret is None:
    raise existing Invalid enabled web provider ConfigFailure
  kwargs["secret"] = provider_config.secret

kwargs.update(provider_config.options)
adapter = registration.factory(**kwargs)
```

Keep these existing behaviors unchanged:

```text
one web quota for each enabled provider
one HttpJsonExecutor for each enabled provider
reserved option rejection includes "secret"
constructor TypeError maps to ConfigFailure(CONFIG_ERROR)
one adapter instance may serve both stages when capabilities/config enable both
executor lifecycle remains owned by Runtime.aclose()
```

Do not pass `secret` for a credentialless registration. Do not special-case Jina's
name. Do not change the factory type alias or make every adapter accept an optional
secret.

- [ ] **Step 4: Verify GREEN and existing runtime invariants**

```bash
uv run pytest \
  tests/runtime/test_runtime_assembly.py::test_runtime_assembles_credentialless_jina_fetch_provider \
  tests/runtime/test_runtime_assembly.py::test_runtime_assembly_builds_enabled_adapters_with_shared_quotas_and_closes_clients \
  tests/runtime/test_runtime_assembly.py::test_runtime_maps_invalid_brightdata_zone_to_config_failure \
  tests/runtime/test_runtime_assembly.py::test_runtime_rejects_reserved_web_adapter_kwargs \
  -v
```

Expected: Jina constructs without a secret, existing providers still receive their
secret, constructor failures retain the existing startup mapping, reserved keys remain
blocked, quotas are unchanged, and all clients close once.

- [ ] **Step 5: Refactor with tests green**

Keep type narrowing explicit inside the `requires_api_key` branch so the factory
kwargs contain a real `SecretValue` for required providers and no `secret` key for
credentialless providers.

```bash
uv run ruff check src/agent_search_gateway/runtime.py tests/runtime/test_runtime_assembly.py
uv run mypy src/agent_search_gateway/runtime.py tests/runtime/test_runtime_assembly.py
```

Expected: both pass.

---

### Task 9: Document Jina's credentialless fetch and Reader-side refresh boundary

**Files:**
- Modify: `tests/docs/test_documented_config.py:53-193`
- Modify: `config.example.toml:108-120`
- Modify: `README.md:15,50-74`
- Reference: `docs/designs/architectures/20260830-jina-reader-fetch.md:25-32,355-377`
- Reference: `docs/designs/error-handlings/20260830-jina-reader-fetch.md:253-276`

- [ ] **Step 1: Write/refactor the failing documentation contract test**

Update `test_example_config_and_readme_document_all_new_provider_contracts` before
changing the documentation.

Refactor its provider sets into explicit contracts:

```text
credential-required provider names:
  existing listed providers only

credentialless provider names:
  {"jina"}
```

For each credential-required provider, assert:

```text
api_key_env is a string
api_key_env.strip() is non-empty
registration.requires_api_key is True
```

This replaces the unrelated brittle requirement that the environment-variable name
must equal one fixed redaction placeholder; it does not make credentials optional.

Add Jina expectations:

```text
Jina is present in config.example.toml
resolved enable_search is False
resolved enable_fetch is True
resolved api_key_env is None
resolved secret is None
resolved options == {"api_url": "https://r.jina.ai"}
raw Jina table has no api_key_env key
registry Jina requires_api_key is False
README contains | Jina Reader | no | yes |
README states that actual Jina calls send `X-No-Cache: true`
README states that this does not bypass already prepared gateway content
```

Keep all existing provider capability rows and command-documentation assertions.

- [ ] **Step 2: Run the documentation test and verify RED for missing Jina docs**

```bash
uv run pytest tests/docs/test_documented_config.py::test_example_config_and_readme_document_all_new_provider_contracts -v
```

Expected after the semantic credential assertion refactor:

```text
FAIL because Jina is absent from config.example.toml and/or README
```

The pre-existing Decodo literal-placeholder failure should no longer mask the Jina
RED state.

- [ ] **Step 3: Add the example configuration and README contract**

Append Jina after SerpApi and before academic providers in `config.example.toml`:

```toml
[web_providers.jina]
enable_search = false
enable_fetch = true
api_url = "https://r.jina.ai"
```

Do not add `api_key_env`, a dummy credential, optional Jina controls, or a refresh
flag.

Update README:

```text
qualify the installation instruction so only credential-required providers need
api_key_env environment values

add | Jina Reader | no | yes | to the capability table

document that Jina Reader is fetch-only and credentialless in this integration

document that each actual Jina request sends X-No-Cache: true to refresh the
Reader-side result

state explicitly that already prepared in-memory gateway content still short-circuits
provider fetch, so this feature is not a gateway force-refresh API

optionally note that configured provider order remains priority; users can place Jina
earlier when they prefer the free provider first
```

Avoid claiming an authenticated mode, guaranteed rate, or gateway-level refresh.

- [ ] **Step 4: Verify GREEN and all documented configuration**

```bash
uv run pytest tests/docs/test_documented_config.py -v
```

Expected: example TOML resolves with stub secrets for credential-required providers,
Jina resolves with no secret, capability rows match the registry, the README exposes
the refresh boundary, and the prior Decodo assertion failure is gone.

- [ ] **Step 5: Run the complete focused feature set**

```bash
uv run pytest \
  tests/providers/web/test_jina.py \
  tests/providers/test_registry.py \
  tests/unit/test_config_web_providers.py \
  tests/runtime/test_runtime_assembly.py \
  tests/docs/test_documented_config.py \
  -v
uv run ruff check \
  src/agent_search_gateway/providers/web/jina.py \
  src/agent_search_gateway/providers/registry.py \
  src/agent_search_gateway/providers/defaults.py \
  src/agent_search_gateway/config.py \
  src/agent_search_gateway/runtime.py \
  tests/providers/web/test_jina.py \
  tests/providers/test_registry.py \
  tests/unit/test_config_web_providers.py \
  tests/runtime/test_runtime_assembly.py \
  tests/docs/test_documented_config.py
uv run mypy src tests
```

Expected: all focused tests and static checks pass.

---

### Task 10: Run the complete regression gate and audit the architecture boundary

**Files:**
- Verify: all files listed in the intended footprint
- Verify unchanged: `src/agent_search_gateway/providers/http.py`
- Verify unchanged: `src/agent_search_gateway/scheduler/fetch.py`
- Verify unchanged: `src/agent_search_gateway/orchestrators/fetch.py`
- Verify unchanged: `src/agent_search_gateway/url_store.py`
- Verify unchanged: public models/protocol/CLI/daemon files

- [ ] **Step 1: Re-run the deterministic feature suite**

```bash
uv run pytest \
  tests/providers/web/test_jina.py \
  tests/providers/test_registry.py \
  tests/unit/test_config_web_providers.py \
  tests/runtime/test_runtime_assembly.py \
  tests/docs/test_documented_config.py \
  -v
```

Expected: all Jina adapter, registry, config, runtime, and documentation contracts pass
without credentials or network access.

- [ ] **Step 2: Prove unchanged transport, fallback, and gateway-cache behavior**

```bash
uv run pytest \
  tests/providers/test_http_executor.py \
  tests/scheduler/test_fetch_outcomes.py \
  tests/scheduler/test_fetch_capacity.py \
  tests/orchestrators/test_url_fetch_admission.py::test_url_fetch_enforces_admission_and_uses_cached_fields \
  -v
```

Expected:

```text
HTTP text/JSON request, timeout, status, retry, and logging tests remain green
execution and semantic failures still fall back through FetchScheduler
cancellation semantics remain unchanged
already prepared URLStore content does not invoke a fetch provider
```

Do not add a live Jina call to prove cache refresh. The exact adapter header/body test
is the stable offline contract.

- [ ] **Step 3: Run the repository gate**

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -q
```

Expected:

```text
locked dependency sync succeeds
ruff passes
mypy passes
pytest reports zero failures
existing opt-in integration tests remain skipped by default
```

The pre-feature baseline had `1 failed, 521 passed, 4 skipped`. The final passing count
will increase because deterministic Jina tests are added. The existing four skips
should remain; no Jina live-test skip is added. Any remaining failure in
`test_documented_config.py` means Task 9 has not correctly separated credential-
required and credentialless documentation contracts.

- [ ] **Step 4: Review the final file and symbol footprint**

```bash
git diff --name-only
if rg -n "\bjina\b|force_refresh" \
  src/agent_search_gateway/orchestrators \
  src/agent_search_gateway/scheduler \
  src/agent_search_gateway/url_store.py \
  src/agent_search_gateway/protocol.py \
  src/agent_search_gateway/models.py; then
  echo "unexpected Jina or force_refresh branch in core fetch layers" >&2
  exit 1
fi
```

Review the implementation diff relative to this committed plan; the plan file itself
is therefore not part of the expected implementation footprint.

Expected `git diff --name-only` feature footprint:

```text
README.md
config.example.toml
src/agent_search_gateway/config.py
src/agent_search_gateway/providers/defaults.py
src/agent_search_gateway/providers/registry.py
src/agent_search_gateway/providers/web/jina.py
src/agent_search_gateway/runtime.py
tests/docs/test_documented_config.py
tests/providers/test_registry.py
tests/providers/web/test_jina.py
tests/runtime/test_runtime_assembly.py
tests/unit/test_config_web_providers.py
```

The `rg` scope check should report no Jina or `force_refresh` branch in core
orchestration, scheduling, storage, protocol, or models.

- [ ] **Step 5: Perform the final behavior review with tests green**

Confirm:

```text
Jina implements only URLFetchProvider.fetch
Jina sends POST + X-No-Cache + JSON target URL through request_text
Jina sends no Authorization header and owns no SecretValue
empty text uses the existing provider ExecutionFailure
HTTP 408/429/5xx and transport retry remain HttpJsonExecutor behavior
non-empty unusable text remains cheap-check/judge behavior
provider fallback and quota selection remain FetchScheduler behavior
URL admission, prepared-content caching, store mutation, safety, and summaries remain
FetchOrchestrator/URLStore behavior
configuration order remains provider priority
no target URL or page body is added to transport logs or exceptions
no new public request, response, error, cache, or persistence contract exists
```

Re-run the complete gate after any cleanup.

---

## Self-Review

### Spec coverage

| Design requirement | Plan task(s) |
|---|---|
| Built-in Jina integration implements existing fetch contract only | 1, 3, 5 |
| Capabilities are exactly `search=False, fetch=True` | 5, 9 |
| Unauthenticated/free Reader mode; no API key accepted or resolved | 5, 6, 7, 8, 9 |
| POST configured Reader endpoint | 1 |
| Normalized target URL is carried exactly in JSON body | 1 |
| `X-No-Cache: true` is sent on every actual Jina call | 1, 9 |
| No Authorization or optional Jina controls | 1, 5, 8 |
| Text response maps unchanged to both candidate fields | 1, 3 |
| Empty/whitespace text raises existing provider failure | 3 |
| Invalid `api_url` uses existing `configured_string()` TypeError | 2 |
| Generic registration declares whether a credential is required | 4 |
| Existing registrations remain credential-required by default | 4, 5, 6, 7, 8 |
| Enabled Jina resolves with `api_key_env=None`, `secret=None` | 6 |
| Enabled Jina rejects any supplied `api_key_env` before env lookup | 7 |
| Runtime constructs Jina without a `secret` kwarg | 8 |
| Runtime still rejects missing secrets for credential-required providers | 8 |
| Normal web quota/executor lifecycle applies to Jina | 8 |
| Jina is a normal configured-order fallback provider | Boundaries, 5, 8, 10 |
| Shared HTTP retry/status/timeout/logging behavior remains authoritative | Boundaries, 10 |
| HTTP 429 receives existing retry/fallback behavior; no RPM limiter | Boundaries, 10 |
| Non-empty semantic failures remain scheduler cheap-check/judge behavior | Boundaries, 10 |
| `asyncio.CancelledError` propagation remains unchanged | Boundaries, 10 |
| Target URL stays out of the outer HTTP endpoint path/log field | 1, 10 |
| Page text, request body, and secrets are not added to logs/errors | 1, 3, 7, 8, 10 |
| Reader-side refresh does not bypass prepared URLStore content | Boundaries, 9, 10 |
| No `force_refresh`, store, singleflight, protocol, or public model change | Boundaries, 10 |
| No new exception family or `ErrorCode` | 3, 7, 10 |
| Example config omits a dummy Jina key | 9 |
| README documents fetch capability and precise refresh boundary | 9 |
| Default tests remain deterministic, offline, and credential-free | 1-10 |
| No Jina Search, API-key mode, provider controls, live test, or rate limiter | Boundaries, 9, 10 |

### File-structure review

Expected feature footprint:

```text
src/agent_search_gateway/providers/web/jina.py
src/agent_search_gateway/providers/registry.py
src/agent_search_gateway/providers/defaults.py
src/agent_search_gateway/config.py
src/agent_search_gateway/runtime.py

tests/providers/web/test_jina.py
tests/providers/test_registry.py
tests/unit/test_config_web_providers.py
tests/runtime/test_runtime_assembly.py
tests/docs/test_documented_config.py

config.example.toml
README.md
```

Reuse:

```text
tests/support/http.py::RecordingTextExecutor
src/agent_search_gateway/providers/web/common.py::TextRequester
src/agent_search_gateway/providers/web/common.py::configured_string
src/agent_search_gateway/providers/web/common.py::failure
src/agent_search_gateway/providers/http.py::HttpJsonExecutor.request_text
```

No fixture directory, HTTP mock framework, scheduler test, orchestrator test, daemon
test, protocol test, acceptance test, or live integration test is expected. If the
implementation requires changing core fetch orchestration, stop and re-check the
approved architecture instead of silently widening scope.

### Ordering and dependency review

- The exact successful Jina request exists before validation and failure edge cases.
- Constructor validation is locked before the adapter is exposed through the registry.
- Empty-body behavior is locked before scheduler/runtime integration can invoke it.
- The registration default is added before Jina opts out of credential requirements.
- Jina metadata/capabilities exist before config resolution branches on that metadata.
- Credentialless success is implemented before the stricter no-credential rejection.
- Config produces `secret=None` before runtime is generalized to accept it.
- Runtime wiring is complete before the real example configuration enables Jina.
- Documentation tests distinguish required and absent credentials before README/TOML
  advertise the new provider.
- Full-suite and core-cache/fallback regressions run only after deterministic feature
  behavior is complete.

### Type and contract consistency

- `JinaReaderAdapter.fetch(url: NormalizedURL) -> URLFetchCandidate` matches the
  existing runtime-checkable provider protocol exactly.
- `TextRequester.request_text()` receives method, endpoint, stage, headers, and
  `json_body`; no transport API extension is introduced.
- The header key/value remain exactly `X-No-Cache` / `true` from test through adapter.
- The JSON field remains exactly `url` and contains `str(NormalizedURL)`.
- Successful response text remains a `str` and populates both candidate fields without
  trimming or parsing.
- `WebProviderRegistration.requires_api_key` is a trailing `bool = True`, preserving
  positional construction and secure defaults.
- `ResolvedWebProviderConfig.api_key_env` stays `str | None`; `secret` stays
  `SecretValue | None`.
- `api_key_env` remains a shared config key and is never passed in provider options.
- Credential-required runtime factories receive a non-optional `SecretValue` only
  after explicit narrowing; the Jina factory receives no `secret` keyword at all.
- `JinaReaderAdapter` stores no secret and exposes no search method.
- No new request model, response model, scheduler outcome, quota type, URL record
  field, protocol frame, error code, or result-file field is introduced.

### Final implementation gate

Implementation is ready only after:

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -q
```

The gate must be network-free, require no Jina credential, report zero failures, and
retain the existing skipped opt-in integration tests. The implementation must not log
or persist Jina page text, request JSON bodies, or credentials, and it must not imply
that `X-No-Cache` refreshes already prepared gateway content.
