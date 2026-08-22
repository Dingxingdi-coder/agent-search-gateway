## Testing: Parallel Search and Extract Provider

### 1. Test Strategy

The Parallel provider addition should be verified primarily with the repository's existing deterministic fixture/mock pattern. The feature is an adapter-level extension, so tests should concentrate on request construction, provider-response mapping, provider-specific configuration validation, registry/runtime assembly, and preservation of existing orchestration/fallback semantics. A small opt-in live test should verify the real Parallel V1 contract without becoming a normal CI dependency.

Primary goals:

- Prove `parallel` is registered as an ordinary search-and-fetch web provider with the exact provider-specific option whitelist selected by the architecture.
- Prove normal gateway config plumbing carries `api_url`, `mode`, `search_fetch_policy`, and `extract_fetch_policy` into one `ParallelAdapter` instance without changes to `config.py` or runtime contracts.
- Prove Search sends only the selected minimal Parallel V1 fields: `search_queries=[query]`, optional `mode`, and optional Search fetch policy.
- Prove Search does not send `objective`, `max_results`, session state, or other unselected Parallel controls.
- Prove all Search `excerpts` are joined with `"\n\n"` into `KeywordSearchHit.snippet` and are not copied into `raw_content` or `content`.
- Prove malformed individual Search results are skipped while malformed top-level responses fail the Parallel provider pipeline.
- Prove Extract always requests `advanced_settings.full_content=true` and independently applies only `extract_fetch_policy` when configured.
- Prove a matching non-empty `full_content` becomes both `URLFetchCandidate.raw_content` and `URLFetchCandidate.content`.
- Prove Extract matching errors, missing matching results, malformed matching URLs, and missing/empty full content follow the existing provider execution-failure path.
- Prove provider-specific mode/policy validation fails at runtime construction/config startup rather than during the first network call.
- Prove existing search fan-out, shared per-web-provider quota, fetch scheduling, semantic body validation, error taxonomy, and result contracts do not require Parallel-specific branches.
- Prove the optional live contract test is skipped by default and consumes real Parallel requests only when explicitly enabled with a credential.

No new test framework, fixture framework, HTTP mocking framework, CI service, or mandatory network dependency is introduced.

---

### 2. Test Layers

| Layer | Purpose | Real network? | Suggested location |
|---|---|---:|---|
| Provider adapter | Search/Extract request construction and response mapping | No | `tests/providers/web/test_parallel.py` |
| Provider fixtures | Representative Parallel V1 Search/Extract responses | No | `tests/fixtures/providers/parallel/` |
| Config | Existing whitelist/options plumbing and invalid top-level options | No | `tests/unit/test_config_web_providers.py` |
| Registry | Built-in registration order, capabilities, allowed keys | No | existing registry/runtime coverage |
| Runtime | One adapter instance participates in both enabled stages, shared quota, constructor validation mapping | No | `tests/runtime/test_runtime_assembly.py` |
| Orchestrator/scheduler regression | Existing fan-out/fallback/semantic validation remains provider-agnostic | No | existing orchestrator/scheduler suite; add focused test only if adapter integration exposes an uncovered regression |
| Documentation config | Parallel example configuration stays loadable/documented | No | existing `tests/docs/` patterns |
| Live provider contract | Detect drift between fixtures/adapter and Parallel production V1 | Yes, opt-in | `tests/integration/test_live_parallel.py` |

The deterministic provider adapter tests are the primary correctness tests. The live test is only a contract smoke test and must not replace fixture coverage.

---

### 3. Provider Fixtures

Add a focused fixture directory:

```text
tests/fixtures/providers/parallel/
  search.json
  extract.json
  extract_error.json
```

Fixtures should contain only enough real-shape fields to exercise the adapter contract. Do not copy the entire Parallel schema into fixtures.

#### `search.json`

Representative shape:

