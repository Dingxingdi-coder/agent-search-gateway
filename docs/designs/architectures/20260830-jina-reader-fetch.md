## Architecture: Jina Reader Fetch Provider

### 1. Scope & Assumptions

#### In Scope

- Add Jina Reader as a built-in web provider with `search=false, fetch=true`.
- Use Jina Reader's unauthenticated/free Reader mode; no API key is required or accepted by this integration.
- Force a fresh Reader-side fetch whenever Jina is actually invoked by sending `X-No-Cache: true`.
- Preserve the existing `URLFetchProvider.fetch(url) -> URLFetchCandidate` contract and the existing `FetchScheduler` fallback flow.
- Reuse the existing shared text HTTP execution boundary (`HttpJsonExecutor.request_text`) for timeout, retry, status handling, and observability.
- Add the smallest generic registry/config/runtime support necessary for an enabled web provider that intentionally has no API credential.
- Keep Jina-specific request construction and response validation inside one thin adapter.
- Update only the provider registry, config/runtime credential plumbing, focused tests, example configuration, and provider capability documentation required by the integration.

#### Todo

- Gateway-level force refresh of already-populated `URLStore` content.
- Optional Jina API-key support for higher rate limits.
- Jina Search (`s.jina.ai`).
- Provider-specific Jina controls such as engine selection, selectors, wait conditions, proxy selection, locale, response format, screenshots, or DNT behavior.
- Generic freshness/cache policy configuration across fetch providers.
- RPM-aware provider throttling. Existing gateway quotas remain concurrency limits only.

#### Assumptions

- "Refresh" in this feature means bypassing Jina Reader's own cached page content when the provider is invoked; it does not bypass the gateway's existing `URLStore` short-circuit for previously prepared content.
- Jina Reader supports unauthenticated `POST https://r.jina.ai/` with a JSON body containing the target `url` and returns LLM-ready text.
- `X-No-Cache: true` instructs Jina Reader to ignore its cached result and fetch the target again.
- The current shared HTTP executor already supports text responses and JSON request bodies, so no HTTP transport API change is required.
- Configuration order remains the fetch-provider priority order. No automatic free-first or cost-based sorting is introduced.
- A Jina `429` or other remote failure follows the existing retry/fallback semantics; no Jina-specific rate limiter is added.

---

### 2. Architecture Summary

Jina Reader is added as a normal fetch-only built-in adapter behind the existing `URLFetchProvider` protocol. `JinaReaderAdapter.fetch()` sends one unauthenticated `POST` request to the configured Reader endpoint, puts the normalized target URL in the JSON body, sets `X-No-Cache: true`, rejects an empty response, and maps the returned text directly to `URLFetchCandidate(raw_content=text, content=text)`. The existing `FetchScheduler` continues to own provider selection, quota leasing, retry-independent fallback, cheap body validation, and LLM judgment; `FetchOrchestrator` continues to own URL admission, gateway caching, store mutation, safety, and focus summaries. The only shared-core adjustment is to let a `WebProviderRegistration` declare that its integration is intentionally credentialless, defaulting all existing registrations to the current credential-required behavior. No Jina-specific branch is added to config resolution, runtime assembly, scheduler, orchestrator, storage, CLI, or protocol code.

---

### 3. Design Decisions

#### Runtime Model

##### Add Jina as a Normal Fetch-Only Adapter

- Description: Add `providers/web/jina.py` with `JinaReaderAdapter` implementing only `fetch()` and register it as `ProviderCapabilities(search=False, fetch=True)`.
- Rationale: The current provider architecture already has first-class fetch-only providers and supplies all required fallback, validation, quota, and persistence behavior.
- Trade-offs: Jina-specific capabilities beyond basic Reader fetch are intentionally unavailable.
- Rejected Alternatives:
  - Jina-specific fetch path in `FetchOrchestrator`:
    - Description: Detect Jina centrally and call it outside the provider scheduler.
    - Why Rejected: Duplicates existing provider lifecycle behavior and leaks vendor knowledge into core orchestration.
  - Generic scraping-provider abstraction:
    - Description: Introduce a new base layer for free/paid scraping services.
    - Why Rejected: One additional provider does not justify a new abstraction and would increase migration surface.

