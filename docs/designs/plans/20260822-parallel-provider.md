# Parallel Search and Extract Provider Implementation Plan

**Goal:** Add a built-in `parallel` web provider that implements the existing keyword-search and URL-fetch contracts through Parallel V1 Search and Extract, with provider-local configuration validation and no changes to core orchestration, scheduling, protocol, quota, storage, or result contracts.

**Architecture:** `docs/designs/architectures/20260822-parallel-provider.md`

**Error handling:** `docs/designs/error-handlings/20260822-parallel-provider.md`

**Testing:** `docs/designs/testings/20260822-parallel-provider.md`

---

## Baseline and implementation boundaries

Current worktree baseline before implementation:

```text
uv run pytest -q
180 passed, 2 skipped
```

Keep the implementation adapter-local. Intended production-code footprint:

```text
Create:
  src/agent_search_gateway/providers/web/parallel.py

Modify:
  src/agent_search_gateway/providers/defaults.py

Documentation/config example:
  config.example.toml
  README.md
```

The following are sources of truth to consume, not production files to modify unless a failing test proves the approved design cannot be implemented otherwise:

```text
src/agent_search_gateway/config.py
src/agent_search_gateway/runtime.py
src/agent_search_gateway/providers/web/common.py
src/agent_search_gateway/orchestrators/search.py
src/agent_search_gateway/orchestrators/fetch.py
src/agent_search_gateway/scheduler/fetch.py
src/agent_search_gateway/concurrency.py
src/agent_search_gateway/errors.py
src/agent_search_gateway/protocol.py
src/agent_search_gateway/url_store.py
src/agent_search_gateway/result_writer.py
```

Do not introduce Parallel-specific branches in core orchestration/scheduling/quota/storage/protocol code, new `ErrorCode` values, provider-specific retry logic, `objective`, query rewriting, `max_results`, sessions, generic provider-option passthrough, a configurable `full_content` switch, a gateway-wide freshness abstraction, or separate Search/Extract quotas.

`HttpJsonExecutor` remains the sole owner of HTTP timeout/transport/status retry and invalid-JSON behavior. Adapter code must not catch and downgrade those failures. `asyncio.CancelledError` must propagate unchanged through both Search and Extract paths.

Authentication stays inside the existing `SecretValue` boundary. Tests may assert that the `x-api-key` header is populated from the test secret, but must not print or persist the secret value. Do not add Parallel-specific raw request/response logging.

### Locked adapter interface

```python
class ParallelAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        http_executor: JsonRequester,
        mode: str | None = None,
        search_fetch_policy: Mapping[str, object] | None = None,
        extract_fetch_policy: Mapping[str, object] | None = None,
    ) -> None: ...

    async def search(self, query: str) -> list[KeywordSearchHit]: ...

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate: ...
```

Policy validation stays in `parallel.py` and accepts only:

```text
max_age_seconds:
  int, but not bool
  >= 600

timeout_seconds:
  int | float, but not bool
  no additional local range restriction

disable_cache_fallback:
  bool
```

Unknown nested keys and non-mapping policy containers raise `TypeError`. Copy validated policy mappings into adapter-owned dictionaries so caller mutation cannot couple Search and Extract behavior. `api_url` remains a required constructor argument like the existing built-ins; do not invent Parallel-only URL syntax validation absent from the approved error-handling design.

---

### Task 1: Implement the minimal Parallel Search request and result mapping

**Files:**
- Create: `src/agent_search_gateway/providers/web/parallel.py`
- Create: `tests/providers/web/test_parallel.py`
- Create: `tests/fixtures/providers/parallel/search.json`
- Reference: `src/agent_search_gateway/providers/web/tavily.py:19-71`
- Reference: `src/agent_search_gateway/providers/web/common.py:22-69`
- Reference: `src/agent_search_gateway/providers/contracts.py:13-25`

- [ ] **Step 1: Write the failing basic Search contract test**

Create `tests/fixtures/providers/parallel/search.json` with one realistic result containing `url`, `title`, two `excerpts`, and ignored metadata such as `search_id`, `publish_date`, and `session_id`.

Add `test_parallel_search_builds_minimal_v1_request_and_maps_excerpts` using `RecordingJsonExecutor`.

Scenario:

```text
Construct ParallelAdapter with api_url=https://parallel.example.test/.
Call await adapter.search("hello world").
Assert POST https://parallel.example.test/v1/search, stage="search".
Assert x-api-key header is derived from the test SecretValue without logging it.
Assert JSON body is exactly {"search_queries": ["hello world"]}.
Assert no objective, mode, max_results, session_id, or advanced_settings is sent.
Assert the fixture maps to one KeywordSearchHit whose snippet is
"First relevant excerpt.\n\nSecond relevant excerpt.".
Assert raw_content == "" and content == "".
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/providers/web/test_parallel.py::test_parallel_search_builds_minimal_v1_request_and_maps_excerpts -v
```

