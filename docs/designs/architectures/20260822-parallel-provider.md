## Architecture: Parallel Search and Extract Provider

### 1. Scope & Assumptions

#### In Scope
- Add a built-in `parallel` web provider adapter without changing the gateway's core provider contracts, runtime model, socket protocol, search orchestration, fetch scheduling, quota model, URL store, or result-file format.
- Register Parallel as supporting both keyword search and URL fetch.
- Use Parallel V1 endpoints: `POST /v1/search` for `KeywordSearchProvider.search()` and `POST /v1/extract` for `URLFetchProvider.fetch()`.
- Authenticate Parallel requests with the existing provider secret mechanism and the Parallel `x-api-key` request header.
- Map gateway `keyword_search(query)` to Parallel `search_queries=[query]`; do not synthesize or expose `objective` in this version.
- Leave Parallel result count at the provider default; do not expose `max_results` in this version.
- Support optional Parallel Search `mode` values `turbo`, `basic`, and `advanced`; omit `mode` from the provider request when it is not configured.
- Support independent optional `search_fetch_policy` and `extract_fetch_policy` provider-specific configuration.
- Support all current Parallel `FetchPolicy` fields: `max_age_seconds`, `timeout_seconds`, and `disable_cache_fallback`, with adapter-local field, type, and documented-range validation.
- Map `search_fetch_policy` to `/v1/search` `advanced_settings.fetch_policy` and `extract_fetch_policy` to `/v1/extract` `advanced_settings.fetch_policy`.
- Always request `/v1/extract` with `advanced_settings.full_content=true` because the gateway fetch contract requires page body content.
- Map Parallel Search `excerpts` to the gateway `KeywordSearchHit.snippet` by joining all excerpts with `"\n\n"`; do not map Search excerpts into `raw_content` or `content`.
- Map successful Parallel Extract `full_content` Markdown to both `URLFetchCandidate.raw_content` and `URLFetchCandidate.content`, following the existing pattern for providers whose cleaned representation is also the available source body.
- Extend existing configuration examples/documentation and provider tests in the same style as current built-in providers.

#### Todo
- Parallel `objective` support.
- Multiple rewritten `search_queries` generated from one gateway keyword query.
- Configurable Parallel `max_results`, `max_chars_total`, excerpt settings, source policy, location, session IDs, or client-model hints.
- Generic provider option passthrough.
- A provider-agnostic abstraction for live-fetch/freshness policies.
- Parallel APIs other than Search and Extract.
- Cross-request Parallel `session_id` persistence or coupling between Search and Extract.

#### Assumptions
- Parallel's current V1 contract remains `POST /v1/search` and `POST /v1/extract` under the configured `api_url`, with `x-api-key` authentication.
- Parallel Search requires at least one `search_queries` item and accepts `mode` values `turbo`, `basic`, and `advanced`; omitted mode uses the provider default.
- Parallel Search results provide `url`, optional `title`, and `excerpts`; results are already ordered by provider relevance.
- Parallel Extract accepts one or more URLs, but the gateway's `URLFetchProvider.fetch()` contract remains one normalized URL per call.
- Parallel Extract returns requested full-page content as Markdown in `full_content` when `advanced_settings.full_content=true` is used.
- `FetchPolicy.max_age_seconds`, when provided, must be an integer of at least 600 seconds; `timeout_seconds` must be numeric; `disable_cache_fallback` must be boolean. No undocumented range restrictions will be invented locally.
- Existing `HttpJsonExecutor` remains responsible for transport timeouts, retries, HTTP-status classification, and request lifecycle logging.
- Existing orchestrators remain responsible for provider quotas, candidate validation, URL normalization, duplicate handling, storage mutation, and result writing.

---

### 2. Architecture Summary

Parallel is added as one more built-in web adapter behind the existing `KeywordSearchProvider` and `URLFetchProvider` contracts. The provider registry declares `parallel` as search- and fetch-capable and whitelists only `api_url`, `mode`, `search_fetch_policy`, and `extract_fetch_policy` as provider-specific options. The existing config resolver preserves those options, and `Runtime._build_web_providers()` injects them into a new `ParallelAdapter` together with `name`, `secret`, and `http_executor`. The adapter validates only Parallel-specific configuration and maps gateway calls to Parallel V1 HTTP shapes. Search orchestration continues to run Parallel concurrently with all other enabled keyword providers under the existing per-provider quota, while URL fetch continues to use the existing capacity-aware `FetchScheduler`. No new core abstraction, scheduler path, persistent state, protocol field, result shape, or generic passthrough mechanism is introduced.