##### Preserve Existing Fetch Scheduling and Provider Order

- Description: Jina participates in `FetchScheduler` exactly like every other fetch provider. Its position comes from configured provider order.
- Rationale: Current behavior is deterministic and already supports fallback after execution or semantic failure.
- Trade-offs: The gateway does not automatically prefer Jina because it is free; users who want free-first behavior must place Jina earlier in configuration.
- Rejected Alternatives:
  - Cost-aware scheduler ordering:
    - Description: Add provider pricing metadata and automatic free-first ordering.
    - Why Rejected: This is a separate scheduling-policy feature and is substantially more invasive than provider integration.

#### Interface / Protocol

##### Preserve `URLFetchProvider` and `URLFetchCandidate`

- Description: Jina implements the existing `fetch(NormalizedURL) -> URLFetchCandidate` contract with no new provider-specific fields.
- Rationale: Reader returns page text that maps directly to the existing body contract.
- Trade-offs: Jina metadata such as final URL, title, usage, warning fields, or cache metadata is not represented.
- Rejected Alternatives:
  - Extend `URLFetchCandidate` with provider metadata:
    - Description: Add Jina response metadata to the core domain object.
    - Why Rejected: No current downstream component consumes it, and vendor fields would expand a stable shared contract.

##### Use POST With Target URL in the Request Body

- Description: Send `POST <api_url>` with `json_body={"url": str(normalized_url)}` and `headers={"X-No-Cache": "true"}` through `request_text()`.
- Rationale: The existing executor already supports JSON bodies for text responses. Body transport avoids embedding the target URL in the provider endpoint path used by HTTP lifecycle logs and preserves URL fragments that would otherwise not be transmitted by normal GET URL semantics.
- Trade-offs: The adapter depends on Jina's POST form rather than the visually simpler prefix-style GET form.
- Rejected Alternatives:
  - Prefix-style GET (`https://r.jina.ai/<target>`):
    - Description: Construct Reader URL by prepending the endpoint to the target URL.
    - Why Rejected: It embeds request-specific target data into the provider endpoint path and cannot faithfully transmit hash fragments.
  - New HTTP executor method for sensitive endpoint logging:
    - Description: Add a per-request log-endpoint override to shared transport.
    - Why Rejected: POST body transport solves the issue with existing interfaces and zero transport changes.

#### State Management

##### Keep Refresh Provider-Local

- Description: `X-No-Cache: true` is fixed adapter behavior whenever Jina is called. No refresh flag is added to gateway request models.
- Rationale: The requested benefit is fresh Jina fetches; changing gateway cache semantics would affect `URLFetchRequest`, protocol, singleflight, locking, store mutation, and user-visible behavior.
- Trade-offs: Repeating `url-fetch` for a URL whose `URLStore` record already contains prepared content still returns the gateway-cached content without calling Jina.
- Rejected Alternatives:
  - `force_refresh` on `URLFetchRequest`:
    - Description: Add a public flag that bypasses existing stored content.
    - Why Rejected: It is a separate cross-cutting feature and violates the minimal-intrusion boundary of this provider addition.

#### Storage / Persistence

##### Preserve Existing URL Store Ownership

- Description: Jina returns a candidate only; `FetchOrchestrator` remains solely responsible for merging accepted content into `URLStore`.
- Rationale: Provider adapters are currently side-effect free outside remote HTTP access.
- Trade-offs: Jina-specific freshness or response metadata is not persisted.
- Rejected Alternatives:
  - Store provider/cache metadata in `URLStore`:
    - Description: Persist Jina timestamps/cache state.
    - Why Rejected: The gateway has no consumer for that state and it would couple the core store to one provider.

#### Provider Integration

