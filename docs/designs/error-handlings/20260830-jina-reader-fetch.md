## Error Handling: Jina Reader Fetch Provider

### 1. Principles

This feature preserves the gateway's existing error taxonomy and fetch fallback model. Jina Reader introduces no new public error code, exception hierarchy, retry subsystem, cache error, or scheduler branch.

Primary rules:

- Configuration problems fail at startup through existing `ConfigFailure(ErrorCode.CONFIG_ERROR, ...)` paths.
- Jina is fetch-only; enabling search fails through the existing capability check.
- Jina's free integration is credentialless. It does not create or consume `SecretValue`.
- Existing credential-required providers retain their current `api_key_env` validation unchanged.
- HTTP status, timeout, transport retry, and lifecycle logging remain owned by `HttpJsonExecutor.request_text()`.
- Jina adapter validation is limited to provider-specific construction and non-empty fetched text.
- A non-empty but semantically unusable page remains a `FetchScheduler` semantic failure, not an adapter/transport failure.
- `asyncio.CancelledError` propagates unchanged.
- Provider response bodies, request bodies, and secrets are never added to logs or error messages.
- No change is made to `ErrorCode`.

---

### 2. Configuration Failures

#### Unsupported Search Capability

Condition:

- `web_providers.jina.enable_search = true`.

Handling:

- Existing config resolution checks `registration.capabilities.search`.
- Raise `ConfigFailure(ErrorCode.CONFIG_ERROR, "web provider jina does not support search")` using the current generic path.
- Do not defer this to a missing `search()` method at runtime.

#### Missing Credential for Existing Providers

Condition:

- Any built-in registration with `requires_api_key=True` is enabled but has no valid `api_key_env` or the referenced environment variable is empty/unset.

Handling:

- Preserve current startup failure behavior exactly.
- The new registration metadata must not make credentials optional globally.

#### Credential Supplied to Jina Free Integration

Condition:

- Jina is enabled and its table contains `api_key_env`.

Handling:

- Raise `ConfigFailure(ErrorCode.CONFIG_ERROR, ...)` during config resolution.
- Do not resolve the environment variable and then silently ignore the secret.

Rationale:

- This design intentionally supports Jina's unauthenticated/free mode only. Accepting unused credentials creates ambiguous security semantics and hidden configuration mistakes.

#### Unknown Provider Option

Condition:

- Jina config contains any provider-specific key other than `api_url`.

Handling:

- Reuse `_validate_options()` and the registration's `allowed_config_keys`.
- Fail startup with existing `CONFIG_ERROR` behavior.
- Do not add Jina-specific option parsing in `config.py`.

#### Invalid `api_url`

Condition:

- Jina is enabled and `api_url` passed to `JinaReaderAdapter` is missing, non-string, empty, or whitespace-only.

Handling:

- Adapter construction uses existing `configured_string()` validation and raises `TypeError`.
- Existing `Runtime._build_web_providers()` constructor boundary translates this to `ConfigFailure(ErrorCode.CONFIG_ERROR, "Invalid configuration for web provider jina")`.
- Do not add a Jina branch in central config validation.

#### Disabled Jina Provider

Condition:

- Both `enable_search=false` and `enable_fetch=false`.

Handling:

- Preserve current disabled-provider behavior: no credential requirement, no adapter construction, no network access.
- Top-level option whitelisting still applies.

---

### 3. Runtime Assembly Failures

#### Credentialless Adapter Construction

Handling:

- `Runtime._build_web_providers()` must allow an enabled provider whose registration says `requires_api_key=False` and whose resolved `secret` is `None`.
- `secret` must not be passed to the Jina factory.
- Existing credential-required registrations must still fail if their resolved secret is unexpectedly `None`.

Rationale:

- The runtime change is a narrow generalization of credential plumbing, not a relaxation of provider authentication invariants.

#### Unexpected Constructor Error

Handling:

- Preserve the current `TypeError -> ConfigFailure(CONFIG_ERROR)` conversion.
- No provider-specific constructor exception type is introduced.

---

### 4. Shared HTTP Failures

Jina uses the existing `HttpJsonExecutor.request_text()` path:

```text
POST <configured api_url>
headers = {"X-No-Cache": "true"}
json_body = {"url": "<normalized target>"}
```

No change to HTTP executor behavior is required.

#### Retryable Status

Condition:

- HTTP 408, 429, or any 5xx response from Jina.

Handling:

- Apply the existing resolved retry policy.
- Emit the existing `http_retrying` event family.
- After retry exhaustion, raise the existing provider `ExecutionFailure` status failure.
- `FetchScheduler` records the execution failure and may fall back to another unattempted provider.

Jina note:

- The unauthenticated Reader service may return 429 under its public rate limits. This feature intentionally treats that as normal provider failure/retry/fallback rather than introducing a Jina-specific RPM scheduler.

#### Timeout or Transport Error

Condition:

- `httpx.TimeoutException` or `httpx.TransportError` while calling Jina.

Handling:

- Use existing retry behavior.
- After exhaustion, raise the existing transport `ExecutionFailure`.
- Adapter code must not implement a second retry loop.

#### Non-Retryable HTTP 4xx

Condition:

- Any HTTP 4xx other than 408/429.

Handling:

- Preserve current non-retryable status behavior.
- Raise existing provider `ExecutionFailure`.
- Scheduler may fall back to another fetch provider.
- Do not introduce special public errors for Jina-specific target rejection or service policy responses.

#### HTTP Success With Text

Handling:

- `request_text()` returns `response.text` unchanged.
- It does not parse Jina-specific headers or metadata.
- It does not log the response body.

---

### 5. Adapter Failures

#### Empty Page Body

Condition:

- Jina returns HTTP success but `response.text` is empty or whitespace-only.

Handling:

- `JinaReaderAdapter.fetch()` raises the existing provider failure form, e.g. `failure(name, "fetch", "page body is empty")`.
- Do not construct `URLFetchCandidate(raw_content="")`.
- Do not mark the URL unavailable inside the adapter.
- `FetchScheduler` treats this as execution failure and may try another provider.

#### Non-Empty Error-Like Text

Condition:

- Jina returns a non-empty text response that is not useful page content.

Handling:

- The adapter does not maintain a list of Jina error strings or heuristically parse text envelopes.
- Return the non-empty text as `URLFetchCandidate`.
- Existing `cheap_check()` and LLM judge determine semantic acceptability.

Rationale:

- Adding provider-specific textual error heuristics would be brittle and duplicate the gateway's existing semantic validation layer.

#### Candidate Mapping

Handling:

- Successful text maps to `URLFetchCandidate(text, text)`.
- No JSON parsing or metadata extraction is performed.
- Existing scheduler candidate type/non-empty validation remains authoritative.

---

### 6. Semantic Failure and Fallback

#### Valid Candidate Rejected by Gateway

Condition:

- Jina returns non-empty text, but `cheap_check()` fails or the LLM judge returns `ok=false`.

Handling:

- Preserve `FetchScheduler` semantic-failure behavior.
- Log the existing body-rejection/provider-fallback events.
- Attempt another configured fetch provider if available.
- Do not retry Jina with alternate headers, engines, rendering modes, or cache policies.

#### All Providers Fail

Handling:

- Preserve existing scheduler/orchestrator aggregation behavior.
- If all attempts are execution failures, existing `ALL_PROVIDERS_FAILED` behavior remains.
- If semantic failure is seen according to current scheduler rules, preserve the existing semantic-failure outcome and URL unavailability behavior.
- No Jina-specific aggregate error is added.

---

### 7. Refresh Boundary

#### Reader-Side Refresh

Handling:

- Every actual Jina call includes `X-No-Cache: true`.
- The adapter does not expose this as a user-configurable flag in this feature.

#### Gateway-Cached Content

Condition:

- `URLStore` already contains prepared `content` for the admitted URL.

Handling:

- Preserve `FetchOrchestrator._prepare_content()` behavior: return cached content and do not call any provider.
- Do not treat this as a Jina cache failure.
- Do not add `force_refresh`, clear store state, or modify singleflight keys in this feature.

Rationale:

- Reader-side cache bypass and gateway-level refresh are distinct behaviors. Only the former is in scope.

---

### 8. Cancellation

Condition:

- The request is cancelled while waiting for quota, retrying, performing Jina HTTP I/O, validating the candidate, or running downstream LLM stages.

Handling:

- Preserve `asyncio.CancelledError` propagation.
- Never translate cancellation into `ALL_PROVIDERS_FAILED`.
- Existing quota/context-manager cleanup remains responsible for releasing capacity.

---

### 9. Sensitive Data and Logging

#### Target URL at HTTP Boundary

Handling:

- Use Jina's POST form with the target URL in `json_body` rather than embedding the target inside the Reader endpoint path.
- Existing HTTP lifecycle logs sanitize/log only the configured `api_url` endpoint.
- Existing HTTP code does not log request JSON bodies.

Rationale:

- This minimizes target-URL exposure at the transport log layer and avoids nested-URL sanitization problems.

#### Provider-Level URL Logging

Handling:

- Existing fetch scheduler/orchestrator target URL logging policy remains unchanged.
- This feature does not add another Jina-specific target log.

#### Credentials

Handling:

- Jina free mode creates no secret and sends no Authorization header.
- Existing secret-redaction behavior for other providers remains unchanged.

#### Response Bodies

Handling:

- Never add Jina page text to exceptions or new log fields.
- Existing length-only provider/candidate telemetry remains sufficient.

---

### 10. Exception Boundaries

```text
config resolution
  -> Jina search enabled => ConfigFailure(CONFIG_ERROR)
  -> Jina api_key_env supplied => ConfigFailure(CONFIG_ERROR)
  -> credential-required provider missing key => existing ConfigFailure(CONFIG_ERROR)

Runtime._build_web_providers
  -> credentialless Jina: construct without secret kwarg
  -> invalid adapter configuration TypeError
       => existing ConfigFailure(CONFIG_ERROR)

JinaReaderAdapter.fetch
  -> HttpJsonExecutor.request_text
       retryable status / transport => existing retries
       terminal HTTP / transport => ExecutionFailure
       success => str
  -> empty/whitespace str => existing provider ExecutionFailure
  -> non-empty str => URLFetchCandidate(text, text)

FetchScheduler
  -> execution failure => existing fallback
  -> semantic rejection => existing fallback
  -> accepted => existing orchestrator/store flow
```

No new exception family or public error contract is introduced.