Expected:

```text
FAIL during collection/import because agent_search_gateway.providers.web.parallel does not exist
```

- [ ] **Step 3: Implement only the minimal Search path**

Domain-specific pseudocode:

```text
request_body = {"search_queries": [query]}
request POST endpoint(api_url, "/v1/search") through JsonRequester
use stage="search" and x-api-key authentication

root = require_object(payload, name, "search", "response")
results = require_list(root.get("results"), name, "search", "results")

hits = []
for item in results:
  try:
    result = require_object(item, name, "search", "result")
    url = non_empty_string(result.get("url"), name, "search", "result.url")
    title = optional_string(result.get("title"), name, "search", "result.title")
    excerpts = require_list(result.get("excerpts"), name, "search", "result.excerpts")
    excerpt_strings = require every excerpt to be a string
    snippet = "\n\n".join(excerpt_strings)
    hits.append(KeywordSearchHit(url=url, title=title, snippet=snippet))
  except ExecutionFailure:
    continue
return hits
```

Do not map Search excerpts into `raw_content` or `content`.

- [ ] **Step 4: Verify GREEN and nearby provider regressions**

```bash
uv run pytest tests/providers/web/test_parallel.py -v
uv run pytest tests/providers/web/test_tavily.py tests/providers/web/test_exa.py -v
```

Expected: Parallel basic Search passes and existing adapter tests remain green.

- [ ] **Step 5: Refactor with tests green**

Keep request construction and parsing local to `parallel.py`; do not add a generic request builder to `common.py` for one provider.

```bash
uv run ruff check src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
uv run mypy src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
```

Expected: both pass.

---

### Task 2: Add optional Search mode and Search fetch-policy mapping

**Files:**
- Modify: `src/agent_search_gateway/providers/web/parallel.py`
- Modify: `tests/providers/web/test_parallel.py`

- [ ] **Step 1: Write failing request-shape tests**

Add:

```text
test_parallel_search_includes_configured_mode
  parameterized over turbo/fast/basic/advanced

test_parallel_search_omits_mode_when_not_configured

test_parallel_search_maps_only_search_fetch_policy
  construct with different Search and Extract policies
  assert Search gets only search_fetch_policy
```

Expected Search body when mode and Search policy are configured:

```python
{
    "search_queries": ["hello world"],
    "mode": "turbo",
    "advanced_settings": {
        "fetch_policy": {
            "max_age_seconds": 3600,
            "timeout_seconds": 15,
            "disable_cache_fallback": False,
        }
    },
}
```

Add partial-policy cases for each supported field. Treat an explicitly supplied empty mapping as a valid empty policy object and send `advanced_settings.fetch_policy={}`. Only `None` omits Search `advanced_settings`.

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest tests/providers/web/test_parallel.py -k "mode or search_fetch_policy" -v
```

Expected: failures because optional Search settings are not yet retained/applied.

- [ ] **Step 3: Implement minimal Search option composition**

```text
store mode
store Search and Extract policy mappings separately
copy mappings into adapter-owned dicts

body = {"search_queries": [query]}
if mode is not None:
  body["mode"] = mode
if search_fetch_policy is not None:
  body["advanced_settings"] = {"fetch_policy": copied_search_policy}