---

### 3. Design Decisions

#### Runtime Model

##### Parallel Is a Normal Built-In Web Adapter

- Description: Implement one `ParallelAdapter` that exposes both `search(query)` and `fetch(url)` and register it through the existing built-in `ProviderRegistry`.
- Rationale: The repository already isolates third-party web APIs behind provider adapters. Parallel's Search and Extract APIs map directly onto the existing two web-provider contracts, so the feature requires no new runtime concept.
- Trade-offs: Parallel-specific API evolution must be handled in this adapter rather than through a generic provider-definition layer.
- Rejected Alternatives:
  - Add a Parallel-specific orchestrator or scheduler:
    - Description: Route Parallel calls through dedicated orchestration logic.
    - Why Rejected: It would duplicate existing search/fetch scheduling responsibilities and increase coupling for no semantic benefit.
  - Add a generic config-driven HTTP provider framework:
    - Description: Define arbitrary request/response mappings in TOML.
    - Why Rejected: This changes the architecture and expands validation/security surface far beyond the requested provider addition.

#### Interface / Protocol

##### Preserve All Gateway Public Contracts

- Description: Do not change CLI commands, NDJSON socket frames, `KeywordSearchProvider`, `URLFetchProvider`, `KeywordSearchHit`, `URLFetchCandidate`, `SearchRecord`, or result JSONL shape.
- Rationale: Parallel can be represented completely inside the existing adapter boundary.
- Trade-offs: Parallel-specific capabilities that do not fit current gateway entry points, such as `objective` or sessions, remain unavailable.
- Rejected Alternatives:
  - Extend `keyword-search` with Parallel-specific request fields:
    - Description: Add `objective`, mode, freshness, or result count to the CLI/protocol.
    - Why Rejected: Provider-specific controls would leak into migration-stable public contracts and affect all providers.

##### Provider-Specific Options Stay Explicit and Whitelisted

- Description: Add `api_url`, `mode`, `search_fetch_policy`, and `extract_fetch_policy` to Parallel's `allowed_config_keys`. Preserve the existing config flow in which common fields are parsed centrally, allowed provider-specific options are stored in `ResolvedWebProviderConfig.options`, and runtime passes those options into the adapter constructor.
- Rationale: This catches misspelled or unsupported top-level provider options at startup while preserving the repository's current configuration architecture.
- Trade-offs: Supporting a new Parallel option later requires an explicit registry and adapter change.
- Rejected Alternatives:
  - Arbitrary option passthrough:
    - Description: Forward unknown nested or top-level TOML fields to Parallel.
    - Why Rejected: It removes the existing whitelist boundary, weakens configuration validation, and changes the provider integration model.

##### Use Provider-Native Names for Parallel-Specific Policy Controls

- Description: Expose `search_fetch_policy` and `extract_fetch_policy`, corresponding directly to Parallel Search and Extract endpoint terminology.
- Rationale: These settings exist only under `[web_providers.parallel]`; provider-native endpoint names make it unambiguous which Parallel request is affected and avoid coupling the two endpoints through one shared policy.
- Trade-offs: `extract` differs from the gateway's internal method name `fetch`, so readers must understand that Parallel Extract implements the gateway fetch contract.
- Rejected Alternatives:
  - `search_fetch_policy` plus bare `fetch_policy`:
    - Description: Name the Extract setting after the gateway `fetch()` method.
    - Why Rejected: The asymmetry makes it unclear whether the bare setting affects both endpoints or only gateway fetch.
  - One shared `fetch_policy`:
    - Description: Apply one policy to both Search and Extract.
    - Why Rejected: Parallel defines different default live-fetch behavior for the two endpoints; a shared setting would couple latency/freshness behavior unnecessarily.

#### State Management

##### Do Not Add Parallel Session State

- Description: Do not persist or propagate Parallel `session_id` values between Search and Extract requests.
- Rationale: The current gateway does not model provider sessions, and the requested feature is fully functional without them.
- Trade-offs: The adapter does not take advantage of potential contextual improvements from reusing a Parallel session across related calls.
- Rejected Alternatives:
  - Store Parallel session IDs in `URLStore` or orchestrator state:
    - Why Rejected: It would introduce provider-specific mutable state into core components and change cross-request semantics.