```json
{
  "search_id": "search_fixture",
  "results": [
    {
      "url": "https://example.com/parallel",
      "title": "Parallel result",
      "publish_date": "2026-01-01",
      "excerpts": [
        "First relevant excerpt.",
        "Second relevant excerpt."
      ]
    }
  ],
  "session_id": "session_fixture"
}
```

The adapter test consumes `url`, `title`, and `excerpts`. `search_id`, `publish_date`, and `session_id` exist only to demonstrate that unmodeled provider metadata is safely ignored.

#### `extract.json`

Representative shape:

```json
{
  "extract_id": "extract_fixture",
  "results": [
    {
      "url": "https://example.com/parallel",
      "title": "Parallel result",
      "excerpts": ["Excerpted text"],
      "full_content": "# Parallel page\n\nFull page content."
    }
  ],
  "errors": [],
  "session_id": "session_fixture"
}
```

The adapter consumes only matching `url` and non-empty `full_content`.

#### `extract_error.json`

Representative shape:

```json
{
  "extract_id": "extract_fixture_error",
  "results": [],
  "errors": [
    {
      "url": "https://example.com/parallel",
      "error_type": "fetch_error",
      "http_status_code": 500,
      "content": "provider diagnostic text"
    }
  ],
  "session_id": "session_fixture"
}
```

Tests should prove that a matching error causes provider failure without requiring the gateway to preserve or expose the provider's raw error content.

---

### 4. ParallelAdapter Search Tests

Create `tests/providers/web/test_parallel.py` in the same style as `test_brave.py`, `test_tavily.py`, and the other current adapter tests, using `RecordingJsonExecutor`.

#### Basic Search Mapping

Construct:

```python
ParallelAdapter(
    name="parallel",
    api_url="https://parallel.example.test/",
    secret=SecretValue("[REDACTED_SECRET]"),
    http_executor=executor,
)
```

Call:

```python
await adapter.search("hello world")
```

Assert exactly:

- HTTP method is `POST`.
- URL is `https://parallel.example.test/v1/search`.
- headers contain `{"x-api-key": "[REDACTED_SECRET]"}`.
- request JSON is exactly `{"search_queries": ["hello world"]}` when no optional settings are configured.
- request JSON does not contain `objective`, `mode`, `max_results`, `session_id`, or `advanced_settings` when omitted.
- returned hit URL/title match the fixture.
- returned snippet is `"First relevant excerpt.\n\nSecond relevant excerpt."`.
- returned `raw_content == ""` and `content == ""`.

This is the key minimal-request regression test.

#### Search Mode Mapping

Parameterize over:

```text
turbo
fast
basic
advanced
```

For each valid mode:

- adapter construction succeeds;
- Search request includes top-level `"mode": <value>`;
- no mode-specific gateway behavior is added elsewhere.

Also test that `mode=None`/omission leaves the field absent rather than explicitly sending Parallel's current default.

#### Search Fetch Policy Mapping

Configure:

```python
search_fetch_policy={
    "max_age_seconds": 3600,
    "timeout_seconds": 15,
    "disable_cache_fallback": False,
}
```

Assert Search request contains:

```json
{
  "search_queries": ["hello world"],
  "advanced_settings": {
    "fetch_policy": {
      "max_age_seconds": 3600,
      "timeout_seconds": 15,
      "disable_cache_fallback": false
    }
  }
}
```

Assert `extract_fetch_policy`, if configured at the same time, does not appear in Search request construction.

#### Empty/Partial Search Policy

Cover partial policies independently:

- only `max_age_seconds`;
- only `timeout_seconds`;
- only `disable_cache_fallback`;
- empty mapping if the implementation accepts it as a valid no-op policy object.

The request must include only configured policy fields. Do not inject field defaults in the gateway.

#### Multiple Excerpts

Cover:

- two excerpts join in provider order with exactly one blank line separator (`"\n\n"`);
- one excerpt is unchanged;
- an empty excerpts array produces an empty snippet, leaving abstract fallback to `SearchOrchestrator`;
- empty string excerpts remain strings and participate in the join rather than triggering invented filtering semantics, unless implementation uses an existing helper that naturally preserves equivalent output.