```

Do not add objective, max_results, provider defaults, or a shared policy. Invalid-value validation belongs to Task 7.

- [ ] **Step 4: Verify GREEN and preserve the minimal no-options request**

```bash
uv run pytest tests/providers/web/test_parallel.py -k "search" -v
```

Expected: all Search mapping tests pass; no-options body remains exactly `{"search_queries": [query]}`.

- [ ] **Step 5: Refactor/check**

```bash
uv run ruff check src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
uv run mypy src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
```

Expected: both pass.

---

### Task 3: Lock Search response tolerance and top-level failure semantics

**Files:**
- Modify: `tests/providers/web/test_parallel.py`
- Modify: `src/agent_search_gateway/providers/web/parallel.py` only if tests expose a gap
- Reference: `tests/providers/web/test_search_result_tolerance.py:79-170`

- [ ] **Step 1: Add malformed-entry and top-level tests**

Parameterize malformed individual results surrounded by valid results:

```text
non-object result
missing URL
non-string URL
empty URL
non-string title
missing excerpts
excerpts not an array
one excerpt element not a string
```

Assert only the malformed entry is skipped and valid entries retain provider order.

Add top-level failures:

```text
response root not an object
results missing
results not a list
```

Assert existing `ExecutionFailure`, not `[]`.

Also cover:

```text
{"results": []} -> []
empty excerpts -> snippet=""
one excerpt -> unchanged
two excerpts -> joined exactly with "\n\n"
empty-string excerpt values remain valid strings and are not filtered
```

- [ ] **Step 2: Run focused tests and verify RED where behavior is missing**

```bash
uv run pytest tests/providers/web/test_parallel.py -k "malformed or empty or excerpts or tolerance" -v
```

Expected: any incorrect tolerance boundary fails; a valid empty Search is not reclassified as provider failure.

- [ ] **Step 3: Keep the exception boundary exact**

```text
top-level require_object/require_list stay outside per-result try/except
per-result object/url/title/excerpts parsing stays inside try/except ExecutionFailure
valid results=[] or all entries skipped returns []
```

Do not catch HTTP/transport/protocol failures or cancellation. Do not normalize Search URLs inside the adapter.

- [ ] **Step 4: Verify GREEN with existing tolerance coverage**

```bash
uv run pytest tests/providers/web/test_parallel.py -v
uv run pytest tests/providers/web/test_search_result_tolerance.py -v
```

Expected: all pass.

- [ ] **Step 5: Refactor/check**

Keep the per-entry `try/except ExecutionFailure` narrow enough that top-level failures cannot be swallowed.

```bash
uv run ruff check src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
uv run mypy src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
```

Expected: both pass.

---

### Task 4: Implement Parallel Extract with mandatory full content

**Files:**
- Modify: `src/agent_search_gateway/providers/web/parallel.py`
- Modify: `tests/providers/web/test_parallel.py`
- Create: `tests/fixtures/providers/parallel/extract.json`
- Reference: `src/agent_search_gateway/providers/web/exa.py:68-84`
- Reference: `src/agent_search_gateway/providers/web/tavily.py:73-90`

- [ ] **Step 1: Write the failing basic Extract contract test**

Create `tests/fixtures/providers/parallel/extract.json` with one matching result containing `url` and non-empty Markdown `full_content`, plus ignored provider metadata.

Add `test_parallel_extract_requests_full_content_and_maps_matching_body`.

Scenario:

```text
Call adapter.fetch(normalize_url("https://example.com/parallel")).
Assert POST https://parallel.example.test/v1/extract, stage="fetch".
Assert x-api-key authentication is supplied through the existing secret boundary.
Assert JSON body is exactly:
{
  "urls": ["https://example.com/parallel"],
  "advanced_settings": {"full_content": True},
}
Assert objective, search_queries, max_chars_total, session_id, and excerpt tuning are absent.
Assert full_content maps to both URLFetchCandidate.raw_content and .content.
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/providers/web/test_parallel.py::test_parallel_extract_requests_full_content_and_maps_matching_body -v
```

Expected:

```text
FAIL because ParallelAdapter.fetch is not implemented
```

- [ ] **Step 3: Implement only the minimal Extract path**

Domain-specific pseudocode:

```text
body = {
  "urls": [str(url)],
  "advanced_settings": {"full_content": True},
}
request POST endpoint(api_url, "/v1/extract") through JsonRequester
use stage="fetch" and x-api-key authentication

root = require_object(payload, name, "fetch", "response")
results = require_list(root.get("results"), name, "fetch", "results")
errors = require_list(root.get("errors"), name, "fetch", "errors")

for item in results:
  result = require_object(item, name, "fetch", "result")
  if normalized_match(result.get("url"), url, name, "fetch"):
    full_content = non_empty_string(result.get("full_content"), ...)
    return URLFetchCandidate(raw_content=full_content, content=full_content)