#### Storage / Persistence

##### Keep Existing URL Store and Result Persistence Semantics

- Description: `ParallelAdapter` returns provider-domain candidate objects only. `SearchOrchestrator` and `FetchScheduler` retain ownership of normalization, validation, URL-store mutation, deduplication, availability state, and JSONL result writing.
- Rationale: Provider adapters must not mutate core state; this is an explicit existing architecture boundary.
- Trade-offs: Some provider response metadata such as publish dates, request IDs, warnings, and usage metrics is intentionally discarded because current domain objects have no fields for it.
- Rejected Alternatives:
  - Store Parallel metadata in `URLStore`:
    - Why Rejected: It would expand core data contracts for one provider and violate minimal-intrusion scope.

#### Provider Integration

##### Map Keyword Search Directly to One Parallel Search Query

- Description: `ParallelAdapter.search(query)` sends `POST /v1/search` with `search_queries: [query]`. It does not send `objective`, does not rewrite the query, and does not set `max_results`.
- Rationale: Gateway `keyword-search` already supplies one keyword query. Mapping it directly preserves user intent and avoids introducing hidden query-generation semantics.
- Trade-offs: Parallel documents that multiple concise queries plus an objective can improve retrieval quality, so this minimal mapping may not use the full provider capability.
- Rejected Alternatives:
  - Copy the same input into both `objective` and `search_queries`:
    - Why Rejected: The two Parallel fields have different semantics and the gateway has no basis for treating one input as both.
  - Generate 2-3 search queries internally:
    - Why Rejected: Requires query rewriting logic or an LLM stage and changes keyword-search behavior.

##### Expose Optional Search Mode Without Forcing a Default

- Description: Allow optional `mode = "turbo" | "basic" | "advanced"`. If omitted, do not include the field in the Parallel request.
- Rationale: Mode materially controls provider search behavior/cost/latency, while omission lets Parallel own its documented default and prevents the gateway from copying a provider default into its compatibility contract.
- Trade-offs: Effective behavior can change if Parallel changes its default in a future API revision.
- Rejected Alternatives:
  - Hard-code `advanced`:
    - Why Rejected: Unnecessarily freezes a provider default into gateway behavior.
  - Do not expose mode:
    - Why Rejected: It removes a high-impact provider-native control at very low implementation cost.

##### Keep Parallel Search Excerpts as Search Abstract Material Only

- Description: For each valid result, join all string `excerpts` with `"\n\n"` and store the joined value as `KeywordSearchHit.snippet`. Map optional title to `title`. Leave `raw_content` and `content` empty.
- Rationale: Parallel Search excerpts are relevance-focused snippets, not the complete page body. Treating them as body content would bypass the gateway's intended fetch/body validation semantics.
- Trade-offs: Search results do not opportunistically populate body fields even when excerpts are substantial.
- Rejected Alternatives:
  - Use only the first excerpt:
    - Why Rejected: Discards provider-returned relevant context without reducing external requests.
  - Treat excerpts as `content`:
    - Why Rejected: Semantically misrepresents compressed search excerpts as fetched page content.

##### Tolerate Malformed Individual Search Results

- Description: Require the top-level response and `results` collection to match the expected shape. Within `results`, skip an individual entry if required fields are malformed or its excerpts cannot be parsed, while preserving other valid entries.
- Rationale: This matches existing web adapter behavior and prevents one bad provider result from discarding an otherwise useful search response.
- Trade-offs: A partially malformed provider response can silently yield fewer candidates; existing provider/orchestrator debug logging remains the diagnostic mechanism.
- Rejected Alternatives:
  - Fail the provider on any malformed result:
    - Why Rejected: More brittle than existing adapters and inconsistent with current tolerance semantics.

##### Implement Gateway Fetch with Parallel Extract Full Content