Do not sort excerpts or truncate them in the adapter.

#### Individual Result Tolerance

Build a response with valid entries surrounding malformed entries. Parameterize malformed entries such as:

- non-object result;
- missing URL;
- non-string URL;
- empty URL;
- non-string title;
- missing excerpts;
- excerpts not an array;
- excerpt element not a string.

Assert:

- malformed entry is omitted;
- valid entries before/after it are retained in original order;
- the Search call itself succeeds.

This proves exact continuation of current per-result tolerance semantics.

#### Top-Level Search Failure

Cover malformed top-level shapes:

- response is not an object;
- `results` absent;
- `results` not a list.

Assert existing `ExecutionFailure` behavior, not `[]`.

#### Valid Empty Search

For `{"results": []}` plus any otherwise irrelevant metadata, assert `await search(...) == []`.

This distinguishes a valid no-hit response from a malformed provider response.

---

### 5. ParallelAdapter Extract Tests

Use the same `RecordingJsonExecutor` and `normalize_url()`/`NormalizedURL` conventions already used by fetch-capable provider tests.

#### Basic Extract Mapping

Call `fetch()` for the fixture URL and assert:

- HTTP method is `POST`;
- URL is `https://parallel.example.test/v1/extract`;
- header is `{"x-api-key": "[REDACTED_SECRET]"}`;
- request body is exactly:

```json
{
  "urls": ["https://example.com/parallel"],
  "advanced_settings": {
    "full_content": true
  }
}
```

when no Extract policy is configured;
- request does not include `objective`, `search_queries`, `max_chars_total`, `session_id`, or excerpt tuning fields;
- returned candidate is:

```python
URLFetchCandidate(
    raw_content="# Parallel page\n\nFull page content.",
    content="# Parallel page\n\nFull page content.",
)
```

#### Extract Fetch Policy Mapping

Configure `extract_fetch_policy` and assert it is nested beside the mandatory full-content flag:

```json
{
  "urls": ["https://example.com/parallel"],
  "advanced_settings": {
    "full_content": true,
    "fetch_policy": {
      "max_age_seconds": 600,
      "timeout_seconds": 30,
      "disable_cache_fallback": true
    }
  }
}
```

Assert `search_fetch_policy`, even if configured on the same adapter, does not appear in the Extract request.

#### Full Content Is Mandatory Adapter Behavior

Explicit regression assertion:

- every Extract request generated by `ParallelAdapter.fetch()` contains `advanced_settings.full_content is True`;
- there is no adapter configuration path that sets it false or omits it.

This protects the gateway fetch contract from later provider-tuning refactors.

#### URL Matching

Cover:

- exact matching URL;
- textually different but normalization-equivalent result URL is accepted using existing `normalized_match()` behavior;
- unrelated result followed by matching result returns matching body;
- malformed result URL causes existing provider failure where current `normalized_match()` semantics require it.

Do not introduce a Parallel-only URL comparison rule.

#### Matching Error

Using `extract_error.json`, assert the matching error produces existing `ExecutionFailure` for `parallel/fetch`.

Do not assert the provider's raw `content` diagnostic is reproduced in the gateway error message; preferably assert it is absent if the implementation deliberately sanitizes it.

#### No Matching Result

Provide structurally valid `results`/`errors` arrays containing only unrelated URLs and assert provider failure.

#### Invalid Full Content

Parameterize matching result `full_content` as:

- missing;
- `None`;
- non-string;
- empty string;
- whitespace-only string.

Assert provider failure in every case. Do not fall back to `excerpts`.

#### Malformed Top-Level Extract Response

Cover:

- response not object;
- `results` missing/not list;
- `errors` missing/not list, if implementation treats both arrays as required to match the provider V1 response contract.

Assert existing provider execution/protocol failure, not an empty candidate.

---

### 6. Parallel-Specific Configuration Validation Tests