raise failure(name, "fetch", "matching extraction result was not returned")
```

Task 6 completes matching provider-error handling. Always include `advanced_settings.full_content=True`; there is no constructor/config path that can turn it off.

- [ ] **Step 4: Verify GREEN and nearby fetch-provider regressions**

```bash
uv run pytest tests/providers/web/test_parallel.py -k "extract and not error" -v
uv run pytest tests/providers/web/test_tavily.py tests/providers/web/test_exa.py -v
```

Expected: basic Parallel Extract passes and existing fetch-capable adapters remain green.

- [ ] **Step 5: Refactor without moving semantic admission into the adapter**

The adapter checks provider shape and non-empty `full_content` only. It must not call the gateway cheap check/judge or mutate URL state.

```bash
uv run ruff check src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
uv run mypy src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
```

Expected: both pass.

---

### Task 5: Map Extract fetch policy independently from Search policy

**Files:**
- Modify: `src/agent_search_gateway/providers/web/parallel.py`
- Modify: `tests/providers/web/test_parallel.py`

- [ ] **Step 1: Write failing endpoint-policy independence tests**

Add `test_parallel_extract_maps_only_extract_fetch_policy` using different Search and Extract policies on the same adapter.

Expected Extract body:

```python
{
    "urls": ["https://example.com/parallel"],
    "advanced_settings": {
        "full_content": True,
        "fetch_policy": {
            "max_age_seconds": 600,
            "timeout_seconds": 30,
            "disable_cache_fallback": True,
        },
    },
}
```

Assert no Search-policy value appears in the Extract request. Invoke `search()` on the same adapter and assert Search still receives only `search_fetch_policy`.

Add partial Extract policies and an explicitly empty mapping. Omitted Extract policy keeps exactly `advanced_settings={"full_content": True}`; an explicit empty policy may add only `"fetch_policy": {}` beside `full_content`.

- [ ] **Step 2: Run policy tests and verify RED**

```bash
uv run pytest tests/providers/web/test_parallel.py -k "extract_fetch_policy or policy_independence" -v
```

Expected: failures because `fetch()` does not yet apply the independently stored Extract policy.

- [ ] **Step 3: Add only Extract request composition**

```text
advanced_settings = {"full_content": True}
if extract_fetch_policy is not None:
  advanced_settings["fetch_policy"] = copied_extract_policy
body = {
  "urls": [str(url)],
  "advanced_settings": advanced_settings,
}
```

Keep Search and Extract policy state separate. Do not introduce one shared policy alias.

- [ ] **Step 4: Verify GREEN for both endpoints**

```bash
uv run pytest tests/providers/web/test_parallel.py -k "policy or full_content" -v
```

Expected: Search and Extract receive only their own policies and every Extract request still has `full_content=True`.

- [ ] **Step 5: Refactor/check**

If a small helper is used to copy policy data into a request, keep endpoint ownership explicit so future edits cannot accidentally apply one policy to both endpoints.

```bash
uv run ruff check src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
uv run mypy src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
```

Expected: both pass.

---

### Task 6: Implement Extract URL matching and provider-failure semantics

**Files:**
- Modify: `src/agent_search_gateway/providers/web/parallel.py`
- Modify: `tests/providers/web/test_parallel.py`
- Create: `tests/fixtures/providers/parallel/extract_error.json`
- Reference: `src/agent_search_gateway/providers/web/common.py:51-69`
- Reference: `docs/designs/error-handlings/20260822-parallel-provider.md` (Extract Response Failures)

- [ ] **Step 1: Write failing Extract matching/failure tests**

Create `extract_error.json` with `results=[]` and one matching error entry. Use a harmless provider diagnostic string only to prove that raw provider diagnostics are not copied into gateway exception text.

Cover:

```text
exact result URL -> accepted
normalization-equivalent result URL -> accepted
unrelated result followed by matching result -> matching body returned
matching provider error after no usable matching result -> ExecutionFailure
no matching result or matching error -> ExecutionFailure
malformed result URL -> ExecutionFailure via normalized_match semantics
```

Parameterize matching `full_content` as:

```text
missing
None
non-string
empty string
whitespace-only string
```

Every case must raise existing `ExecutionFailure`; never fall back to `excerpts`.

Add malformed top-level Extract cases:

```text
response root not object
results missing/not list
errors missing/not list
```

Assert provider failure rather than an empty candidate. For the matching-error case, assert the harmless raw provider diagnostic is absent from the exception string.

- [ ] **Step 2: Run focused failure tests and verify RED**

```bash
uv run pytest tests/providers/web/test_parallel.py -k "extract and (error or matching or full_content or malformed)" -v
```

Expected: missing matching/error/full-content classification fails at the adapter boundary.

- [ ] **Step 3: Implement result-first, then error matching**

Domain-specific pseudocode:

```text
root = require_object(...)
results = require_list(root.get("results"), ...)
errors = require_list(root.get("errors"), ...)

for item in results:
  result = require_object(item, ...)
  if normalized_match(result.get("url"), target, ...):
    full_content = non_empty_string(result.get("full_content"), ...)
    return URLFetchCandidate(full_content, full_content)

for item in errors:
  provider_error = require_object(item, ...)
  if normalized_match(provider_error.get("url"), target, ...):
    raise failure(name, "fetch", "provider reported extraction failure")

