## Error Handling: Parallel Search and Extract Provider

### 1. Error-Handling Principles

This feature preserves the gateway's existing error taxonomy and fallback semantics. Parallel-specific failures are translated at the same boundaries used by current web providers; no new public error code, exception hierarchy, retry path, or scheduler behavior is introduced.

Primary rules:

- Invalid enabled Parallel configuration fails daemon startup with the existing `ConfigFailure(ErrorCode.CONFIG_ERROR, ...)` path.
- HTTP timeouts, transport failures, retryable statuses, non-retryable HTTP errors, and invalid JSON remain the responsibility of `HttpJsonExecutor`.
- Parallel response-shape violations use the existing web-provider parsing helpers and `ExecutionFailure` / `ProtocolFailure` semantics.
- Search is tolerant at the individual result-entry boundary: malformed entries are skipped when the adapter can isolate the failure to that entry.
- A malformed top-level Search response fails the Parallel search pipeline, not the whole keyword-search command unless every provider pipeline fails.
- Extract is a single-URL fetch attempt. A malformed/missing matching result, a matching provider error, or missing non-empty `full_content` fails the Parallel fetch attempt so the existing `FetchScheduler` can try another provider.
- Body quality rejection (`cheap_check` or judge rejection) remains a semantic fetch outcome owned by `FetchScheduler`; the Parallel adapter must not classify it as a provider protocol error.
- `asyncio.CancelledError` is always re-raised and never converted into provider failure.
- Provider secrets, request bodies, full page content, excerpts, and raw provider error payloads are not included in new exception messages.

---

### 2. Configuration Failures

#### Unknown Parallel Top-Level Option

Condition:

- `[web_providers.parallel]` contains a field other than the existing shared web-provider fields or Parallel's declared `api_url`, `mode`, `search_fetch_policy`, or `extract_fetch_policy`.

Handling:

- Existing `resolve_web_provider_config()` calls `_validate_options()` against the Parallel registration whitelist.
- Raise the existing `ConfigFailure(ErrorCode.CONFIG_ERROR, "unknown config key(s) for parallel: ...")`.
- Daemon startup fails before adapter/runtime construction or any network request.

Rationale:

- This is the existing provider configuration boundary and catches spelling mistakes without provider-specific branches in `config.py`.

#### Missing Credential Environment Variable

Condition:

- Parallel is enabled for Search or Fetch and `api_key_env` is missing, empty, or points to an unset/empty environment variable.

Handling:

- Preserve the existing web-provider configuration behavior.
- Fail startup with `ConfigFailure(ErrorCode.CONFIG_ERROR, ...)`.
- The message may identify the environment-variable name but must never include a credential value.

#### Invalid Parallel Search Mode

Condition:

- `mode` is provided but is not one of `turbo`, `basic`, or `advanced`, or is not a string.

Handling:

- `ParallelAdapter` validates it during construction.
- Raise `TypeError` from adapter construction.
- Preserve existing `Runtime._build_web_providers()` behavior: catch constructor `TypeError` and raise `ConfigFailure(ErrorCode.CONFIG_ERROR, "Invalid configuration for web provider parallel")`.
- Do not defer validation until the first Search request.

Rationale:

- Adapter-local validation keeps provider-specific schema knowledge outside central config while existing runtime assembly already has a constructor-error-to-config-error boundary.

Trade-off:

- The current runtime conversion intentionally returns a generic provider-configuration message, so the exact invalid mode is not surfaced unless the runtime error model is changed. This feature does not change that core behavior.

#### Invalid Fetch Policy Container

Condition:

- `search_fetch_policy` or `extract_fetch_policy` is provided but is not a mapping/TOML table.

Handling:

- Reject during `ParallelAdapter` construction with `TypeError`.
- Runtime converts it to the existing `CONFIG_ERROR` startup failure.

#### Unknown Nested Fetch Policy Field

Condition:

- Either policy contains a key other than:
  - `max_age_seconds`
  - `timeout_seconds`
  - `disable_cache_fallback`

Handling:

- Reject during adapter construction with `TypeError`.
- Do not silently drop unknown nested fields and do not forward them to Parallel.

Rationale:

- Top-level provider options are already whitelisted centrally; the adapter owns the equivalent whitelist for its provider-native nested object.