Most nested validation should be tested directly against `ParallelAdapter` because the architecture intentionally keeps Parallel schema knowledge out of `config.py`.

#### Valid Mode

Parameterize valid `turbo`, `fast`, `basic`, `advanced` and omission.

#### Invalid Mode

Parameterize examples:

```text
"ADVANCED"
""
1
True
{}
```

Assert constructor `TypeError`.

#### Policy Container Validation

For each of `search_fetch_policy` and `extract_fetch_policy`, reject non-mapping values such as:

```text
"live"
1
True
[]
```

#### Unknown Policy Key

Reject:

```python
{"max_age_seconds": 600, "unknown": True}
```

Do not silently strip `unknown`.

#### `max_age_seconds`

Accept representative values:

```text
600
3600
86400
```

Reject:

```text
599
0
-1
600.0
True
"600"
```

The boolean case is mandatory because `bool` is an `int` subclass in Python.

#### `timeout_seconds`

Accept representative numeric values consistent with the design:

```text
1
15
30.5
```

Reject:

```text
True
False
"30"
[]
{}
```

Do not add tests for undocumented min/max restrictions the implementation is not supposed to invent.

#### `disable_cache_fallback`

Accept only `True` and `False`; reject integers, strings, and null-like values if the field is explicitly present.

#### Policy Independence

Construct adapter with different Search and Extract policies. Invoke both methods through separate recording executors/responses and assert each endpoint receives only its own policy.

This is the main regression against accidentally sharing one mutable policy object or applying a single policy to both endpoints.

---

### 7. Existing Config Resolver Tests

Extend `tests/unit/test_config_web_providers.py` only enough to prove the existing generic config path handles Parallel-like nested options; do not add Parallel-specific parsing branches to the test helper unless production code actually changes there.

Required coverage:

- a registered provider can whitelist `mode`, `search_fetch_policy`, and `extract_fetch_policy` as top-level option keys;
- nested TOML/dict policy values survive into `ResolvedWebProviderConfig.options` unchanged for adapter validation;
- an unknown top-level provider option is still rejected by existing `_validate_options()`;
- shared fields (`enable_search`, `enable_fetch`, `api_key_env`, `max_concurrency`) are not duplicated into `options`.

If current generic config tests already prove arbitrary mapping values survive in `options`, prefer adding only the minimum assertion needed for the newly documented Parallel example rather than duplicating generic behavior.

---

### 8. Default Registry Tests

Extend the existing built-in-registry assertion used by runtime assembly so the ordered built-in registrations include Parallel with:

```text
("parallel", True, True)
```

Also assert its exact allowed config keys where the suite already has or gains a focused built-in registration assertion:

```python
frozenset({
    "api_url",
    "mode",
    "search_fetch_policy",
    "extract_fetch_policy",
})
```

Do not change `ProviderRegistry` behavior itself.

Registration-order expectations should place Parallel at one deliberate position in `build_default_registry()` and then preserve that order. Because search result merging follows configured provider order rather than registry order after config resolution, no new ranking behavior is implied.

---

### 9. Runtime Assembly Tests

Extend `tests/runtime/test_runtime_assembly.py` with Parallel enabled for both stages in the test config.

Assert:

- one `ParallelAdapter` is instantiated;
- the same object identity appears in both `runtime.web_search_providers` and `runtime.web_fetch_providers`;
- one Parallel web quota is created and shared across both stages;
- configured `max_concurrency` is reflected by `runtime.quotas.get_web("parallel").limit`;
- Parallel contributes one HTTP executor/client, which is closed exactly once by `Runtime.aclose()`;
- runtime debug representation/logging does not contain the provider credential;
- runtime counts/provider-name assertions update only for the newly enabled provider.

#### Invalid Adapter Configuration Maps to Existing ConfigFailure

Resolve a syntactically allowed Parallel config containing, for example:

```toml
mode = "invalid"
```

or an invalid nested policy value, then call `Runtime.build(...)`.