raise failure(name, "fetch", "matching extraction result was not returned")
```

Do not include the raw provider error object, its diagnostic content, extracted page body, or request payload in new exception messages. Let failures propagate to the existing scheduler fallback path.

- [ ] **Step 4: Verify GREEN with scheduler regressions**

```bash
uv run pytest tests/providers/web/test_parallel.py -v
uv run pytest tests/scheduler/test_fetch_capacity.py tests/scheduler/test_fetch_outcomes.py -v
```

Expected: Parallel failures use existing provider execution-failure semantics and scheduler fallback/semantic-failure tests remain green.

- [ ] **Step 5: Refactor/check the boundary**

Keep provider protocol validity separate from body acceptability. No cheap check, judge call, state mutation, provider retry, or fallback selection belongs in `ParallelAdapter.fetch()`.

```bash
uv run ruff check src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
uv run mypy src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
```

Expected: both pass.

---

### Task 7: Validate Parallel mode and FetchPolicy configuration at construction time

**Files:**
- Modify: `src/agent_search_gateway/providers/web/parallel.py`
- Modify: `tests/providers/web/test_parallel.py`
- Reference: `src/agent_search_gateway/runtime.py:152-176`
- Reference: `docs/designs/error-handlings/20260822-parallel-provider.md` (Configuration Failures)

- [ ] **Step 1: Write failing constructor-validation tests**

Test valid `mode` values:

```text
None
turbo
fast
basic
advanced
```

Reject invalid mode values:

```text
ADVANCED
empty string
integer
boolean
mapping
```

For both `search_fetch_policy` and `extract_fetch_policy`, reject non-mapping containers such as a string, integer, boolean, or list. Reject an unknown nested policy key.

Validate individual fields:

```text
max_age_seconds
  accept: 600, 3600, 86400
  reject: 599, 0, -1, 600.0, True, "600"

timeout_seconds
  accept: 1, 15, 30.5
  reject: True, False, "30", list, mapping

disable_cache_fallback
  accept: True, False
  reject: integers, strings, explicit None
```

Every invalid provider-specific setting raises `TypeError` during construction, before any HTTP request.

Add one mutation-safety test: construct with mutable Search/Extract policy dictionaries, mutate the caller-owned dictionaries, invoke both endpoints, and assert requests still use the values captured at construction.

- [ ] **Step 2: Run constructor tests and verify RED**

```bash
uv run pytest tests/providers/web/test_parallel.py -k "invalid_mode or policy_validation or mutation" -v
```

Expected: failures because invalid settings are accepted or policy objects are retained by reference.

- [ ] **Step 3: Implement one provider-local policy validator**

Suggested private shape:

```python
def _validate_fetch_policy(
    value: Mapping[str, object] | None,
    *,
    label: str,
) -> dict[str, object] | None: ...
```

Pseudocode:

```text
if value is None:
  return None
if not Mapping:
  raise TypeError
if any key outside {max_age_seconds, timeout_seconds, disable_cache_fallback}:
  raise TypeError

if max_age_seconds present:
  require int and not bool and value >= 600
if timeout_seconds present:
  require int|float and not bool
if disable_cache_fallback present:
  require bool

return a new dict copy
```

Constructor mode validation:

```text
if mode is not None:
  require str
  require mode in {turbo, fast, basic, advanced}
```

Do not add undocumented numeric restrictions. Do not add a provider-specific config exception type; `Runtime._build_web_providers()` already converts constructor `TypeError` to the existing startup `ConfigFailure`.

- [ ] **Step 4: Verify GREEN and all request mapping**

```bash
uv run pytest tests/providers/web/test_parallel.py -v
```

Expected: all deterministic Parallel adapter tests pass.

- [ ] **Step 5: Refactor/check validation names and types**

Keep one policy-key whitelist and one validator shared by both constructor arguments. Do not duplicate the validation rules.

```bash
uv run ruff check src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
uv run mypy src/agent_search_gateway/providers/web/parallel.py tests/providers/web/test_parallel.py
```

Expected: both pass.

---

### Task 8: Register Parallel and prove generic config/runtime plumbing needs no core changes

**Files:**
- Modify: `src/agent_search_gateway/providers/defaults.py:5-11,14-62`
- Modify: `tests/unit/test_config_web_providers.py:13-68`
- Modify: `tests/runtime/test_runtime_assembly.py:28-153`
- Inspect only: `src/agent_search_gateway/config.py:59-150`
- Inspect only: `src/agent_search_gateway/runtime.py:130-182`

- [ ] **Step 1: Write failing registry and generic-config assertions**

Extend the built-in registration expectation so Parallel is appended after the existing providers, minimizing unrelated order churn:

```text
("parallel", True, True)
```

Assert its exact allowed config keys:

```python
frozenset({
    "api_url",
    "mode",
    "search_fetch_policy",
    "extract_fetch_policy",
})
```

Extend `tests/unit/test_config_web_providers.py` only enough to prove the existing generic resolver preserves nested provider options. Let the existing generic dual-capability registration whitelist the two policy option names, put nested mappings into raw config, and assert they survive unchanged in `ResolvedWebProviderConfig.options`. Assert shared fields still do not appear in `options`, and preserve the existing unknown top-level option rejection.

Add two real-default-registry disabled-provider cases:

```text
parallel with enable_search=false and enable_fetch=false:
  requires no credential environment variable
  retains syntactically allowed nested policy data without adapter-local value validation