- Description: `ParallelAdapter.fetch(url)` sends `POST /v1/extract` with `urls: [str(url)]` and always sets `advanced_settings.full_content = true`. It does not send `objective` or `search_queries`. For the matching normalized URL, require a non-empty `full_content` string and return it as both `raw_content` and `content`.
- Rationale: Parallel disables full content by default, while the gateway's fetch contract is specifically about obtaining a page body. Explicitly requesting full content is required to implement the contract rather than merely retrieving excerpts.
- Trade-offs: Full-page extraction can return substantially more data than excerpts and may increase downstream body-validation/LLM work.
- Rejected Alternatives:
  - Use default Extract excerpts:
    - Why Rejected: Excerpts are not a complete page body and would weaken existing fetch semantics.
  - Make `full_content` user-configurable:
    - Why Rejected: Disabling it would make the adapter unable to satisfy `URLFetchProvider.fetch()` consistently.

##### Map Fetch Policies Only at the Adapter Boundary

- Description: If configured, map `search_fetch_policy` to Search `advanced_settings.fetch_policy` and `extract_fetch_policy` to Extract `advanced_settings.fetch_policy`. The two policies are independent and omitted independently.
- Rationale: Parallel places the same `FetchPolicy` schema under two endpoint-specific advanced-settings objects, but their default behaviors differ. Adapter mapping preserves provider semantics without introducing a gateway-wide freshness abstraction.
- Trade-offs: Similar configuration is duplicated if a user intentionally wants identical policies for Search and Extract.
- Rejected Alternatives:
  - Introduce a gateway-level freshness policy:
    - Why Rejected: Other providers use different APIs and semantics; standardization is outside the requested change.

##### Validate Parallel-Specific Configuration Locally

- Description: Adapter construction validates `mode` and each supplied policy object. Policy keys are limited to `max_age_seconds`, `timeout_seconds`, and `disable_cache_fallback`; `max_age_seconds` is a non-boolean integer >= 600, `timeout_seconds` is a non-boolean numeric value, and `disable_cache_fallback` is boolean. Missing fields are allowed so Parallel can apply field defaults.
- Rationale: The central config resolver intentionally validates only top-level provider option names. Adapter-local validation catches malformed provider-specific nested configuration at daemon startup rather than at first live API call.
- Trade-offs: The adapter mirrors a small part of Parallel's request schema and must be updated if that schema changes.
- Rejected Alternatives:
  - Let Parallel validate everything remotely:
    - Why Rejected: Configuration mistakes would survive startup and fail only during a user request.
  - Add Parallel-specific branches to `config.py`:
    - Why Rejected: Central config is provider-agnostic today; special-casing one provider would increase core coupling.

##### Use Parallel V1 Authentication and Endpoint Composition

- Description: Build request URLs with the existing endpoint helper from configured `api_url`, targeting `/v1/search` and `/v1/extract`, and send the secret as `x-api-key`.
- Rationale: This follows Parallel's current V1 API while preserving configurable base URLs for tests/proxies and the repository's existing secret wrapper/executor pattern.
- Trade-offs: A future Parallel authentication or path migration requires an adapter update.
- Rejected Alternatives:
  - Hard-code full request URLs:
    - Why Rejected: Prevents existing test/proxy configuration patterns and differs from most current adapters.

#### Concurrency / Scheduling

##### Reuse Existing Provider Quota and Search Fan-Out

- Description: Parallel participates in `SearchOrchestrator.keyword_search()` through the existing `asyncio.gather` fan-out. Calls acquire `ProviderQuotaManager.get_web("parallel")` exactly like other keyword providers.
- Rationale: Multi-provider parallelism already exists and is provider-independent.
- Trade-offs: Parallel Search competes with Parallel Extract for the same configured web-provider concurrency quota, consistent with current provider quota semantics.
- Rejected Alternatives:
  - Separate Parallel search and extract quotas:
    - Why Rejected: Changes the existing rule that one web provider shares one quota across supported stages.

##### Reuse Existing FetchScheduler

- Description: Parallel Extract is one candidate in the existing capacity-aware `FetchScheduler`; the scheduler decides when to try it and moves to another provider when the Parallel attempt fails or returns an unacceptable body.
- Rationale: The scheduler already implements the desired one-provider-at-a-time fetch behavior and semantic body admission.
- Trade-offs: Parallel Extract is not fired speculatively in parallel with other fetch providers even if doing so could reduce tail latency.
- Rejected Alternatives:
  - Parallelize all fetch providers:
    - Why Rejected: Changes request/cost behavior and contradicts the existing fetch scheduling design.

#### Security

##### Keep API Keys Inside Existing Secret Boundaries