Assert existing `ConfigFailure` with the current generic message pattern for invalid web provider configuration.

This proves adapter-local validation occurs at startup while preserving the existing runtime error boundary.

---

### 10. Orchestrator and Scheduler Regression Coverage

No new Parallel-specific orchestration implementation is expected, so avoid adding broad duplicate tests merely because a new adapter exists.

Existing tests already own these behaviors and must remain green:

- keyword providers run concurrently;
- one keyword provider can fail while another succeeds;
- all keyword provider pipelines failing yields `ALL_PROVIDERS_FAILED`;
- URL normalization/deduplication occurs after provider output;
- web-provider quota is acquired around search;
- fetch providers are selected through `FetchScheduler` rather than fired all at once;
- fetch execution failure falls back to another provider;
- body cheap-check/judge rejection remains semantic failure;
- cancellation propagates;
- shared quota semantics remain per provider.

Add a new orchestrator/scheduler test only if implementation requires new behavior there. The desired implementation should not.

A useful minimal integration-style local test may instantiate `ParallelAdapter` behind an existing fake/recording executor and pass it through runtime/orchestrator if needed to prove no special wiring is required, but this is optional if runtime assembly plus adapter tests already cover the boundary.

---

### 11. Documentation and Example-Config Tests

Update `config.example.toml` with a Parallel section using safe illustrative values. The example should show Search + Fetch capability and the optional controls without implying they are mandatory.

Representative documented shape:

```toml
[web_providers.parallel]
enable_search = true
enable_fetch = true
api_url = "https://api.parallel.ai"
api_key_env = "[REDACTED_SECRET]"
mode = "turbo"

[web_providers.parallel.search_fetch_policy]
max_age_seconds = 3600
timeout_seconds = 15
disable_cache_fallback = false

[web_providers.parallel.extract_fetch_policy]
max_age_seconds = 600
timeout_seconds = 30
disable_cache_fallback = true
```

If existing documentation tests parse/validate `config.example.toml`, extend them only as required so the Parallel example remains compatible with registry/config resolution. Do not require real credentials or network access.

README/provider documentation should state that:

- Parallel supports both Search and Fetch in the gateway;
- `mode` is optional;
- Search and Extract policies are independent;
- Extract always requests full content internally;
- result count remains Parallel's default in this version.

Tests should prefer configuration-parsing assertions over brittle prose snapshots unless current docs tests already enforce exact documented fields.

---

### 12. Opt-In Live Parallel Contract Test

Add `tests/integration/test_live_parallel.py` following the same opt-in pattern already used for Tavily/OpenAI.

#### Activation

The file should be skipped unless the existing live-provider gate is explicitly enabled:

```text
WEB_SEARCH_RUN_INTEGRATION=1
```

Inside each test, skip if the configured Parallel credential is absent.

Normal `pytest` and normal CI therefore perform zero Parallel network calls and incur zero provider charges.

#### Live Search Smoke Test

Construct a real `HttpJsonExecutor` and `ParallelAdapter` using `https://api.parallel.ai` and a real `SecretValue` sourced from the test environment.

Call a stable generic query such as:

```text
OpenAI official website
```

Assert only contract-level invariants:

- result is a non-empty list;
- each returned hit has string `url`, `title`, and `snippet` fields;
- no exact result count, ordering, URL, or snippet text is asserted.

Do not configure Search fetch policy in the baseline live smoke test unless specifically needed to verify that option; the fixture tests already verify request-body mapping deterministically.

#### Live Extract Smoke Test

Fetch a stable public page such as `https://example.com/` through the real adapter.

Assert only:

- returned candidate has non-empty string `raw_content`;
- returned candidate has non-empty string `content`;
- both values are equal under the selected adapter mapping.

Do not assert exact page text because external page representation/caching may change.

The live Extract test proves in particular that the production request with `advanced_settings.full_content=true` yields a response shape the adapter can consume. Parallel's current V1 docs specify `results` and `errors` arrays, with `full_content` available on successful Extract results when requested. The live test is intended to detect drift in precisely that contract.