##### Add One Explicit Jina Adapter

- Description: `JinaReaderAdapter` accepts `name`, `api_url`, and `http_executor`; it does not accept `secret`. It validates `api_url` with the existing `configured_string()` helper, calls `request_text()`, rejects whitespace-only responses with existing `failure(...)`, and returns the response as both `raw_content` and `content`.
- Rationale: This matches the established small-adapter pattern used by text-returning fetch providers while keeping Jina semantics local.
- Trade-offs: Jina's default output wrapper is accepted as-is rather than parsed into title/content components.
- Rejected Alternatives:
  - Parse Jina's text envelope:
    - Description: Strip Jina title/source headers or convert to a custom response model.
    - Why Rejected: The existing fetch pipeline accepts LLM-ready text and does not require those fields separately.
  - Request/parse Jina JSON mode:
    - Description: Use JSON response mode and extract `data.content`.
    - Why Rejected: It adds response-schema parsing with no benefit to the current fetch contract.

#### Configuration

##### Add Generic Credential-Requirement Metadata to Registration

- Description: Extend `WebProviderRegistration` with `requires_api_key: bool = True`. Register Jina with `requires_api_key=False`; all existing registrations inherit `True` unchanged.
- Rationale: The current core assumption that every enabled web provider requires `api_key_env` is stronger than the provider contract itself. Registration metadata is the narrowest provider-agnostic place to express this property.
- Trade-offs: `WebProviderRegistration` gains one field and config/runtime gain one generic conditional.
- Rejected Alternatives:
  - `if name == "jina"` in config/runtime:
    - Description: Special-case Jina when resolving credentials.
    - Why Rejected: Fewer immediate lines but higher architectural intrusion because core code learns a vendor identity.
  - Make credentials optional for every web provider:
    - Description: Stop validating missing `api_key_env` globally.
    - Why Rejected: Weakens startup validation and can move credential failures from deterministic startup to runtime HTTP failures.

##### Treat Credentialless as No-Credential, Not Optional-Credential

- Description: When `requires_api_key=False`, enabled config must omit `api_key_env`; resolved `secret` remains `None`. When `requires_api_key=True`, current required-key behavior remains unchanged.
- Rationale: This feature targets Jina's free unauthenticated mode and should not silently accept, ignore, or partially support paid/free-key semantics.
- Trade-offs: A user cannot supply a Jina API key without a future explicit design change.
- Rejected Alternatives:
  - Accept and ignore `api_key_env`:
    - Description: Permit a resolved secret but do not use it.
    - Why Rejected: Silently unused credentials are misleading and create unnecessary secret handling.
  - Optional credential mode now:
    - Description: Add required/optional/none credential modes.
    - Why Rejected: Only required and none are needed by current built-in integrations; optional mode is YAGNI.

#### Concurrency / Scheduling

##### Reuse Existing Quotas and HTTP Retry Policy

- Description: Jina receives the same provider concurrency gate and shared retry policy as other web providers.
- Rationale: The runtime already creates one quota and one HTTP executor per enabled provider.
- Trade-offs: Concurrency limits do not guarantee compliance with Jina's current per-minute unauthenticated rate limit; `429` responses may still occur.
- Rejected Alternatives:
  - Jina-specific RPM limiter:
    - Description: Track requests/minute separately from provider concurrency.
    - Why Rejected: No existing web provider has this scheduling primitive, and adding it for one free provider would be disproportionate.

#### Security

##### Keep Target URL Out of HTTP Endpoint Logs

- Description: Use POST body transport so shared `http_endpoint_for_log(api_url)` logs only the configured Reader endpoint. Continue relying on existing request-body non-logging behavior.
- Rationale: A prefix-style GET would place the target URL inside the outer endpoint path, including potentially sensitive URL userinfo/path material that the generic endpoint sanitizer cannot recognize as nested URL data.
- Trade-offs: Debug HTTP lifecycle events do not show the target URL at that layer; existing fetch orchestration events continue using the gateway's existing target-URL logging policy.
- Rejected Alternatives:
  - Log nested target URL from the provider request:
    - Description: Preserve exact provider request URL in logs.
    - Why Rejected: It adds unnecessary target-data exposure and is not needed for transport diagnostics.

