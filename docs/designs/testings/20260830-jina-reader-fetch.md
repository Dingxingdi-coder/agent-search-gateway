## Testing: Jina Reader Fetch Provider

### 1. Test Strategy

Test only the boundaries changed by this feature: the new Jina adapter and the new credentialless-provider registration/config/runtime behavior. Existing fetch scheduling, fallback, URL-store caching, semantic validation, HTTP retries, and public protocol behavior remain unchanged and should rely on their existing provider-agnostic tests.

Primary goals:

- Prove Jina is registered as fetch-only and credentialless.
- Prove Jina can be enabled without `api_key_env` while existing providers preserve current credential validation.
- Prove runtime assembly constructs Jina without passing a `secret` argument.
- Prove the adapter sends the minimal Reader request: POST to the configured endpoint, target URL in JSON body, and `X-No-Cache: true`.
- Prove non-empty text maps to `URLFetchCandidate(text, text)` and empty text is rejected with existing provider failure semantics.
- Keep tests offline, deterministic, and credential-free.
- Avoid duplicating coverage owned by `HttpJsonExecutor`, `FetchScheduler`, or `FetchOrchestrator`.

No new test framework, live-network suite, or provider-specific rate-limit harness is introduced.

---

### 2. Test Layers

| Layer | Purpose | Real network? | Location |
|---|---|---:|---|
| Jina adapter | Request construction and candidate mapping | No | `tests/providers/web/test_jina.py` |
| Registry | Capability, factory, option whitelist, credential requirement | No | `tests/providers/test_registry.py` |
| Config | Credentialless Jina and unchanged credential-required behavior | No | `tests/unit/test_config_web_providers.py` |
| Runtime assembly | Construction with `secret=None` and existing quota/executor plumbing | No | `tests/runtime/test_runtime_assembly.py` |
| Scheduler/orchestrator | Regression only; no Jina-specific tests | No | existing suites |
| Docs/config example | Existing docs/config tests continue to pass | No | existing `tests/docs/` |

The adapter test is the primary provider correctness test. Registry/config/runtime tests prove integration into the existing architecture.

---

### 3. Jina Adapter Tests

Suggested file: `tests/providers/web/test_jina.py`.

Reuse the existing `RecordingTextExecutor` unchanged. It already records method, URL, stage, headers, and JSON body, so no new test support is required.

#### Successful Fetch

Arrange a `JinaReaderAdapter` with a recording executor returning a sentinel Markdown body and use a normalized target containing path, query, and fragment.

Assert:

- `fetch(target)` returns `URLFetchCandidate(text, text)`.
- HTTP method is `POST`.
- Request URL is the configured Reader endpoint.
- Stage is `fetch`.
- Headers contain `X-No-Cache: true`.
- JSON body is exactly `{"url": str(target)}`.
- The outer request URL does not contain the target URL.
- No Authorization header or credential is present.

The query + fragment target proves the adapter transports the exact normalized target through the request body without a second URL-encoding layer.

#### Empty Body

Parameterize empty, spaces-only, and newline/whitespace-only bodies.

Assert `fetch()` raises existing `ExecutionFailure` with a concise `page body is empty` reason and never returns an empty candidate.

Do not test scheduler fallback here; the adapter test stops at the adapter contract boundary.

#### Invalid API URL

Parameterize empty, whitespace-only, and non-string `api_url` values and assert constructor `TypeError` through existing `configured_string()` validation.

Runtime-level conversion of `TypeError` to `ConfigFailure` is a generic runtime concern and needs only focused shared coverage.

#### Deferred Controls Stay Absent

The exact headers/body assertion should also prove no optional Jina controls are injected, including Authorization, engine, selectors, proxy, locale, or response-format settings. A separate test is unnecessary if the exact request assertion already proves this.

---

### 4. Registry Tests

Update `tests/providers/test_registry.py` with the smallest additive assertions.

#### Jina Registration

Assert the default registry contains Jina with:

```python
name == "jina"
capabilities == ProviderCapabilities(search=False, fetch=True)
factory is JinaReaderAdapter
allowed_config_keys == frozenset({"api_url"})
requires_api_key is False
```

Prefer a focused Jina assertion rather than rewriting the existing provider expectation table solely for the new field.

#### Existing Credential Requirement Regression

