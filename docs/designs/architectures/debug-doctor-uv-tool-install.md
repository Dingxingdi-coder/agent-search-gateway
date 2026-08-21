## Architecture: Debug Tracing, Doctor, and uv Tool Installation

### 1. Scope & Assumptions

#### In Scope
- Add `agent-search-gateway start --debug` as a daemon-process debug mode.
- Keep ordinary CLI request/response behavior and the Unix-socket protocol unchanged.
- Emit human-readable single-line `key=value` project logs to daemon stderr in debug mode.
- Persist debug-mode project logs to a rotating cache log: 5 MiB current file plus 3 backups.
- Correlate one business CLI invocation with one 8-hex-character `request_id`.
- Reuse the search request's `request_id` as the existing JSONL result filename token.
- Add per-provider, per-stage, retry/scheduler/quota, and per-candidate execution events without logging prompt/page bodies.
- Add a local, deterministic, no-network `agent-search-gateway doctor` command.
- Document end-user installation with `uv tool install .` while retaining `uv sync --locked` for development and CI.

#### Todo
- Raw request/response payload tracing or a separate TRACE mode.
- `doctor --fix`.
- Live provider/API-key connectivity checks.
- A standalone daemon `status` command.
- PyPI or direct Git installation instructions.
- Machine-readable JSON log output or JSON doctor output.

#### Assumptions
- The daemon remains a foreground Unix-socket process.
- `keyword-search`, `llm-search`, and `url-fetch` are the business workflows that receive request IDs; `start`/`stop` remain lifecycle/control operations.
- Target URLs may retain path, query values, and fragments in debug mode, per the chosen diagnostic trade-off, but URI userinfo is stripped before logging.
- HTTP `endpoint` fields strip URI userinfo, query, and fragment so request-specific query/prompt data is not persisted as transport metadata.
- Authentication headers and secret values must never be intentionally logged.
- Normal mode does not create or append the debug log file.
- `doctor` treats "daemon not running" as informational, not a failure.

---

### 2. Architecture Summary

The feature extends the existing foreground-daemon architecture rather than changing the socket protocol. `start --debug` configures a process-wide logging pipeline only for the `agent_search_gateway` logger namespace, with one stderr handler and one `RotatingFileHandler`. A request-scoped `ContextVar` carries the daemon-generated `request_id` across asyncio tasks so every internal log event can be correlated without modifying public provider contracts. Search workflows additionally receive the same ID explicitly when writing JSONL results, replacing the `ResultWriter`'s late private token generation. A new `doctor` CLI path reuses existing config parsing/resolution and performs deterministic filesystem/socket checks without constructing the full runtime or making network calls. README installation is split into end-user `uv tool install .` and developer/CI `uv sync --locked` flows.

---

### 3. Design Decisions

#### Runtime Model

##### Debug Is a Daemon Startup Mode

- Description: Add `--debug` only to `agent-search-gateway start`. It configures observability before daemon runtime construction and remains active for the lifetime of that foreground process.
- Rationale: Provider orchestration occurs in the daemon, so daemon-local debugging exposes the real intermediate work without expanding the socket protocol.
- Trade-offs: A CLI request issued from another terminal does not stream progress locally; the user watches the daemon terminal or reads `debug.log`.
- Rejected Alternatives:
  - Per-request `--debug` on search/fetch commands:
    - Description: Send a debug flag through the socket and stream events back.
    - Why Rejected: Requires protocol changes, multiplexed event framing, and more concurrency complexity for little benefit in v0.1.
  - Environment-variable-only debug mode:
    - Description: Enable debug through an environment variable.
    - Why Rejected: Less discoverable than the explicit startup flag selected for this version.

##### Doctor Is a Separate Static Diagnostic Command

- Description: Add `agent-search-gateway doctor` as a local, read-only diagnostic command with no external API requests and no automatic repairs.
- Rationale: Configuration/environment/filesystem/socket health is a different question from runtime execution tracing and should not be dumped on every debug startup.
- Trade-offs: `doctor` cannot prove that credentials are accepted by remote providers or that the network is reachable.
- Rejected Alternatives:
  - Print a full config health summary on every `start --debug`:
    - Description: Mix startup diagnostics into runtime tracing.
    - Why Rejected: Repetitive, noisy, and conflates static health checks with execution events.
  - Live provider checks in doctor:
    - Description: Contact configured providers.
    - Why Rejected: Adds side effects, latency, credential usage, and nondeterministic failures.

#### Interface / Protocol

##### Preserve the Socket Protocol