- Description: Continue resolving `api_key_env` centrally to `SecretValue`; the adapter reveals it only while constructing the `x-api-key` header. Do not expose API keys in config objects, result records, or new logs.
- Rationale: This preserves the existing secret-redaction and least-exposure model.
- Trade-offs: Live integration tests require an actual environment secret when explicitly enabled.
- Rejected Alternatives:
  - Provider-specific plaintext key config:
    - Why Rejected: Violates the established environment-secret contract.

#### Observability

##### Reuse Existing Transport and Provider Lifecycle Logging

- Description: Do not add Parallel-specific logging infrastructure. `HttpJsonExecutor` continues to emit transport attempt/retry/failure events, and orchestrators continue provider started/completed/failed and candidate events under provider name `parallel`.
- Rationale: Existing logs already identify provider and stage and are sufficient to diagnose adapter behavior without a new observability path.
- Trade-offs: Parallel-specific response metadata such as `search_id`, `extract_id`, usage, and warnings is not promoted into structured logs in this version.
- Rejected Alternatives:
  - Log complete Parallel requests/responses:
    - Why Rejected: Adds noise and risks persisting page/search content; existing observability intentionally avoids body logging.

#### Future Migration

##### Keep Provider-Specific API Growth Local to ParallelAdapter

- Description: Future Parallel options should continue to be added explicitly to the Parallel registration and adapter unless a repeated cross-provider requirement justifies a core abstraction.
- Rationale: This preserves high cohesion and avoids prematurely generalizing vendor-specific semantics.
- Trade-offs: Some configuration mapping remains repetitive across providers.
- Rejected Alternatives:
  - Generalize after the first occurrence:
    - Why Rejected: One provider-specific capability is insufficient evidence for a stable cross-provider abstraction.

---

### 4. Component Catalog

| Component | Purpose | Key Responsibilities | Public Interfaces | Dependencies | Owns State? | Data-Flow Role |
|---|---|---|---|---|---|---|
| `resolve_web_provider_config` | Resolve common and provider-specific TOML config | Parse common web fields, reject unknown enabled providers/capabilities, enforce Parallel top-level option whitelist through registry, resolve API-key env | `resolve_web_provider_config(...)` | `ProviderRegistry`, environment, `SecretValue` | No | Validator / transformer |
| `ProviderRegistry` / Parallel registration | Describe built-in provider capabilities and allowed config surface | Register `parallel`, mark search/fetch support, allow `api_url`, `mode`, `search_fetch_policy`, `extract_fetch_policy` | Existing registry APIs | `ParallelAdapter`, `ProviderCapabilities` | Registration map | Registry |
| `Runtime._build_web_providers` | Instantiate enabled web adapters | Create existing `HttpJsonExecutor`, inject reserved runtime args plus resolved options, translate constructor `TypeError` into existing config failure | Existing runtime assembly | Config, registry, executor | Owns adapter/executor references | Coordinator / factory |
| `ParallelAdapter` | Isolate Parallel V1 API details | Validate Parallel-specific options, authenticate, build Search/Extract requests, parse provider responses, map into gateway contracts | `search(query)`, `fetch(url)` | `JsonRequester`, provider parsing helpers, `SecretValue`, URL normalization helpers | Immutable config only | Adapter / transformer |
| `HttpJsonExecutor` | Execute provider HTTP JSON requests | Timeout, retry, HTTP error classification, lifecycle logging, JSON transport | `request_json(...)` | `httpx`, retry policy | HTTP client | Transport boundary |
| `SearchOrchestrator` | Coordinate all keyword providers | Run Parallel concurrently with peers under quota, validate candidates, normalize/deduplicate URLs, write search results | Existing `keyword_search(...)` | Parallel and other `KeywordSearchProvider`s, quota manager, URL store | Workflow-local aggregation | Coordinator |
| `FetchScheduler` | Select fetch providers and accept usable bodies | Invoke Parallel Extract when selected, validate returned candidate with existing cheap check/judge, try another provider on failure/rejection | Existing `fetch_until_accepted(...)` | Parallel and other `URLFetchProvider`s, quotas, LLM stages | Per-call attempted-provider set | Scheduler / validator |
| `URLStore` / `ResultWriter` | Preserve existing search/fetch state and output | Store admitted normalized URL records and write unchanged JSONL search records | Existing APIs | Orchestrators | Yes, URL records / filesystem outputs | State store / sink |