parallel disabled but with an unknown top-level option:
  still raises existing ConfigFailure because registration whitelist validation runs first
```

These tests lock the error-handling document's disabled-provider boundary without adding any special case to `config.py`.

- [ ] **Step 2: Run registry/config tests and verify RED**

```bash
uv run pytest tests/unit/test_config_web_providers.py tests/runtime/test_runtime_assembly.py -v
```

Expected: failures because Parallel is not registered and runtime expectations do not include it.

- [ ] **Step 3: Register Parallel without changing registry/config behavior**

In `providers/defaults.py`:

```text
import ParallelAdapter
append WebProviderRegistration(
  name="parallel",
  capabilities=ProviderCapabilities(search=True, fetch=True),
  factory=ParallelAdapter,
  allowed_config_keys={api_url, mode, search_fetch_policy, extract_fetch_policy},
)
```

Do not modify `ProviderRegistry`, `_validate_options`, `_resolve_one_web_provider`, or `ResolvedWebProviderConfig`.

- [ ] **Step 4: Extend runtime assembly coverage and verify GREEN**

Add Parallel to the runtime test config with both stages enabled, a distinct `max_concurrency`, `api_url`, one valid mode, and different Search/Extract policies. Supply its credential only through the test environment/`SecretValue` path already used by the suite; never place the credential value in log assertions.

Assert:

```text
one ParallelAdapter is constructed
the same object identity appears in web_search_providers and web_fetch_providers
one web quota named parallel is shared across both stages
quota limit equals configured max_concurrency
one HTTP executor/client is added for Parallel and closed exactly once
runtime repr/log output does not expose the Parallel credential
runtime provider counts/names/limits change only by the newly enabled provider
```

Add `test_runtime_maps_invalid_parallel_configuration_to_config_failure`: resolve a syntactically whitelisted Parallel config with an invalid `mode`, call `Runtime.build(...)`, and assert the existing generic `ConfigFailure` message for invalid web-provider configuration.

Run:

```bash
uv run pytest tests/unit/test_config_web_providers.py tests/runtime/test_runtime_assembly.py -v
```

Expected: all pass without production changes to `config.py` or `runtime.py`.

- [ ] **Step 5: Run provider/runtime regressions and static checks**

```bash
uv run pytest tests/providers tests/runtime tests/unit/test_config_web_providers.py -q
uv run ruff check src/agent_search_gateway/providers/defaults.py src/agent_search_gateway/providers/web/parallel.py tests/unit/test_config_web_providers.py tests/runtime/test_runtime_assembly.py
uv run mypy src tests
```

Expected: all pass and no Parallel-specific config/runtime branch exists.

---

### Task 9: Document Parallel configuration and keep the example config executable

**Files:**
- Modify: `config.example.toml:4-45`
- Modify: `README.md:50-62`
- Modify: `tests/docs/test_documented_config.py:31-46`
- Reference: `docs/designs/architectures/20260822-parallel-provider.md` (Parallel Provider Configuration Contract)

- [ ] **Step 1: Write a failing documented-config assertion**

Extend `test_example_config_loads_with_stub_secrets_and_readme_commands_match_cli_help` with configuration-focused assertions after resolving the example:

```text
find provider named parallel
enable_search is True
enable_fetch is True
options contains exactly api_url, mode, search_fetch_policy, extract_fetch_policy
nested policy values retain their TOML scalar types
```

Prefer resolver assertions over brittle README prose snapshots.

- [ ] **Step 2: Run the docs test and verify RED**

```bash
uv run pytest tests/docs/test_documented_config.py -v
```

Expected: failure because the example config does not yet contain a Parallel section.

- [ ] **Step 3: Add safe example configuration and README documentation**

Add a `[web_providers.parallel]` section that demonstrates:

```text
enable_search = true
enable_fetch = true
api_url = https://api.parallel.ai
api_key_env = the environment-variable name chosen for the Parallel credential
optional mode = turbo
search_fetch_policy with max_age_seconds=3600, timeout_seconds=15, disable_cache_fallback=false
extract_fetch_policy with max_age_seconds=600, timeout_seconds=30, disable_cache_fallback=true
```

Commit only the environment-variable name, never a credential value.

In the README provider-capability section:

```text
add Parallel | yes | yes
state mode is optional
state Search and Extract fetch policies are independent
state Extract always requests full content internally
state result count remains Parallel's provider default in this version
```

Do not document unsupported objective, result-count controls, sessions, or arbitrary provider options.

- [ ] **Step 4: Verify GREEN through the real resolver**

```bash
uv run pytest tests/docs/test_documented_config.py tests/unit/test_config_web_providers.py -v
```

Expected: the example config parses/resolves with stub credentials and the resolved Parallel options match the default registration.

- [ ] **Step 5: Refactor/check documentation consistency**

Review README/config example against the four registered Parallel-specific option names and the architecture's non-goals.

```bash
uv run ruff check tests/docs/test_documented_config.py
uv run mypy tests/docs/test_documented_config.py
```

Expected: both pass.

---

### Task 10: Add opt-in live Parallel contract smoke tests and run the final gate

**Files:**
- Create: `tests/integration/test_live_parallel.py`
- Reference: `tests/integration/test_live_tavily_and_openai.py:13-46`
- Reference: `docs/designs/testings/20260822-parallel-provider.md` (Opt-In Live Parallel Contract Test)

The live test is a contract smoke check after deterministic TDD coverage is complete. It must remain skipped in normal runs and must not drive production design.

- [ ] **Step 1: Write the opt-in live Search and Extract tests**

Follow the repository's existing live-provider gate:

```python
pytestmark = pytest.mark.skipif(
    os.environ.get("WEB_SEARCH_RUN_INTEGRATION") != "1",
    reason="live provider integration tests are opt-in",
)
```

Use one environment-variable name dedicated to the Parallel credential. Read only its value from `os.environ`; never commit, print, or snapshot that value. Each live test skips individually when the credential is absent.

Search smoke scenario:

```text
construct real HttpJsonExecutor + ParallelAdapter
api_url = https://api.parallel.ai
search for "OpenAI official website"
assert the return value is a non-empty list
assert each returned hit has string url/title/snippet
assert no exact result count, ordering, URL, title, or snippet text
```

Extract smoke scenario:

```text
fetch normalize_url("https://example.com/")
assert raw_content is a non-empty string
assert content is a non-empty string
assert raw_content == content
```

Do not print the full extracted page body.

- [ ] **Step 2: Verify the default run is skipped and network-free**

```bash
uv run pytest tests/integration/test_live_parallel.py -v
```

Expected:

```text
2 skipped when WEB_SEARCH_RUN_INTEGRATION is not enabled
```

If either test performs a network request in the default run, fix the test gate before proceeding.

- [ ] **Step 3: Run all deterministic Parallel feature tests**

```bash
uv run pytest tests/providers/web/test_parallel.py tests/unit/test_config_web_providers.py tests/runtime/test_runtime_assembly.py tests/docs/test_documented_config.py -v
```

Expected: all deterministic Parallel tests pass without network access.

- [ ] **Step 4: Run the complete development verification gate**

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

Expected:

```text
locked sync succeeds
ruff passes
mypy passes
all default tests pass
live provider integrations remain skipped unless explicitly enabled
```

The pre-feature baseline is `180 passed, 2 skipped`. The final passing count should rise only because of added deterministic tests; the new live Parallel tests add default skips rather than mandatory network calls.

- [ ] **Step 5: Refactor/review the final diff with tests green**

Confirm the implementation footprint is still adapter-local:

```text
no Parallel branch in SearchOrchestrator
no Parallel branch in FetchScheduler
no Parallel branch in ProviderQuotaManager
no Parallel state in URLStore
no protocol or result-shape changes
no HTTP retry changes
no generic provider-option passthrough
```

Re-run the complete gate after any cleanup.

- [ ] **Step 6: Optionally verify the real Parallel V1 contract**

Only when the operator intentionally enables live integration tests, has configured the Parallel credential, and accepts provider request cost:

```bash
WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_parallel.py -v
```

Expected:

```text
live Search parses current Parallel V1 results
live Extract returns non-empty equal raw/content full-page content
```

Do not add retry logic in the test; exercise the production `HttpJsonExecutor` behavior.

---

## Self-Review

### Spec coverage

| Design requirement | Plan task(s) |
|---|---|
| Built-in provider implements existing Search + Fetch contracts only | 1, 4, 8 |
| Parallel V1 `/v1/search` and `/v1/extract` | 1, 4 |
| `x-api-key` authentication through existing secret boundary | 1, 4 |
| Search maps exactly one gateway query to `search_queries=[query]` | 1 |
| No objective, query rewriting, max-results control, or session coupling | 1, 4, 9, 10 |
| Optional mode `turbo`/`fast`/`basic`/`advanced`; omission sends no mode | 2, 7 |
| Independent Search/Extract FetchPolicy options | 2, 5, 7 |
| Exact FetchPolicy key/type/range validation | 7 |
| Search excerpts join with `\n\n` into snippet only | 1, 3 |
| Malformed individual Search result is skipped | 3 |
| Malformed top-level Search response fails provider | 3 |
| Valid empty Search response returns `[]` | 3 |
| Extract always sends `advanced_settings.full_content=true` | 4, 5 |
| Extract `full_content` maps to both raw/content | 4 |
| Extract uses normalized URL matching | 4, 6 |
| Matching Extract error, missing match, malformed URL, invalid full content use existing failure path | 6 |
| Semantic body rejection remains scheduler-owned | 4, 6, 10 |
| Provider-local invalid config raises constructor `TypeError` | 7 |
| Runtime maps constructor `TypeError` to existing startup `ConfigFailure` | 8 |
| Exact top-level Parallel config whitelist | 8 |
| Generic nested config plumbing remains unchanged | 8 |
| Disabled provider is not constructed and does not require a credential | 8 |
| One adapter instance and one shared web quota cover both stages | 8 |
| Existing HTTP retries/status/JSON/cancellation semantics remain authoritative | 8, 10 |
| No new public error code or protocol/result shape | Boundaries, 10 |
| Example config and README expose only selected Parallel options | 9 |
| Live contract tests are opt-in and default network-free | 10 |
| Secrets/raw provider diagnostics/body content are not promoted into logs/errors | 1, 4, 6, 8, 10 |

### File-structure review

Expected feature footprint:

```text
src/agent_search_gateway/providers/web/parallel.py
src/agent_search_gateway/providers/defaults.py