- Description: Do not add request IDs, debug flags, or progress events to the NDJSON request/response protocol.
- Rationale: Debugging is an implementation/observability concern, not a public protocol concern for this version.
- Trade-offs: The request ID is visible in daemon logs and search filenames but not returned as a separate response field.
- Rejected Alternatives:
  - Add `request_id` to protocol envelopes:
    - Description: Return correlation metadata to every client.
    - Why Rejected: Unnecessary compatibility surface because search commands already return a filename containing the ID.

##### Doctor Uses Human-Readable Status Lines and Meaningful Exit Codes

- Description: Doctor emits `[ok]`, `[info]`, and `[fail]` lines. It returns `1` iff at least one `[fail]` exists; otherwise `0`.
- Rationale: Humans can read it directly and scripts can use the exit status.
- Trade-offs: No machine-readable structured output in this version.
- Rejected Alternatives:
  - Always exit 0:
    - Why Rejected: Makes automation unable to distinguish healthy from unhealthy environments.
  - Treat daemon-not-running as failure:
    - Why Rejected: A stopped foreground daemon is a valid state, especially immediately after installation.

#### State Management

##### One Request ID Per Business Workflow

- Description: On daemon dispatch of `keyword-search`, `llm-search`, or `url-fetch`, reserve an eight-hex ID through `RequestIdRegistry`. The registry rejects active IDs and, for search workflows, IDs colliding with existing `keyword-<id>.jsonl` or `llm-<id>.jsonl` files before the workflow is logged. The accepted ID is then bound to request-scoped logging context until that workflow completes.
- Rationale: Eight hex characters preserve the current filename token shape while giving concurrent logs a stable correlation key; reservation preserves the one-request/one-result-file invariant even under rare collisions.
- Trade-offs: 32 bits is not intended as a globally unique identifier across machines or permanent archives; bounded regeneration plus exclusive result-file creation provides local collision defense.
- Rejected Alternatives:
  - UUIDs:
    - Why Rejected: Longer and less readable for a local bounded log/result namespace.
  - Independent log ID and result-file token:
    - Why Rejected: Creates avoidable two-ID correlation work.

##### Use ContextVar for Log Correlation, Explicit ID for Result Naming

- Description: A `ContextVar` stores the current request ID for log formatting/filtering across awaited calls and asyncio tasks. Search orchestration passes the same ID explicitly to `ResultWriter.write_results(..., request_id=...)` because the filename is functional output, not merely logging metadata.
- Rationale: Context propagation avoids threading a diagnostic argument through every provider/stage contract, while explicit result naming avoids hiding a functional dependency inside ambient context.
- Trade-offs: The observability module gains async-context semantics that must be tested for concurrent isolation.
- Rejected Alternatives:
  - Pass request ID through every orchestrator/provider/LLM method:
    - Why Rejected: Pollutes domain/provider interfaces with an operational concern.
  - Let `ResultWriter` read the ContextVar:
    - Why Rejected: Makes filename behavior depend on hidden ambient state.

#### Storage / Persistence

##### Rotating Debug Log in Cache

- Description: Add a runtime log path under `~/.cache/agent-search-gateway-cli/logs/debug.log`. In debug mode, append project logs at DEBUG and above, rotate at 5 MiB, and retain 3 backups.
- Rationale: Preserves pre-restart evidence while bounding disk use to roughly 20 MiB.
- Trade-offs: Older evidence is eventually discarded by rotation.
- Rejected Alternatives:
  - Truncate on every startup:
    - Why Rejected: Destroys the previous run, which is often the most useful evidence after a crash/restart.
  - One file per daemon session:
    - Why Rejected: Requires separate retention/cleanup policy for a small local daemon.

##### Session Boundary Markers

- Description: Every successful debug daemon startup logs `session_started` with PID/debug state; orderly shutdown logs `session_stopped`.
- Rationale: A single rotating file may contain multiple daemon lifetimes, so explicit boundaries are necessary for interpretation.
- Trade-offs: Abrupt process death may omit `session_stopped`, which itself is useful evidence.

##### Search Result Filename Reuses Request ID

- Description: Replace late `ResultWriter` token generation with `{kind}-{request_id}.jsonl`; retain exclusive-create semantics to guard against rare collisions.
- Rationale: The filename becomes a direct join key between CLI output and debug logs without changing the visible filename pattern.
- Trade-offs: Request ID generation must happen before the search pipeline rather than at final file write.

#### Provider Integration

##### Record Operational Metadata, Not Payload Bodies