Ownership boundary: `ParallelAdapter` must not know about or mutate `URLStore`, `ResultWriter`, provider quota scheduling, LLM body judgment, or socket/CLI concerns. Core orchestrators must not know Parallel request/response field names.

---

### 5. Data Flow

#### 5.1 Daemon Startup with Parallel Enabled

```text
ForegroundDaemon.start
  -> load TOML
  -> build_default_registry
       register parallel:
         capabilities = search + fetch
         allowed_config_keys = api_url, mode, search_fetch_policy, extract_fetch_policy
  -> resolve_web_provider_config
       parse enable_search / enable_fetch / api_key_env / max_concurrency
       collect remaining Parallel fields into options
       if unknown top-level Parallel option:
         raise ConfigFailure(CONFIG_ERROR)
       if unsupported stage requested:
         raise ConfigFailure(CONFIG_ERROR)
       if referenced Parallel credential environment variable is missing:
         raise ConfigFailure(CONFIG_ERROR)
  -> Runtime._build_web_providers
       create HttpJsonExecutor(provider_name="parallel")
       construct ParallelAdapter(
         name="parallel",
         secret=SecretValue(...),
         http_executor=executor,
         **parallel_options,
       )
       ParallelAdapter validates:
         api_url shape required by constructor
         mode, if present, is turbo/basic/advanced
         each fetch policy is a mapping
         each policy contains only supported keys
         max_age_seconds is int >= 600 when present
         timeout_seconds is numeric when present
         disable_cache_fallback is bool when present
       if adapter configuration raises TypeError:
         Runtime converts to existing ConfigFailure(CONFIG_ERROR)
       add adapter to search tuple if enable_search
       add adapter to fetch tuple if enable_fetch
  -> construct existing ProviderQuotaManager/SearchOrchestrator/FetchScheduler
  -> daemon becomes ready
```

#### 5.2 `keyword-search` with Parallel

```text
SearchOrchestrator.keyword_search(query)
  -> validate/strip query
  -> asyncio.gather one pipeline per enabled KeywordSearchProvider

Parallel pipeline:
  -> acquire ProviderQuotaManager.get_web("parallel").lease()
  -> ParallelAdapter.search(query)
       request_body = {"search_queries": [query]}
       if mode configured:
         request_body["mode"] = mode
       if search_fetch_policy configured:
         request_body["advanced_settings"]["fetch_policy"] = validated policy
       POST endpoint(api_url, "/v1/search")
         headers = {"x-api-key": secret}
       HttpJsonExecutor handles timeout/retry/HTTP failures
       parse top-level object
       if top-level results is malformed:
         raise existing provider ExecutionFailure
       hits = []
       for result in results:
         try:
           require non-empty string url
           parse optional title
           require excerpts list
           require each excerpt to be a string
           snippet = excerpts joined with "\n\n"
           hits.append(KeywordSearchHit(url, title, snippet))
         catch provider parse failure for this result:
           continue
       return hits
  -> release provider quota
  -> SearchOrchestrator stages each hit with existing validation
       if snippet empty, title may supply abstract
       if neither produces an abstract:
         reject candidate with existing semantics
       normalize URL
       do not treat Parallel Search excerpts as page body
  -> merge provider pipelines in existing configured order
  -> deduplicate normalized URLs
  -> URLStore.admit(...)
  -> ResultWriter writes unchanged keyword JSONL

if Parallel pipeline fails but any provider pipeline completes:
  continue with completed providers
if every keyword provider pipeline fails:
  raise ALL_PROVIDERS_FAILED
```

#### 5.3 `url-fetch` When Parallel Is Selected

```text
FetchOrchestrator.url_fetch(url)
  -> existing admission/singleflight path
  -> FetchScheduler.fetch_until_accepted(normalized_url)
       select available unattempted fetch provider
       if provider == parallel:
         acquire ProviderQuotaManager.get_web("parallel") capacity
         -> ParallelAdapter.fetch(normalized_url)
              request_body = {
                "urls": [str(normalized_url)],
                "advanced_settings": {"full_content": true},
              }
              if extract_fetch_policy configured:
                request_body["advanced_settings"]["fetch_policy"] = validated policy
              POST endpoint(api_url, "/v1/extract")
                headers = {"x-api-key": secret}
              HttpJsonExecutor handles timeout/retry/HTTP failures
              parse required top-level results/errors collections
              for result in results:
                if result.url normalized-matches requested URL:
                  require non-empty full_content string
                  return URLFetchCandidate(
                    raw_content=full_content,
                    content=full_content,
                  )
              for error in errors:
                if error.url normalized-matches requested URL:
                  raise existing provider ExecutionFailure
              raise existing provider ExecutionFailure for missing matching result
         release provider quota
         FetchScheduler validates candidate with existing cheap check + judge
         if provider execution failed or body rejected:
           mark Parallel attempted and try next available fetch provider
         else:
           return accepted candidate
  -> existing FetchOrchestrator writes accepted body state to URLStore
```