#### Invalid `max_age_seconds`

Condition:

- Present value is a boolean, non-integer, or integer below 600.

Handling:

- Reject during adapter construction with `TypeError`.
- Runtime converts it to startup `CONFIG_ERROR`.

Notes:

- Explicitly reject booleans because Python `bool` is a subclass of `int`.
- Omission is valid and means Parallel applies its own field default.

#### Invalid `timeout_seconds`

Condition:

- Present value is not an `int` or `float`, or is a boolean.

Handling:

- Reject during adapter construction with `TypeError`.
- Do not invent a local numeric range beyond the provider contract already selected for this design.

#### Invalid `disable_cache_fallback`

Condition:

- Present value is not `bool`.

Handling:

- Reject during adapter construction with `TypeError`.

#### Disabled Parallel Provider

Condition:

- Both `enable_search=false` and `enable_fetch=false`.

Handling:

- Preserve current config behavior: no API credential is required and no adapter is constructed.
- Existing top-level option-name validation still applies because it occurs before the disabled-provider return path.
- Nested policy values do not need adapter validation because the adapter is not part of the runtime.

Rationale:

- This exactly preserves the behavior of disabled existing providers and avoids adding validation work for unused provider configuration.

---

### 3. HTTP / Transport Failures

Parallel requests use `HttpJsonExecutor` without provider-specific retry logic.

#### Retryable HTTP Status

Condition:

- Parallel returns HTTP `408`, `429`, or any `5xx` response.

Handling:

- Existing `HttpJsonExecutor` retries according to the resolved gateway `RetryPolicy`.
- After retry exhaustion, raise existing `ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "parallel/<stage>: HTTP status ...")`.
- Search: the Parallel pipeline fails; other keyword providers continue.
- Fetch: the Parallel attempt becomes an execution failure; `FetchScheduler` tries another unattempted provider if available.

#### Timeout or Transport Failure

Condition:

- `httpx.TimeoutException` or `httpx.TransportError` occurs.

Handling:

- Existing executor retry policy applies.
- After retry exhaustion, raise existing `ExecutionFailure` with transport-failure semantics.
- Do not add retries inside `ParallelAdapter`.

#### Non-Retryable HTTP 4xx

Condition:

- Parallel returns `4xx` other than `408` or `429`.

Handling:

- `HttpJsonExecutor` does not retry.
- Raise existing `ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, ...)` for the stage.
- Authentication/authorization failures therefore fail only this provider pipeline/attempt under the current orchestrator/scheduler rules.

#### Invalid JSON

Condition:

- Successful HTTP response cannot be decoded as JSON.

Handling:

- Existing `HttpJsonExecutor` raises `ProtocolFailure(ErrorCode.PROTOCOL_ERROR, "parallel/<stage>: response was not valid JSON")`.
- Adapter must not catch and downgrade it to an empty result.

#### Cancellation

Condition:

- Search or Extract task is cancelled while waiting for quota, HTTP, parsing, or orchestration.

Handling:

- Re-raise `asyncio.CancelledError` through all Parallel adapter paths.
- Do not convert cancellation into `ALL_PROVIDERS_FAILED`.
- Existing quota/context-manager cleanup remains responsible for releasing acquired capacity.

---

### 4. Search Response Failures

#### Malformed Top-Level Search Response

Condition examples:

- Response root is not an object.
- Required `results` is absent or is not an array.

Handling:

- Use existing `require_object()` / `require_list()` helpers.
- Raise `ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "parallel/search: ...")`.
- `SearchOrchestrator._run_keyword_pipeline()` logs/fails only the Parallel pipeline.
- If at least one other keyword provider pipeline completes, the command still succeeds using completed providers.
- If no keyword provider pipeline completes, existing `keyword_search()` raises `ALL_PROVIDERS_FAILED`.

#### Malformed Individual Search Result

Condition examples:

- Result is not an object.
- `url` is missing, empty, or not a string.
- `title`, when present, is not a string.
- `excerpts` is missing or not an array.
- Any excerpt element is not a string.

Handling:

- Parse each result inside the same per-entry `try/except ExecutionFailure` pattern used by existing adapters.
- Skip the malformed result and continue parsing subsequent entries.
- Do not fail the whole Parallel search pipeline solely because one result entry is structurally malformed.

Rationale:

- This exactly follows the current web-provider tolerance boundary.

#### Empty Excerpts

Condition:

- `excerpts` is a valid but empty array, or all valid excerpt strings join to an empty/whitespace snippet.

Handling:

- Return a `KeywordSearchHit` with an empty snippet if the entry is otherwise structurally valid.
- Existing `SearchOrchestrator._stage_keyword_hit()` uses title as the fallback abstract.
- If both snippet and title are empty, existing candidate logic rejects that hit with `empty_abstract`.

Rationale:

- Abstract acceptance/rejection already belongs to the orchestrator; the adapter should not duplicate it.

#### Syntactically Invalid URL String

Condition:

- Parallel returns a non-empty string in `url`, but the gateway's later URL normalization rejects it.

Handling:

- Do not add Parallel-specific URL normalization inside the adapter.
- Preserve the same downstream normalization behavior used for existing search providers.

Rationale:

- Moving URL-domain validation into only one adapter would make provider behavior inconsistent and duplicate an orchestrator responsibility.

#### Empty Search Results

Condition:

- Search response is structurally valid and `results=[]`, or every individual result is skipped as malformed.

Handling:

- Return an empty `list[KeywordSearchHit]`.
- This counts as a completed Parallel provider pipeline under existing orchestration semantics, not an execution failure.

---

### 5. Extract Response Failures

Extract represents one gateway fetch attempt for one normalized URL. The adapter must either return one valid non-empty full-content candidate or raise an existing execution failure.

#### Malformed Top-Level Extract Response

Condition examples:

- Response root is not an object.
- Required result/error collections do not have the expected array shape.

Handling:

- Use existing provider parsing helpers.
- Raise `ExecutionFailure`/`ProtocolFailure` through the existing adapter conventions.
- `FetchScheduler._attempt()` converts it into an `execution_failure` outcome and may select another provider.

#### Matching Extract Result Has Missing/Invalid `full_content`

Condition:

- A result URL normalized-matches the requested URL, but `full_content` is absent, not a string, empty, or whitespace-only.

Handling:

- Treat it as provider execution/protocol failure using existing `failure("parallel", "fetch", ...)` / `non_empty_string()` helpers.
- Do not return excerpts as a fallback body.
- Do not mark the URL unavailable directly from the adapter.

Rationale:

- The adapter always requests `full_content=true`; a successful gateway fetch candidate therefore requires actual full content.

#### Matching Provider Error

Condition:

- Parallel's Extract response contains an error entry corresponding to the requested normalized URL and no usable matching result has been returned.

Handling:

- Raise an existing `ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, ...)` for `parallel/fetch`.
- Do not include the complete raw provider error object or page/request content in the exception message.
- The scheduler may try another fetch provider.

#### No Matching Result or Error

Condition:

- Extract response is structurally valid, but neither a successful result nor a recognized error entry corresponds to the requested normalized URL.

Handling:

- Raise an existing provider `ExecutionFailure` with a concise reason such as `matching extraction result was not returned`.
- Scheduler treats it as an execution failure and may fall back.

#### Provider Returns a Different URL Representation

Condition:

- Result URL differs textually but normalizes to the requested gateway URL.

Handling:

- Accept it using existing `normalized_match()` behavior.

Condition:

- Result URL is malformed such that it cannot be normalized.

Handling:

- Preserve existing `normalized_match()` semantics: raise provider execution failure rather than accepting a potentially mismatched body.

#### Body Fails Gateway Semantic Validation

Condition:

- Parallel returns valid non-empty full content, but existing `cheap_check()` fails or judge returns `ok=false`.

Handling:

- This is not a Parallel adapter error.
- `FetchScheduler` returns `semantic_failure` for that attempt and continues according to its existing provider-selection logic.
- Parallel adapter does not rewrite, retry, or substitute excerpts.

Rationale:

- Transport/protocol validity and body acceptability are separate existing layers.

---

### 6. Exception-Boundary Rules

#### Adapter Constructor

Expected invalid Parallel-specific config:

```text
ParallelAdapter.__init__
  -> validate mode/policies
  -> raise TypeError
Runtime._build_web_providers
  -> catch TypeError
  -> ConfigFailure(CONFIG_ERROR, "Invalid configuration for web provider parallel")
```

No new constructor exception type is added.

#### Search Adapter