- Description: Provider/LLM debug events may include provider name, stage, model, sanitized endpoint, target URL, attempt, HTTP status, elapsed time, counts, character lengths, `extra_body` keys, parse/decision booleans or fixed outcome codes, and failure category. Do not log query/prompt/page/candidate/LLM-response bodies or free-form LLM decision reasons.
- Rationale: These fields explain execution behavior while avoiding huge logs and unnecessary persistence of user/web content.
- Trade-offs: Prompt-construction bugs cannot be diagnosed from logs alone; prompt builders remain testable directly.
- Rejected Alternatives:
  - Full request/response dumps:
    - Why Rejected: High noise and data-exposure cost disproportionate to normal debugging needs.

##### Keep Authentication Secrets Out of Events

- Description: Logging call sites never include Authorization/API-key header values. Existing `SecretValue` redaction remains defense-in-depth for any secret that reaches a log message accidentally.
- Rationale: Debug persistence must not turn credentials into cache files.
- Trade-offs: Authentication-header shape is visible only as non-secret metadata if explicitly useful.

#### Concurrency / Scheduling

##### Emit Scheduler, Quota, Retry, and Candidate Decisions

- Description: Add debug events around provider selection/waiting, retry attempts/statuses, provider completion/failure, and every search/fetch candidate acceptance/rejection.
- Rationale: These are the hidden decisions that currently make workflows appear opaque.
- Trade-offs: A large search result set produces proportionally more log lines.
- Rejected Alternatives:
  - Provider-level summaries only:
    - Why Rejected: Cannot explain why an individual URL disappeared between provider output and final result.

##### Keep Third-Party HTTP Debug Logs Disabled

- Description: `--debug` sets DEBUG only on the project's logger namespace; `httpx`/`httpcore` remain at their normal levels.
- Rationale: The shared `HttpJsonExecutor` is a better semantic boundary and avoids low-level socket/TLS noise.
- Trade-offs: Extremely low-level HTTP transport debugging still requires separate manual configuration outside this feature.

#### Security

##### Target URL Query/Fragment Diagnostics Are Allowed, but Userinfo Is Not

- Description: Log target URL path, query, and fragment when relevant to provider/candidate/fetch events, after stripping URI userinfo. HTTP transport `endpoint` fields additionally strip query and fragment so dynamic request parameters such as search queries are not persisted as endpoint metadata.
- Rationale: The chosen debugging policy favors target-URL reproducibility while treating embedded authentication and transport request parameters as a separate secret/payload boundary.
- Trade-offs: Signed/session-bearing target URL query values can still be persisted in `debug.log`; users must treat debug logs as potentially sensitive local artifacts.

##### Debug File Creation Is Fail-Closed

- Description: If `start --debug` cannot create/open the log directory/file, startup fails explicitly instead of silently falling back to stderr-only mode.
- Rationale: A user who explicitly requested persistent debug evidence should not be misled into believing it is being captured.
- Trade-offs: A logging-path permission issue prevents daemon startup in debug mode even if normal operation would otherwise work.

#### Observability

##### Stable Human-Readable key=value Events

- Description: Use a consistent single-line formatter and stable event names/fields, e.g. `DEBUG request=a1b2c3d4 provider=tavily stage=search event=completed hits=10 elapsed_ms=526`.
- Rationale: Human-readable terminal output remains easy to scan while structured fields are straightforward to assert in tests and grep later.
- Trade-offs: Parsing is less robust than JSON if future log ingestion becomes a requirement.

##### Expected Failures Do Not Emit Tracebacks

- Description: Timeouts, 429/5xx retries, provider execution failures, semantic rejection, and invalid provider data emit concise structured lines. Unexpected internal exceptions use `logger.exception(...)` in debug mode so the traceback is retained.
- Rationale: Expected fallback behavior should not drown useful traces in stack dumps.
- Trade-offs: Some expected failure classes provide less stack-level detail by design.

#### Future Migration

##### Keep Diagnostics Additive to Existing Contracts

- Description: Treat `--debug`, `doctor`, logging fields, and result-ID ownership as CLI/runtime additions without changing public result JSONL schema or socket envelopes.
- Rationale: Preserves the v0.1 protocol/config migration boundaries and keeps a future daemon rewrite possible.
- Trade-offs: Some diagnostic metadata remains local-only rather than available through remote/client protocol abstractions.

##### Separate User Installation From Development Environment

- Description: README user installation becomes `uv tool install .`; development/verification and CI continue to use `uv sync --locked` and `uv run ...`.
- Rationale: Tool installation creates an isolated CLI runtime without dev dependencies, while developers still need the locked test/lint/type-check environment.
- Trade-offs: Documentation must explain two uv workflows instead of one command for every audience.

