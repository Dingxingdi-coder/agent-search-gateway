## Error Handling: Debug Tracing, Doctor, and uv Tool Installation

### 1. Error-Handling Principles

This feature preserves the existing gateway error taxonomy and adds diagnostic behavior around it rather than redefining business failures.

Primary rules:

- Existing `GatewayError` subclasses and public socket error responses keep their current meaning.
- `--debug` may add diagnostic evidence, but it must not change whether a provider failure, semantic rejection, or user input is considered successful or failed.
- Debug logging is fail-closed only during debug bootstrap. After the daemon has successfully started, a later log-file write/rotation failure must not fail an otherwise valid search/fetch workflow.
- Expected failures produce concise single-line events without tracebacks.
- Unexpected internal exceptions produce tracebacks only when daemon debug mode is enabled; normal mode keeps the current concise internal-error behavior.
- `doctor` aggregates independent local checks and reports all findings it can obtain rather than stopping at the first failure.
- `doctor` does not introduce new socket-protocol `ErrorCode` values because it is a local CLI diagnostic path, not a daemon request.
- Authentication secrets must not appear in terminal output, `debug.log`, doctor output, or tracebacks intentionally emitted by this feature.

---

### 2. Debug Bootstrap Failures

#### Debug Log Directory Creation Fails

Condition:

- `agent-search-gateway start --debug` cannot create or access `~/.cache/agent-search-gateway-cli/logs/`.

Handling:

- Treat as daemon startup/configuration failure.
- Raise/map to `ConfigFailure(ErrorCode.CONFIG_ERROR, ...)` with the affected path and a concise OS-level reason where safe.
- Print the message to CLI stderr through the existing `start` error path.
- Exit non-zero.
- Do not start the runtime or bind the daemon socket.

Rationale:

- The user explicitly requested persistent debug evidence; silently degrading to terminal-only logs would be misleading.

#### Debug Log File Cannot Be Opened

Condition:

- `debug.log` cannot be opened in append mode, including permission, file-type, or filesystem errors.

Handling:

- Same as debug log directory failure: fail startup with `CONFIG_ERROR`.
- If one logging handler was already installed before another handler fails, remove and close all handlers installed by the failed configuration attempt before returning the error.

#### Rotating Handler Configuration Fails

Condition:

- Construction of the 5 MiB / 3-backup rotating file handler fails for any reason.

Handling:

- Fail debug startup.
- Do not leave partially configured project logger state behind.

#### Normal Startup Is Unaffected

Condition:

- `agent-search-gateway start` without `--debug`.

Handling:

- Do not create/open `debug.log`.
- Preserve existing warning/error behavior.
- A broken or unwritable debug-log path must not prevent normal startup.

---

### 3. Runtime Logging Failures

#### File Write or Rotation Fails After Successful Startup

Condition:

- The rotating file handler was opened successfully at startup but a later write/flush/rotation fails, for example because the filesystem becomes read-only or disk space is exhausted.

Handling:

- Do not convert the current business workflow into a gateway failure.
- Do not recurse through the normal logger to report a logger failure.
- Emit a concise best-effort emergency diagnostic directly to the original process stderr (`sys.__stderr__` or equivalent handler-safe path).
- Continue allowing the stderr logging handler to emit subsequent project events when possible.

Rationale:

- Fail-closed bootstrap guarantees debug persistence was initially available; after startup, observability infrastructure must not become a new source of search/fetch failures.

#### Session Stop Marker Is Missing

Condition:

- Process is killed, crashes below the Python logging layer, or otherwise cannot execute orderly shutdown.

Handling:

- No recovery action is required.
- Absence of `session_stopped` after a prior `session_started` is valid diagnostic evidence of an abnormal process end.

---

### 4. Request-ID Failure Rules

#### Request ID Generation

For each `keyword-search`, `llm-search`, and `url-fetch` request:

```text
request_id = secrets.token_hex(4)
```

The daemon must generate the ID before logging the workflow-start event.

#### Collision Avoidance

Because the ID is now functional correlation state rather than a private `ResultWriter` token:

- The daemon/request-ID helper must reject an ID already active in the current daemon.
- For search workflows, it must also reject an ID whose `keyword-<id>.jsonl` or `llm-<id>.jsonl` result path already exists in the cache.
- Generate another token until an available ID is found.
- `ResultWriter` still uses exclusive creation as a final defensive check.

If exclusive creation nevertheless reports `FileExistsError` after the ID was accepted, treat it as an unexpected internal/filesystem race. Do not silently choose a second filename token because that would break the one-request/one-ID invariant already present in logs.

#### Request Context Cleanup

The request-scoped `ContextVar` token must be reset in `finally` for:

- success;
- `GatewayError`;
- unexpected exception;
- task cancellation.

A cancelled or failed request must never leak its request ID into a later connection or workflow task.

---

### 5. Existing Business Failures Under Debug Mode

Debug mode does not alter the existing user-visible error model.