Assert existing built-ins retain `requires_api_key is True` through the new defaulted field. Representative registrations are sufficient if that keeps the test concise.

Purpose: prevent the registration change from silently making all web providers credentialless.

Existing positional construction of `WebProviderRegistration` in tests must remain valid because the new field has a default.

---

### 5. Config Tests

Update `tests/unit/test_config_web_providers.py`.

#### Jina Resolves Without Credential

Enable Jina fetch with `api_url` and no `api_key_env`; use an empty environment mapping.

Assert:

- resolution succeeds;
- `enable_fetch is True`, `enable_search is False`;
- `api_key_env is None`;
- `secret is None`;
- options contain only `api_url`.

#### Jina Rejects `api_key_env`

Configure enabled Jina with `api_key_env` present.

Assert resolution fails with `ConfigFailure` / `CONFIG_ERROR`. The environment variable value should not need to be resolved first.

Purpose: prove this integration is explicitly no-credential rather than optional-credential or silently ignored credential.

#### Unsupported Search Capability

Add `("jina", "enable_search")` to the existing unsupported-capability parameterized test rather than creating a separate bespoke test.

#### Existing Credential-Required Behavior Remains

Keep the existing test proving an enabled credential-required provider fails when its key environment variable is absent. Do not add a duplicate copy; if the shared change weakens this invariant, the existing suite must fail.

---

### 6. Runtime Assembly Test

Add one focused test to `tests/runtime/test_runtime_assembly.py` with only Jina enabled and no credential environment variable.

Assert:

- Runtime construction succeeds.
- `runtime.web_search_providers == ()`.
- `runtime.web_fetch_providers` contains exactly one `JinaReaderAdapter` named `jina`.
- The adapter receives the configured `api_url`.
- A normal web quota exists for Jina with the resolved concurrency limit.
- A normal shared HTTP executor is created and closed through existing runtime lifecycle behavior.
- No `SecretValue` is required.

The Jina constructor intentionally does not accept `secret`; accidental passing of a secret kwarg therefore makes this focused runtime test fail through the existing constructor boundary. Do not add a Jina-specific runtime branch to satisfy the test.

---

### 7. Tests Intentionally Not Added

#### No `FetchScheduler` Jina Test

Existing scheduler tests already cover provider execution failure, semantic failure, fallback, candidate validation, capacity selection, and all-provider failure. Jina enters through the unchanged `URLFetchProvider` contract, so provider-name-specific scheduler tests would duplicate existing behavior.

#### No `FetchOrchestrator` Refresh Test

Gateway-level cached-content behavior is explicitly unchanged. Existing tests that prove cached content skips provider fetch remain the regression contract. `X-No-Cache` is verified at the adapter request boundary only when Jina is actually invoked.

#### No New HTTP Executor Tests

This feature does not modify `HttpJsonExecutor`. POST requests, headers, JSON bodies, text responses, timeout handling, 408/429/5xx retries, non-retryable 4xx failures, logging, and redaction are existing transport capabilities with existing tests.

#### No Live Jina Test

A live test is not required. It would introduce external availability/rate-limit flakiness, and verifying real cache refresh would require a controllable changing origin. The stable integration contract is sufficiently tested by exact POST/header/body assertions. A future opt-in smoke test is justified only if API drift becomes a demonstrated maintenance problem.

#### No Rate-Limiter Test

No Jina-specific RPM limiter is introduced. HTTP 429 remains an existing transport/retry/fallback case, so there is no new rate-limit component to test.

---

### 8. Documentation Regression

Update `config.example.toml` with a credentialless Jina block and README's provider capability table with Jina fetch support. Existing documentation/config parsing tests should validate these changes naturally. Add a Jina-specific docs test only if the existing generic tests cannot detect an invalid example configuration.

The example must not contain a dummy `api_key_env` for Jina.

---

### 9. Verification Set

Focused implementation verification should include at least:

```text
tests/providers/web/test_jina.py
tests/providers/test_registry.py
tests/unit/test_config_web_providers.py
tests/runtime/test_runtime_assembly.py
```

Then run the repository's normal full test/lint/type verification because the registration dataclass and generic credential resolution are shared code.

Success criteria:

- Jina's exact minimal Reader request is proven offline.
- Jina resolves and constructs without a credential.
- Existing providers still require credentials by default.
- No Jina-specific core orchestration behavior exists.
- Existing full suite remains green.