---

### 4. Component Catalog

| Component | Purpose | Key Responsibilities | Public Interfaces | Dependencies | Owns State? | Data-Flow Role |
|---|---|---|---|---|---|---|
| CLI Parser | Expose startup/debug/doctor commands | Parse `start --debug`, `doctor`, existing business commands | `agent-search-gateway ...` | argparse | No | Source / renderer |
| Observability Configuration | Configure project logging | Set project namespace level, stderr handler, rotating file handler, formatter, secret filters | internal `configure_debug_logging(...)` / teardown helper | logging, `RotatingFileHandler`, RuntimePaths | Yes, process logging handlers | Boundary / sink configuration |
| Request ID Registry | Reserve collision-safe request IDs | Validate generated IDs, reject active/result-file collisions, reserve and release accepted IDs | internal `RequestIdRegistry.reserve(...)` | `secrets`, RuntimePaths/results | Yes, active request IDs | Operational state guard |
| Request Context | Correlate concurrent workflow logs | Bind/reset an already-reserved request ID and expose it to log records | internal context manager/accessor | `contextvars` | Yes, task-local context | Metadata carrier |
| Foreground Daemon | Own request lifecycle | Reserve request ID, bind request context, dispatch workflows, log session/workflow lifecycle, release reservation | existing daemon request handler | request ID registry, observability, runtime | Yes, daemon lifecycle | Coordinator |
| Search Orchestrator | Run search pipelines | Provider fan-out, candidate decisions, result aggregation, pass request ID to writer | internal `keyword_search`, `llm_search` | quotas, stages, store, writer | No | Coordinator |
| Fetch Orchestrator | Run admitted URL workflow | Singleflight/locking, fetch preparation, safety/focus stages, decision logging | internal `url_fetch` | store, scheduler, stages | No | Coordinator |
| Fetch Scheduler | Choose fetch providers | Capacity-aware selection, fallback, semantic/execution outcomes | `fetch_until_accepted` | quotas, stages, providers | No | Scheduler |
| HttpJsonExecutor | Shared HTTP execution boundary | Attempt/status/elapsed/retry logging and JSON/error mapping | `request_json` | httpx, retry | Owns HTTP client | Adapter boundary |
| LLMStages | Name semantic LLM operations | Emit stage start/completion/decision metadata without payload bodies | judge/safety/content-clean/focus-summary/llm-search | LLM clients | No | Transformer / validator |
| ResultWriter | Persist search results | Serialize compact JSONL and use caller-provided request ID in filename | `write_results(kind, records, request_id)` | filesystem | No | Sink |
| Doctor Runner | Diagnose local health | Config existence/parse/resolve, required env resolution, cache/results/log writability, socket state | internal `run_doctor(...)` | config, registry, RuntimePaths, Unix socket | No | Validator / renderer input |
| RuntimePaths | Centralize filesystem locations | Add log directory/debug log path alongside config/socket/results | `RuntimePaths` | pathlib | No | Configuration value |
| README / CI Split | Document installation modes | User `uv tool install .`; developer/CI `uv sync --locked` | documentation/workflow | uv | No | Distribution guidance |

---

### 5. Data Flow

#### 5.1 `agent-search-gateway start --debug`

```text
CLI Parser:
  parse start --debug

Observability Configuration:
  create logs directory
  open rotating debug.log handler (5 MiB, 3 backups, append)
  if file handler setup fails:
    raise startup/config failure
  configure only agent_search_gateway logger namespace at DEBUG
  attach stderr + rotating-file handlers

Foreground Daemon:
  log session_started pid=<pid> debug=true
  prepare socket
  resolve config and build runtime
  start Unix server
  wait until shutdown
  on orderly shutdown:
    log session_stopped pid=<pid>
    close handlers/runtime/socket
  on unexpected internal exception:
    emit traceback in debug log/stderr
    propagate/map through existing startup behavior
```

#### 5.2 `keyword-search` / `llm-search` while debug daemon is running