Expected provider-response parse failures use `ExecutionFailure` from shared provider parsing helpers. The adapter catches `ExecutionFailure` only around an individual Search result so it can skip that result. It must not catch:

- top-level response parse failures;
- HTTP/transport failures from `HttpJsonExecutor`;
- cancellation.

#### Fetch Adapter

Do not catch and suppress provider parsing/execution failures. A fetch attempt needs one valid candidate, so failures propagate to `FetchScheduler`.

#### Orchestrator / Scheduler

Do not add Parallel-specific exception branches:

- `SearchOrchestrator` retains its current per-provider gather/fallback behavior.
- `FetchScheduler` retains its current `ExecutionFailure` versus semantic-failure classification.
- Unexpected non-`ExecutionFailure` exceptions remain wrapped by the existing orchestrator/scheduler generic invalid-data paths.

---

### 7. Error Mapping Matrix

| Failure | Detection owner | Local action | Existing outward behavior |
|---|---|---|---|
| Unknown `[web_providers.parallel]` key | Config resolver | Reject startup | `CONFIG_ERROR` |
| Missing enabled-provider API key env | Config resolver | Reject startup | `CONFIG_ERROR` |
| Invalid `mode` | `ParallelAdapter.__init__` | `TypeError` | Runtime maps to `CONFIG_ERROR` |
| Invalid policy container/key/type/range | `ParallelAdapter.__init__` | `TypeError` | Runtime maps to `CONFIG_ERROR` |
| HTTP 408/429/5xx | `HttpJsonExecutor` | Retry, then fail | Provider execution failure / fallback |
| Timeout/transport error | `HttpJsonExecutor` | Retry, then fail | Provider execution failure / fallback |
| Other HTTP 4xx | `HttpJsonExecutor` | Fail without retry | Provider execution failure / fallback |
| Invalid JSON | `HttpJsonExecutor` | `ProtocolFailure` | Provider pipeline/attempt failure |
| Search top-level malformed | `ParallelAdapter.search` | Raise provider parse failure | Parallel pipeline fails; other providers may succeed |
| One Search result malformed | `ParallelAdapter.search` | Skip entry | Parallel pipeline continues |
| Valid Search response with no usable hits | `ParallelAdapter.search` | Return `[]` | Pipeline counts as completed |
| Extract matching result lacks full content | `ParallelAdapter.fetch` | Raise provider failure | Scheduler may try another provider |
| Extract matching provider error | `ParallelAdapter.fetch` | Raise provider failure | Scheduler may try another provider |
| Extract no matching result | `ParallelAdapter.fetch` | Raise provider failure | Scheduler may try another provider |
| Full content fails cheap check/judge | `FetchScheduler` | Semantic rejection | Existing fetch fallback/outcome semantics |
| Cancellation | asyncio/orchestration | Re-raise | Existing cancellation behavior |

---

### 8. Security and Diagnostic Safety

- Parallel authentication uses `x-api-key`; the header value comes from existing `SecretValue` and must not be placed in exception strings or explicit log fields.
- Config errors may name `api_key_env`, but never reveal the environment value.
- Do not include `search_queries`, excerpts, extracted `full_content`, raw Parallel response bodies, or raw error objects in newly introduced exception messages.
- Reuse existing `HttpJsonExecutor` endpoint logging, which records sanitized endpoint metadata rather than request bodies.
- Reuse existing `provider_started`, `provider_completed`, `provider_failed`, retry, fallback, and candidate events. No Parallel-specific log sink or raw-response logging is added.

---

### 9. Compatibility Rules

The Parallel addition must not alter error behavior for existing providers.

Specifically:

- No new `ErrorCode` values.
- No changes to retryable HTTP status classification.
- No changes to `HttpJsonExecutor` retry policy.
- No changes to the meaning of `ALL_PROVIDERS_FAILED`, `CONFIG_ERROR`, or `PROTOCOL_ERROR`.
- No changes to whether an empty successful search-provider result counts as a completed pipeline.
- No changes to `FetchScheduler`'s execution-failure versus semantic-failure precedence.
- No changes to cancellation propagation.
- No core config special-casing for Parallel nested policy schema.

The only new failure conditions are those necessary to validate Parallel-specific adapter configuration and Parallel-specific response fields at the existing adapter boundary.