##### Do Not Introduce Jina Credential Handling

- Description: The free integration sends no Authorization header and creates no `SecretValue` for Jina.
- Rationale: Avoids unnecessary credential surface and matches the intended free mode.
- Trade-offs: Lower provider-side rate limits than authenticated usage.
- Rejected Alternatives:
  - Dummy environment variable:
    - Description: Preserve current config shape by requiring a placeholder key.
    - Why Rejected: Misrepresents security requirements and forces secret plumbing where no secret exists.

#### Observability

##### Reuse Existing Provider and HTTP Events

- Description: No new log-event family is added. Jina appears under the normal provider name in selection, attempt, retry, failure, semantic rejection, and fallback events.
- Rationale: Existing events already contain provider/stage/status/timing dimensions.
- Trade-offs: Jina-native usage/cache metadata is not observable.
- Rejected Alternatives:
  - Jina-specific cache/usage events:
    - Description: Parse and log Jina metadata.
    - Why Rejected: Requires a richer response format and adds observability surface not used by the gateway.

#### Future Migration

##### Generalize Credentials Only When Another Mode Is Proven

- Description: Use a single defaulted `requires_api_key` flag now. If a future provider truly requires optional credentials, redesign the registration field into an explicit credential mode then.
- Rationale: The current built-ins need only two states: credential required or no credential.
- Trade-offs: A later optional-credential provider would require a small migration of registration metadata.
- Rejected Alternatives:
  - Introduce a three-state credential enum immediately:
    - Description: Model required/optional/none before optional is needed.
    - Why Rejected: Adds unused states, branches, and tests contrary to YAGNI.

---

### 4. Component Catalog

| Component | Purpose | Key Responsibilities | Public Interfaces | Dependencies | Owns State? | Data-Flow Role |
|---|---|---|---|---|---|---|
| `JinaReaderAdapter` | Map Jina Reader to gateway fetch contract | Construct POST request, force Reader no-cache, validate non-empty text, create candidate | Existing `fetch(NormalizedURL)` | `TextRequester`, `configured_string`, `failure` | No; immutable config refs only | Adapter / transformer |
| `WebProviderRegistration` | Describe built-in provider behavior | Capabilities, factory, allowed options, credential requirement | Existing registry APIs + defaulted `requires_api_key` field | Provider contracts | Registration metadata | Registry metadata |
| `resolve_web_provider_config` | Validate and resolve web provider config | Preserve capability/options validation; require secret only for credential-required registrations | Existing function | Registry, environment, `SecretValue` | No | Validator / transformer |
| `Runtime._build_web_providers` | Instantiate enabled adapters | Create executor/quota plumbing; pass `secret` only when resolved; preserve credential-required invariant | Existing runtime assembly | Resolved config, registry | Adapter/executor refs | Coordinator / factory |
| `HttpJsonExecutor.request_text` | Shared HTTP transport policy | Request execution, retries, status mapping, timeout, lifecycle logs, text decode | Existing `request_text()` | `httpx`, retry policy, observability | HTTP client | Transport boundary |
| `FetchScheduler` | Run fetch providers until accepted | Capacity selection, provider fallback, candidate validation, cheap check, judge | Existing `fetch_until_accepted()` | Providers, quotas, LLM stages | Per-call attempt state | Scheduler / validator |
| `FetchOrchestrator` | Coordinate user-visible URL fetch | Admission, gateway cache short-circuit, store mutation, safety, summaries | Existing `url_fetch()` | Store, scheduler, LLM stages | Locks/singleflight refs | Coordinator |
| `URLStore` | Hold admitted URL/body state | Existing merge/cache/unavailable semantics | Existing store methods | Domain models | Yes | Store |