```text
CLI:
  construct existing request envelope
  send over existing Unix socket

Foreground Daemon:
  decode request
  reserve request_id through RequestIdRegistry
    reject active IDs
    for search, reject IDs colliding with existing keyword-/llm- result files
  bind RequestContext(request_id) only after reservation succeeds
  log workflow started

  try:
    Search Orchestrator:
      start configured provider pipelines concurrently
      for each provider/invocation:
        log provider/stage start
        acquire/wait quota -> log relevant wait/acquire events
        HttpJsonExecutor/LLMStages -> log attempts/status/duration/outcome metadata

        for each candidate/hit:
          log URL + candidate source + char counts
          perform validation/cheap-check/judge
          if rejected:
            log rejected reason=<reason>
          else:
            log accepted

      aggregate/deduplicate/admit URLs
      ResultWriter:
        target = <kind>-<request_id>.jsonl
        exclusive-create and write records
      log results_written path=<path> results=<count>
      return absolute path

    daemon logs workflow completed elapsed_ms=...
    return existing SuccessResponse(path)
  catch expected GatewayError:
    log concise failure event
    return existing ErrorResponse
  catch unexpected Exception:
    log traceback with request context
    return existing internal protocol error
  finally:
    reset RequestContext
    release RequestIdRegistry reservation

CLI:
  print absolute path exactly as before
```

#### 5.3 `url-fetch` while debug daemon is running

```text
CLI -> existing URLFetchRequest -> daemon

Daemon:
  reserve request_id through RequestIdRegistry (active-ID collision check)
  bind request context after reservation
  log workflow start

Fetch Orchestrator:
  normalize URL/focus
  singleflight + per-URL lock
  inspect URL store -> log cached/admitted state metadata

  if body fetch required:
    Fetch Scheduler:
      select capacity-available provider -> log selected/wait
      provider fetch -> HTTP attempt metadata
      validate candidate
      cheap-check + judge -> log candidate accepted/rejected
      on execution failure -> log fallback reason and try next provider

  content-clean if needed -> log LLM stage metadata
  safety -> log ok/rejected + reason
  optional focus-summary -> log stage metadata
  return final text or unavailable message

Daemon:
  log workflow completed/failure
  return existing response envelope
  reset request context and release RequestIdRegistry reservation in cleanup
```

#### 5.4 `agent-search-gateway doctor`

```text
CLI Parser:
  parse doctor

Doctor Runner:
  checks = []

  check config_file exists
  if absent:
    add [fail]
  else:
    load TOML
    build default provider registry
    resolve config against current environment
    if ConfigFailure:
      add [fail] with safe diagnostic
    else:
      add [ok] config/provider/env checks

  for cache/results/log locations:
    verify required parent can be created/accessed and is writable
    do not create debug.log itself as an ongoing log artifact
    add [ok]/[fail]

  inspect socket path:
    if absent:
      add [info] daemon not running
    elif live Unix socket accepts local connection/probe:
      add [ok] daemon running
    elif stale/broken socket:
      add [fail]
    elif path exists but is not a socket:
      add [fail]

CLI:
  render all checks
  if any fail:
    exit 1
  else:
    exit 0
```

#### 5.5 Installation / Development

```text
End user:
  clone repository
  cd repository
  uv tool install .
  agent-search-gateway doctor
  agent-search-gateway start [--debug]

Developer / CI:
  uv sync --locked
  uv run ruff check .
  uv run mypy src tests
  uv run pytest -v
```

---

### 6. Interfaces & Contracts

#### Debug Event Contract (Internal, Migration-Stable Only Within This Python Implementation)

```text
<LEVEL> request=<8hex-or-> component/stage fields... event=<name> [elapsed_ms=<int>]
```

Required conventions:
- One physical line per event.
- Business-workflow events include `request=<8hex>`.
- Daemon lifecycle events use no business request ID.
- Provider events include `provider=<name>`.
- LLM events include semantic `stage=<judge|safety|content_clean|focus_summary|llm_search>` and may include `model=<name>`.
- Candidate events include `url=<target URL path/query/fragment with userinfo stripped>` and `event=<accepted|rejected>` plus a stable fixed `reason` when rejected.
- No prompt/page/candidate/LLM-response body fields.
- No authentication secret values.

#### Request ID Contract (Internal Operational Contract)

```text
request_id := 8 lowercase hexadecimal characters generated by secrets.token_hex(4)
```

For successful search commands:

```text
result filename := keyword-<request_id>.jsonl | llm-<request_id>.jsonl
```

The JSONL record schema remains unchanged:

```json
{"url":"https://example.com/article","abstract":"Short search abstract"}
```

#### Doctor Check Contract (CLI Contract)

```text
[ok] <check description>
[info] <non-failing state description>
[fail] <actionable failure description>
```

Exit status:
- `0`: no `[fail]` checks.
- `1`: one or more `[fail]` checks.

`daemon not running` is `[info]`, not `[fail]`.

#### Installation Contract (Documentation/Distribution Contract)

End-user install from the current source checkout:

```bash
uv tool install .
```

Development and CI remain lockfile-driven:

```bash
uv sync --locked
```