---

### 6. Interfaces & Contracts

#### Parallel Provider Configuration Contract

Provider-specific TOML remains under the existing web-provider namespace:

```toml
[web_providers.parallel]
enable_search = true
enable_fetch = true
api_url = "https://api.parallel.ai"
api_key_env = "[REDACTED_SECRET]"
# max_concurrency remains the existing shared web-provider option
# mode is optional; omit to use Parallel's provider default
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

Contract classification:
- `enable_search`, `enable_fetch`, `api_key_env`, `max_concurrency`: existing gateway configuration contract.
- `api_url`, `mode`, `search_fetch_policy`, `extract_fetch_policy`: explicit Parallel-specific configuration contract.
- No arbitrary Parallel fields are accepted.

#### Parallel Adapter Construction Contract

Conceptual constructor shape; implementation may use `Mapping[str, object]` for policy objects to match current runtime option plumbing:

```python
ParallelAdapter(
    *,
    name: str,
    api_url: str,
    secret: SecretValue,
    http_executor: JsonRequester,
    mode: str | None = None,
    search_fetch_policy: Mapping[str, object] | None = None,
    extract_fetch_policy: Mapping[str, object] | None = None,
)
```

This is internal and provider-specific, not a new public gateway contract.

#### FetchPolicy Validation Contract

```text
allowed keys:
  max_age_seconds
  timeout_seconds
  disable_cache_fallback

max_age_seconds:
  optional
  Python int, but not bool
  >= 600

timeout_seconds:
  optional
  Python int or float, but not bool
  no undocumented local range restriction

disable_cache_fallback:
  optional
  Python bool

unknown nested key:
  invalid adapter configuration
```

Partial policy objects are valid. Missing fields are omitted so Parallel applies its own defaults.

#### Parallel Search Request Mapping

```json
{
  "search_queries": ["<gateway keyword query>"],
  "mode": "<optional turbo|basic|advanced>",
  "advanced_settings": {
    "fetch_policy": {
      "max_age_seconds": 3600,
      "timeout_seconds": 15,
      "disable_cache_fallback": false
    }
  }
}
```

`mode` and `advanced_settings` are omitted when not configured. `objective`, `max_results`, sessions, client model, and other advanced settings are not sent.

#### Parallel Search Result Mapping

Provider result:

```json
{
  "url": "https://example.com/page",
  "title": "Example",
  "excerpts": ["Excerpt one", "Excerpt two"]
}
```

Gateway result:

```python
KeywordSearchHit(
    url="https://example.com/page",
    title="Example",
    snippet="Excerpt one\n\nExcerpt two",
    raw_content="",
    content="",
)
```

#### Parallel Extract Request Mapping

```json
{
  "urls": ["https://example.com/page"],
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

`fetch_policy` is omitted when `extract_fetch_policy` is not configured. `full_content` is never omitted or set false by this adapter.

#### Parallel Extract Result Mapping

Provider result for the requested URL:

```json
{
  "url": "https://example.com/page",
  "full_content": "# Full page markdown\n..."
}
```

Gateway result:

```python
URLFetchCandidate(
    raw_content="# Full page markdown\n...",
    content="# Full page markdown\n...",
)
```

Provider response identifiers, publish dates, warnings, usage records, excerpts, and errors do not extend gateway domain objects. Matching `errors` entries are converted into the existing provider execution-failure path.

#### External Provider Contract References

- Parallel Search API: `https://docs.parallel.ai/api-reference/search/search`
- Parallel OpenAPI specification: `https://docs.parallel.ai/public-openapi.json`

These references define the provider-specific HTTP fields only; the gateway's internal/public contracts remain those already present in the repository.