Ownership boundary: Jina code must not know about `URLStore`, scheduler ordering, result files, CLI/socket protocol, LLM semantic validation, or other providers. Core scheduler/orchestrator/store code must not know Jina endpoint/header/body fields.

---

### 5. Data Flow

#### 5.1 Existing `url-fetch` Entry Point With Jina Available

```text
CLI / daemon
  -> existing URLFetchRequest(url, focus)
  -> FetchOrchestrator.url_fetch(url, focus)
       normalized_url = normalize_url(url)
       singleflight by (normalized_url, focus)
       lock normalized_url

       snapshot = URLStore.get(normalized_url)
       if snapshot missing:
           raise existing URL_NOT_ADMITTED
       if snapshot.available is false:
           return existing unavailable message

       if snapshot.content exists:
           # Existing gateway cache semantics; Jina is not called.
           prepared = true
       else:
           if snapshot.raw_content is empty:
               -> FetchScheduler.fetch_until_accepted(normalized_url)
                    select configured available fetch provider
                    if selected provider is JinaReaderAdapter:
                        -> JinaReaderAdapter.fetch(normalized_url)
                             POST configured api_url
                             headers = {"X-No-Cache": "true"}
                             json_body = {"url": str(normalized_url)}
                             -> HttpJsonExecutor.request_text(...)
                                  if retryable HTTP/transport failure:
                                      apply existing retry policy
                                  if terminal HTTP/transport failure:
                                      raise existing ExecutionFailure
                                  else:
                                      return response.text
                             if response text is empty/whitespace:
                                 raise existing provider ExecutionFailure
                             return URLFetchCandidate(text, text)
                    FetchScheduler validates candidate type/non-empty raw body
                    if execution failure:
                        try another configured provider if one remains
                    if cheap_check or judge rejects:
                        mark this attempt semantic failure and try another provider
                    if accepted:
                        return accepted candidate
                    if all attempts exhausted:
                        return existing aggregate outcome

               if scheduler execution failure:
                   raise existing ALL_PROVIDERS_FAILED
               if scheduler semantic failure:
                   URLStore.mark_unavailable(normalized_url)
                   return unavailable message
               URLStore.merge_body(candidate.raw_content, candidate.content)

           if content still empty:
               run existing content_clean and merge result

       run existing safety stage
       if safety rejects:
           mark unavailable; return unavailable message
       if focus absent:
           return stored content
       return existing focus summary
```

No new user-visible entry point is introduced.

---

### 6. Interfaces & Contracts

#### Public Request / Response Contract

Unchanged. Existing URL fetch request and response framing remain stable; no `refresh`, provider selector, or Jina field is added.

#### Internal Provider Contract

Unchanged:

```python
class URLFetchProvider(Protocol):
    name: str

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate: ...
```

#### Internal Registration Contract

Minimal additive change:

```python
@dataclass(frozen=True, slots=True)
class WebProviderRegistration:
    name: str
    capabilities: ProviderCapabilities
    factory: WebProviderFactory
    allowed_config_keys: frozenset[str]
    requires_api_key: bool = True
```

Existing positional construction remains valid because the new field has a default.

#### Jina Registration

```python
WebProviderRegistration(
    "jina",
    ProviderCapabilities(search=False, fetch=True),
    JinaReaderAdapter,
    frozenset({"api_url"}),
    requires_api_key=False,
)
```

#### Jina Configuration Contract

```toml
[web_providers.jina]
enable_search = false
enable_fetch = true
api_url = "https://r.jina.ai"
```

`api_key_env` is intentionally absent and rejected for this credentialless integration.

#### Jina HTTP Mapping

```text
POST <api_url>
X-No-Cache: true
Content-Type: application/json  # supplied by existing httpx json_body handling

{"url": "<normalized target URL>"}

successful non-empty text
  -> URLFetchCandidate(raw_content=text, content=text)
```

No provider-specific field crosses the adapter boundary.