tests/fixtures/providers/parallel/search.json
tests/fixtures/providers/parallel/extract.json
tests/fixtures/providers/parallel/extract_error.json
tests/providers/web/test_parallel.py
tests/unit/test_config_web_providers.py
tests/runtime/test_runtime_assembly.py
tests/docs/test_documented_config.py
tests/integration/test_live_parallel.py

config.example.toml
README.md
```

Reuse `tests/support/http.py::RecordingJsonExecutor`; do not add another HTTP mocking/test framework. No new orchestrator, scheduler, HTTP-executor, daemon, protocol, or acceptance test file is expected. If implementation requires production changes in those layers, stop and re-check the approved architecture rather than silently widening scope.

### Ordering and dependency review

- Search contract exists before optional Search controls and tolerance edge cases.
- Extract contract exists before Extract policy and failure cases.
- Request mapping exists before constructor validation locks its accepted config surface.
- Adapter behavior is complete before default registration/runtime assembly exposes it.
- Registry/runtime wiring exists before the real example config and README advertise the provider.
- Deterministic coverage is complete before optional live contract checks.
- Full-suite verification is last and is expected to prove unchanged orchestration, scheduling, quota, retry, cancellation, protocol, and persistence semantics.

### Type and contract consistency

- `ParallelAdapter.search(query: str) -> list[KeywordSearchHit]` is used consistently; Search excerpts populate only `snippet`.
- `ParallelAdapter.fetch(url: NormalizedURL) -> URLFetchCandidate` is used consistently; `full_content` populates both `raw_content` and `content`.
- Both endpoint methods use the existing `JsonRequester.request_json(...)` transport boundary and stages `"search"` / `"fetch"`.
- Constructor policy parameters stay `Mapping[str, object] | None`; validated copies become adapter-owned `dict[str, object] | None`.
- Policy field names remain exactly `max_age_seconds`, `timeout_seconds`, and `disable_cache_fallback` from validation through request construction.
- Search uses `search_fetch_policy`; Extract uses `extract_fetch_policy`; neither aliases the other.
- `mode` is optional and validated against exactly `turbo`, `fast`, `basic`, and `advanced`.
- `api_url` remains a required provider-specific constructor option; no additional Parallel-only URL validator is introduced beyond established repository patterns.
- No new gateway request type, response type, model field, `ErrorCode`, quota type, scheduler outcome, URL-store field, or result-file field is introduced.

### Final implementation gate

Implementation is ready only after:

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

Optional provider-contract verification remains separate:

```bash
WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_parallel.py -v
```

The default gate must remain network-free and must not expose provider credentials, Search excerpts, Extract full content, or raw provider error payloads in logs or failure messages.