#### Live-Test Failure Semantics

- Network/provider outage is allowed to fail the opt-in run; it must not affect ordinary test runs.
- Missing credential skips, not fails.
- Do not retry independently in the test; exercise the same `HttpJsonExecutor` retry policy used in production.
- Never print the credential or raw full page body on success.

---

### 13. HTTP Executor Coverage

Do not add Parallel-specific retry tests to `tests/providers/test_http_executor.py`.

Existing executor tests remain the authority for:

- retrying 408/429/5xx;
- timeout/transport retry behavior;
- non-retryable 4xx behavior;
- JSON decode failures;
- sanitized logging;
- client close behavior.

Parallel adapter tests should use `RecordingJsonExecutor` and assert only the method/URL/header/body passed to that boundary.

This keeps transport behavior tested once rather than duplicated per provider.

---

### 14. Negative-Space / Non-Regression Tests

The implementation and test diff should demonstrate that the following are not introduced:

- no new public request/protocol fields;
- no new `ErrorCode`;
- no Parallel branch in `SearchOrchestrator`;
- no Parallel branch in `FetchScheduler`;
- no Parallel branch in `ProviderQuotaManager`;
- no Parallel-specific state in `URLStore`;
- no generic provider option passthrough;
- no `objective` mapping;
- no `max_results` configuration;
- no configurable `full_content` switch;
- no shared Search/Extract fetch policy;
- no mandatory live provider test.

This can be verified primarily through code review plus the unchanged existing regression suite; do not create artificial tests that inspect source-code strings unless an existing architectural test pattern already does so.

---

### 15. Suggested Test File Changes

Expected minimal test/fixture footprint:

```text
tests/
  fixtures/providers/parallel/
    search.json
    extract.json
    extract_error.json
  providers/web/
    test_parallel.py
  integration/
    test_live_parallel.py
  unit/
    test_config_web_providers.py          # small extension only if needed
  runtime/
    test_runtime_assembly.py              # built-in registration/runtime extension
  docs/
    ...                                   # only existing config/doc tests that require updates
```

No new orchestrator, scheduler, HTTP-executor, acceptance, daemon, or protocol test file is expected unless implementation unexpectedly touches those layers. Such a need should be treated as a signal that the implementation may be more invasive than the approved design.

---

### 16. Verification Commands

During implementation, run focused tests first, then the complete suite using the repository's existing tooling.

Focused deterministic verification should include the new adapter tests plus changed config/runtime/docs tests. The normal full suite must run without `WEB_SEARCH_RUN_INTEGRATION=1`, proving no network dependency was introduced.

The optional live verification is a separate explicit run with the integration gate and Parallel credential present. Search and Extract together consume real provider requests, so they should not be run as part of every local edit cycle.

Success criteria:

- all deterministic tests pass without network access;
- all pre-existing tests remain green;
- default test run skips live Parallel calls;
- opt-in live Search parses the current Parallel production Search response;
- opt-in live Extract retrieves non-empty full content through the current Parallel production Extract response;
- no secret is exposed in test output or committed fixture/config data.

---

### 17. External Contract Basis

The fixtures and live smoke assertions are intentionally limited to fields documented by Parallel V1:

- Search V1 uses `POST /v1/search`, requires `search_queries`, supports optional `mode`, and returns ordered `results` whose documented sample fields include `url`, `title`, and `excerpts`.
- Extract V1 uses `POST /v1/extract`; its successful response requires `results` and `errors` arrays, and successful result samples include `url` and `full_content`.
- Extract advanced settings disable full content by default, so the request-construction test must prove the adapter explicitly enables it.
- Current `FetchPolicy` fields are `max_age_seconds`, `timeout_seconds`, and `disable_cache_fallback`, with documented minimum `max_age_seconds=600`.

Provider-document drift should be detected by the opt-in live smoke test, while deterministic fixture tests remain the primary regression suite.