| Failure | Existing business behavior | Debug behavior |
|---|---|---|
| Empty query / invalid URL / URL not admitted | Existing input error | One concise request failure event; no traceback |
| Provider timeout / transport failure | Retry/fallback according to current rules | Attempt/failure/retry metadata; no traceback |
| HTTP 408/429/5xx | Retry/fallback according to current rules | Status + attempt + retry metadata; no traceback |
| Provider malformed data | Fail that provider pipeline/attempt | Provider failure event with category; no traceback |
| LLM protocol/parse failure | Retry or fail relevant stage/pipeline | Stage failure event; no raw response; no traceback |
| `cheap_check` rejection | Existing semantic behavior | Candidate rejected event with reason |
| judge `ok=false` | Existing semantic behavior | Candidate rejected event with fixed `judge_rejected` reason code; free-form model reason is not logged |
| safety `ok=false` | Mark unavailable according to existing rules | Safety rejected event with fixed `safety_rejected` reason code; free-form model reason is not logged |
| All providers fail | Existing `ALL_PROVIDERS_FAILED` response | Request failure summary after provider events |
| Daemon shutting down | Existing `DAEMON_SHUTTING_DOWN` response | Concise rejection event |

#### Expected `GatewayError`

When daemon dispatch catches a `GatewayError`:

- Log a single request-scoped failure summary in debug mode.
- Include stable fields such as `request`, `command`, `error_code`, and safe failure category/message.
- Return the same `ErrorResponse` code/message as normal mode.
- Do not log a traceback.

#### Cancellation

When a business workflow receives `asyncio.CancelledError`:

- Emit a debug-only `event=cancelled` line when practical.
- Re-raise cancellation.
- Do not map cancellation into `ALL_PROVIDERS_FAILED` or a semantic rejection.
- Always reset request context.

---

### 6. Unexpected Internal Exceptions

#### Debug Mode Enabled

For exceptions outside the expected `GatewayError`/cancellation paths:

- Use traceback-bearing logging (`logger.exception` or equivalent) with the active `request_id` when one exists.
- The first line must still contain structured context, e.g. command/component/event/error type.
- Do not include request/prompt/page bodies as explicit log fields.
- Return the existing generic daemon response: `protocol_error` / `Internal daemon error`.

#### Debug Mode Disabled

- Preserve the current concise error behavior: log only a short internal failure/type message.
- Do not newly expose tracebacks to ordinary foreground users.
- Return the same generic internal daemon response.

This preserves the design choice that traceback verbosity is diagnostic-mode behavior rather than part of normal CLI output.

---

### 7. Secret-Safety Failures

#### Authentication Headers

Logging call sites must not pass values from these or equivalent authentication fields into log messages:

- `Authorization`
- `x-api-key`
- `X-API-Key`
- `X-Subscription-Token`
- any configured provider secret

Target URL path/query/fragment values are intentionally allowed by the selected debug policy, but URI userinfo must be stripped before logging. HTTP transport `endpoint` fields must strip userinfo, query, and fragment so dynamic request parameters are not persisted as endpoint metadata.

#### Mandatory Final-Stage Redaction

After config resolution succeeds, every resolved non-empty `SecretValue` must be registered with the centralized redactor used by both debug handlers.

Rules:

- Redaction is a backstop, not permission for call sites to log headers.
- Both stderr and rotating-file handlers must redact the fully rendered log line, including any formatted traceback, before it reaches the sink.
- Debug traceback emission is permitted only through a handler/formatter path that guarantees this final-stage redaction. If that guarantee is unavailable, suppress the traceback rather than emit an unredacted one.
- If config resolution itself fails before secrets are resolved, the user-facing `ConfigFailure` message may contain environment-variable names but never secret values.

#### Traceback Safety

Unexpected tracebacks may contain exception messages from libraries. Therefore:

- Central secret redaction must run after message and traceback rendering for every debug sink.
- Regression coverage must include a secret present only in exception text and prove it is absent from both debug stderr and rotating-file output.
- Provider code should continue wrapping transport/protocol failures in gateway errors that do not embed raw headers/request bodies.
- Debug code must not attach full HTTP request objects to exception/log fields.

---

### 8. Doctor Error Model

Doctor returns a collection of independent checks:

```text
status := ok | info | fail
message := concise human-readable diagnostic
```

Doctor should continue to later independent checks after an earlier check fails whenever doing so is safe.

#### Config File Missing

```text
[fail] config file not found: <path>
```

- Exit result becomes failing.
- Continue filesystem and socket diagnostics.

#### TOML Parse Failure

```text
[fail] config parse failed: <safe reason>
```

- Do not attempt config resolution.
- Continue filesystem and socket diagnostics.

#### Config Resolution Failure

Examples:

- unknown enabled provider;
- unsupported provider capability;
- invalid concurrency value;
- referenced LLM provider missing;
- required `api_key_env` missing;
- referenced environment variable unset.

Handling:

```text
[fail] configuration invalid: <ConfigFailure.message>
```

The existing `resolve_config`/provider registry remains the source of truth. Doctor must not duplicate these validation rules.

#### Environment Variables

If full config resolution succeeds, doctor may report required environment variables as present using names only:

```text
[ok] environment variable TAVILY_API_KEY is set
```

Never print the value or a prefix/suffix of the value.

---

### 9. Doctor Filesystem Checks

Doctor is not a repair command. It must not leave configuration, result, log, or socket artifacts behind merely to make a check pass.

#### Existing Directory

To verify writability reliably:

- Create a uniquely named temporary probe file in the directory.
- Close and remove it immediately.
- On success emit `[ok]`.
- On `PermissionError`, `OSError`, or cleanup failure emit `[fail]` with path and safe reason.

This transient probe is diagnostic I/O, not a persistent repair.

#### Directory Does Not Yet Exist

- Do not permanently create the target directory as a doctor side effect.
- Walk to the nearest existing parent and validate that the process could create the missing path (permissions/type checks).
- Report success as the target path being creatable, or failure with the blocking parent/reason.

#### Wrong File Type

If a path expected to be a directory exists as a regular file/symlink target incompatible with the runtime requirement:

```text
[fail] expected directory but found non-directory: <path>
```

Doctor does not rename/remove it.

---

### 10. Doctor Daemon/Socket Checks

#### Socket Missing

```text
[info] daemon not running
```

- Not a failure.
- Overall exit code can remain `0`.

#### Live Daemon Socket

Condition:

- Path exists as a Unix socket and a bounded local connection probe succeeds.

Output:

```text
[ok] daemon running
```

- Close the probe connection cleanly without sending a business request.

#### Stale/Refused Socket

Condition:

- Path exists as a Unix socket but connection is refused.

Output:

```text
[fail] daemon socket is stale or refusing connections: <path>
```

- Do not unlink it; `doctor` has no `--fix` behavior.

#### Socket Probe Timeout

```text
[fail] daemon socket did not respond in time: <path>
```

Use the same bounded local-probe philosophy as daemon startup; doctor must not hang indefinitely.

#### Path Exists but Is Not a Unix Socket

```text
[fail] daemon socket path is not a Unix socket: <path>
```

No mutation.

#### Permission / Other OS Error

```text
[fail] unable to inspect daemon socket: <safe reason>
```

Continue rendering any already-computed checks.

---

### 11. Doctor Unexpected Failure

If doctor itself encounters an implementation bug outside its expected config/filesystem/socket error handling:

- Print one concise `[fail] doctor internal error` message to stderr or as a final failing check.
- Exit `1`.
- Do not print a traceback by default because `doctor` has no debug flag in this version.
- Do not silently return success with incomplete checks.

---

### 12. Doctor Exit Codes

```text
0 -> all checks are [ok] or [info]
1 -> at least one [fail], including an internal doctor failure
```

Examples:

```text
[ok] configuration valid
[ok] cache path writable
[info] daemon not running
=> exit 0
```

```text
[fail] environment variable OPENAI_API_KEY is required
[ok] cache path writable
[info] daemon not running
=> exit 1
```

---

### 13. Result-File Failures

The public search success contract remains "stdout contains an absolute JSONL path".

#### Serialization Failure

If a supposedly valid internal `SearchRecord` cannot be serialized:

- Treat as an unexpected internal invariant failure under the current error taxonomy.
- In debug mode emit traceback/context.
- Do not create a partial success response.

#### File Open/Write Failure

If result-file exclusive creation or write fails:

- Do not return a path to an incomplete/failed file.
- Ensure the file handle is closed.
- Best-effort remove a partially written newly created target when safe.
- Preserve current generic internal-error public behavior unless a separate result-storage error code is designed in a future feature.
- In debug mode record the request ID, target path, OS error type, and traceback for unexpected filesystem failures.

This feature deliberately does not add a new public error code solely for result storage.

---

### 14. Installation / uv Failures

`uv tool install .` is executed by `uv`, before the gateway CLI is available. Resolver/build/install failures are therefore owned and rendered by `uv`; the application does not wrap them.

Documentation rules:

- End-user installation section uses `uv tool install .`.
- Do not imply that `uv tool install .` consumes the project `uv.lock` as the runtime environment.
- Development/CI verification continues to document `uv sync --locked` and `uv run ...`.
- `doctor` is the first application-level health check after a successful tool install and configuration setup.

No automatic uninstall/reinstall/upgrade behavior is added in this version.

---

### 15. Logging Severity Guidance

Use severity consistently so `debug.log` forms a coherent timeline:

| Event | Level |
|---|---|
| debug session start/stop | INFO |
| request start/completion | DEBUG |
| provider/stage start/completion | DEBUG |
| candidate accepted/rejected | DEBUG |
| scheduler/quota wait/selection | DEBUG |
| retryable HTTP status / transport retry | WARNING (preserve current behavior) |
| expected provider/stage terminal failure | DEBUG or WARNING according to existing semantics |
| daemon cleanup problem | ERROR |
| unexpected internal workflow exception | ERROR + traceback in debug mode |
| logging sink emergency degradation | direct stderr emergency line, not recursive logging |

Normal mode should not gain the high-volume DEBUG events.
