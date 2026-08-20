## Testing: Debug Tracing, Doctor, and uv Tool Installation

### 1. Test Strategy

This feature should be verified almost entirely without network access. The main risks are cross-cutting behavior: logging configuration, request-context isolation under asyncio concurrency, deterministic doctor status/exit semantics, and preservation of existing CLI/socket/result contracts.

Primary goals:

- Prove `start --debug` adds diagnostics without changing business-command stdout, socket envelopes, or workflow outcomes.
- Prove one business workflow owns one request ID from daemon dispatch through logs and, for searches, through the JSONL filename.
- Prove concurrent workflows never leak or mix request IDs.
- Prove debug logs contain operational metadata while excluding prompt/page/LLM-response bodies and authentication secrets.
- Prove rotating logs are bounded and survive daemon restarts.
- Prove debug logging bootstrap failures are fatal only for `start --debug`, while post-start logging failures do not fail business workflows.
- Prove `doctor` is local-only, aggregates independent checks, has no persistent repair side effects, and follows the agreed `[ok]` / `[info]` / `[fail]` plus exit-code contract.
- Prove user installation documentation uses `uv tool install .`, while development and CI remain lockfile-driven through `uv sync --locked`.

No new mandatory live-provider test is required.

---

### 2. Test Layers

| Layer | Purpose | Real network? | Suggested location |
|---|---|---:|---|
| Unit | Request context, formatter/filter, runtime paths, ResultWriter naming | No | `tests/unit/` |
| CLI | Parser flags, doctor rendering/exit codes, unchanged stdout/stderr | No | `tests/cli/` |
| Daemon | Request-ID lifecycle, debug startup/shutdown, traceback policy | No | `tests/daemon/` |
| Observability | Handler setup, rotation, redaction, sink failure behavior | No | new focused observability tests |
| Orchestrator | Per-provider/stage/candidate events and result-ID propagation | No | existing `tests/orchestrators/` |
| Scheduler/runtime | Quota/fallback/stage logs and context propagation | No | existing scheduler/runtime tests |
| Doctor | Config/filesystem/socket diagnostics | Local Unix socket only | new `tests/doctor/` |
| Documentation/package | Install/development instructions and script metadata | No | existing docs/package tests |
| Acceptance | End-to-end local debug workflow | Local Unix socket only | existing `tests/acceptance/` |

All filesystem tests use temporary homes/runtime paths. They must not touch the developer's actual home directories.

---

### 3. CLI Parser and Command Tests

#### `start --debug`

Cover:

- `start` parses with debug disabled.
- `start --debug` parses with debug enabled.
- `--debug` is rejected on `keyword-search`, `llm-search`, `url-fetch`, `stop`, and `doctor` in this version.
- Existing business-command positional arguments remain unchanged.
- Help exposes the `start --debug` option and the new `doctor` command.

#### `doctor`

Cover:

- `doctor` requires no daemon.
- Results containing only `ok` and `info` return exit `0`.
- Any `fail` returns exit `1`.
- Doctor does not route through the daemon request/response protocol.

#### Existing Output Contract Regression

Cover:

- Successful search stdout remains only the absolute JSONL path.
- Successful fetch stdout remains only content, summary, or unavailable text.
- Debug events never appear on business-command stdout.
- A CLI process issuing a request from another terminal does not need to know whether the daemon is in debug mode.

---

### 4. RuntimePaths Tests

Extend path tests for the logging location. For a temporary home, assert the existing config/socket/results paths plus:

```text
<home>/.cache/agent-search-gateway-cli/logs/debug.log
```

If implementation exposes a separate `logs_dir`, assert it as well. Test the chosen public RuntimePaths shape rather than requiring redundant properties.

---

### 5. Request Context and Request-ID Tests

#### Token Shape

Cover:

- Generated IDs are exactly 8 lowercase hexadecimal characters.
- IDs are generated for `keyword-search`, `llm-search`, and `url-fetch`.
- Lifecycle/control commands do not allocate business request IDs.

#### Deterministic Injection

Make request-ID generation replaceable in tests, through an injected callable or a small monkeypatchable helper. Use fixed values such as `11111111` and `22222222` for assertions.

#### Context Binding and Reset

Cover context reset after:

- success;
- expected `GatewayError`;
- unexpected exception;
- `asyncio.CancelledError`.

A later request in the same event loop must never inherit a previous ID.

#### Concurrent Isolation

Use two controlled concurrent workflows:

```text
request A -> bind 11111111 -> block inside provider
request B -> bind 22222222 -> block inside provider
release their internal events in interleaved order
```

Assert every A event contains only A's ID and every B event contains only B's ID. Child tasks created through `asyncio.gather` must inherit the correct request context even when completion order differs from start order.

This is a critical concurrency regression test.

#### Collision Handling

With a deterministic factory that repeats values, cover:

- active-request collision regenerates before workflow start;
- search ID colliding with an existing matching result file regenerates;
- once an ID has been accepted and logged, ResultWriter never silently switches to another ID;
- a final exclusive-create race is surfaced as an internal failure rather than breaking the one-request/one-ID invariant.

---

### 6. ResultWriter Tests

Update existing ResultWriter coverage:

- caller-provided ID `a1b2c3d4` produces exactly `keyword-a1b2c3d4.jsonl` or `llm-a1b2c3d4.jsonl`;
- returned path remains absolute/resolved;
- JSONL schema remains exactly `url` and `abstract`;
- request ID is not added to each JSONL record;
- exclusive creation prevents overwrite;
- write failure never returns a success path;
- best-effort partial-file cleanup is verified where deterministic simulation is practical.

Behaviorally verify that ResultWriter no longer needs to invent a second random filename token.

---

### 7. Observability Configuration Tests

#### Normal Mode

Cover:

- normal `start` does not create/open `debug.log`;
- normal mode does not emit high-volume project DEBUG events;
- existing warning/error behavior remains available.

#### Debug Mode Handler Setup

Using temporary paths and captured stderr, assert:

- project logger namespace is DEBUG;
- intended stderr and rotating-file handlers are installed once;
- rotating file uses production defaults of 5 MiB and 3 backups;
- append mode is used;
- third-party HTTP logger levels are not changed to DEBUG;
- repeated setup/teardown does not duplicate handlers or leak file handles.

#### Session Markers

Cover:

- successful debug startup logs one `session_started` marker with PID/debug state;
- orderly shutdown logs one `session_stopped` marker;
- a second daemon run appends rather than truncating the first run;
- abnormal termination is allowed to have no stop marker.

---

### 8. Log Format Contract Tests

Assert semantic fields, not fragile timestamps.

Representative shape:

```text
DEBUG request=11111111 provider=tavily stage=search event=started
DEBUG request=11111111 provider=tavily stage=search event=completed hits=10 elapsed_ms=123
DEBUG request=11111111 url=https://example.com/a?id=42 event=rejected reason=judge_rejected
```

Cover:

- one physical line per event;
- business events contain request ID;
- lifecycle events are distinguishable and need no business ID;
- provider events identify provider;
- LLM events identify semantic stage rather than only generic transport stage;
- duration values are non-negative integers when present;
- full target URL query values are retained, per the chosen policy;
- values containing newlines are normalized/escaped so the one-line invariant holds.

Only assert global field order if the formatter explicitly defines it as a stable contract.

---

### 9. Secret and Payload Safety Tests

Persistent debug logs make this mandatory.

#### Central Redaction

Extend existing `SecretValue` / `SecretRedactingFilter` coverage:

- configured secret sent through a log message is rendered as `[REDACTED_SECRET]` or the implementation's redaction marker;
- multiple secrets are all redacted;
- empty secret values do not cause pathological replacement behavior;
- traceback/message rendering passes through centralized redaction where supported by the handler design.

#### Authentication Values

Provider/HTTP tests inject a distinctive fake credential value and assert the value is absent from both stderr and file captures. Header names may be logged only as non-secret metadata if intentionally designed; header values must not be logged.

#### Prompt and Content Bodies

Inject unmistakable non-secret sentinels such as:

```text
[TEST_USER_BODY]
[TEST_PAGE_BODY]
[TEST_MODEL_BODY]
```

Run representative LLM-search, judge, safety, clean, summary, keyword, and fetch flows. Assert these body sentinels do not appear in debug output while useful metadata such as character counts, provider/model/stage, and decisions does appear.

#### Target URL Policy

Use a URL with ordinary test query values, for example:

```text
https://example.com/download?id=42&mode=test
```

Assert the complete URL appears when a URL event is logged. This makes the selected full-URL diagnostic policy explicit.

---

### 10. Rotation Tests

Do not create multi-megabyte workflow logs. Test the handler factory with a small injectable threshold while production defaults remain 5 MiB.

Assert:

- current file remains `debug.log`;
- backups use standard `.1`, `.2`, `.3` names;
- no `.4` remains when backup count is 3;
- newest events remain in current/recent files;
- retained file count is bounded;
- restart/reconfiguration appends and continues rotation without clearing all history.

---

### 11. Logging Failure Tests

#### Bootstrap Failure

Simulate open/handler failures through injected factories or monkeypatching rather than platform-dependent chmod behavior.

Cover:

- `start --debug` fails with configuration/startup error;
- runtime is not built afterward;
- daemon socket is not bound;
- partially installed handlers are removed/closed;
- normal `start` under the same unusable debug-log path remains unaffected if ordinary runtime paths are valid.

#### Post-Startup Sink Failure

Use a handler that succeeds during setup and then fails during emit/rotation.

Assert:

- business workflow outcome is determined by business logic, not the logging failure;
- emergency fallback attempts direct stderr without recursively logging through the broken handler;
- stderr diagnostics continue where possible.

Target a custom safe-handler/error hook if introduced rather than relying on CPython development-only logging behavior.

---

### 12. Daemon Dispatch Tests

For each business request type, cover:

- request ID allocated before first workflow event;
- context visible inside orchestrator calls;
- completion/failure uses the same ID;
- expected `GatewayError` produces concise failure event and unchanged `ErrorResponse`;
- unexpected exception produces existing generic internal response;
- unexpected exception includes traceback in debug mode;
- normal mode does not newly expose traceback;
- cancellation is re-raised and context is reset.

For shutdown/control:

- shutdown does not allocate a business request ID;
- session lifecycle logging remains correct.

---

### 13. Search Orchestrator Debug Tests

#### Keyword Search

Use fake providers to cover events for:

- provider start/completion with hit count;
- partial provider failure while another succeeds;
- every candidate/hit decision;
- empty abstract/title rejection;
- invalid provider data category;
- no-body case;
- cheap-check rejection;
- judge rejection with short reason;
- judge acceptance;
- URL dedup/admission metadata;
- final result count/path containing the request ID.

No body content should be asserted in logs.

#### LLM Search

Cover:

- each configured invocation emits provider/model/stage metadata;
- output length and parsed-result count can be logged without response body;
- malformed restricted output logs a provider-pipeline failure without raw model output;
- partial invocation failure remains successful when another completes;
- result filename token equals request ID.

---

### 14. Fetch Orchestrator and Scheduler Debug Tests

Use controlled async providers/events to make scheduling deterministic.

Cover:

- full normalized URL appears;
- relevant URL-store branch is visible: unavailable, cached content, raw-content-only, or provider-fetch-required;
- scheduler logs selected provider;
- quota/scheduler waiting is logged without wall-clock sleeps;
- execution failure logs fallback to a later provider;
- semantic rejection logs rejection and a later provider can succeed;
- each fetched candidate gets accepted/rejected metadata;
- content-clean logs stage and size metadata without body;
- safety logs decision and short reason;
- focus-summary logs stage metadata without focus/page body.

#### Singleflight Correlation

For two callers sharing one physical same-URL fetch:

- each outer request retains its own request ID for its wait/start/completion events;
- the shared provider operation is logged once under the request that actually initiated that physical work;
- a waiting caller must not cause one provider call to appear as if it happened twice under two request IDs.

This should be tested explicitly because ContextVar plus singleflight is a subtle concurrency boundary.

---

### 15. HTTP Executor Debug Tests

Extend existing HTTP executor tests to cover:

- attempt event includes provider and semantic stage;
- successful status and elapsed time;
- retryable status classes and transport failures produce concise retry metadata;
- attempt count across retries is correct;
- terminal non-retryable client error preserves existing execution-failure semantics;
- invalid JSON preserves existing protocol-failure semantics without logging raw response;
- fake credential values remain absent from stderr and file captures.

Use deterministic clock injection only if exact durations are asserted; otherwise assert duration type/range.

---

### 16. LLM Stage Debug Tests

For judge, safety, content-clean, focus-summary, and LLM-search, cover:

- start event includes stage/provider/model;
- input-size metadata where implemented;
- completion event includes output size or decision as relevant;
- decision reason is normalized to one line;
- raw message contents are absent;
- invalid response/parse failure logs category, not raw output;
- retries remain execution semantics, not semantic rejection.

---

### 17. Doctor Config Tests

Doctor logic should produce inspectable check results separately from rendering where practical.

Cover:

- config file missing -> fail;
- malformed TOML -> fail;
- valid TOML plus valid environment -> ok;
- unknown/unsupported provider configuration -> fail through existing resolver;
- missing referenced LLM provider -> fail;
- required environment variable not set -> fail;
- required environment variable set -> ok may mention only the variable name;
- configured secret value never appears in output.

Do not reimplement config-validation rules in doctor tests; use cases that prove doctor delegates to the existing resolver.

#### Aggregation

Cover:

- config failure does not prevent independent cache/socket checks;
- multiple failures are all rendered;
- any fail makes final exit `1`.

---

### 18. Doctor Filesystem Tests

Using temporary paths and injectable filesystem operations where needed, cover:

- existing writable cache/results/log directories -> ok;
- missing but creatable path -> ok without leaving permanent repair artifacts;
- expected directory occupied by a regular file -> fail;
- transient write probe is removed after success;
- simulated probe write failure -> fail;
- simulated probe cleanup failure follows the documented fail behavior;
- doctor does not create persistent `debug.log` or result files;
- doctor does not modify config contents.

Prefer injected failures over permission-mode assumptions that differ under CI/root environments.

---

### 19. Doctor Socket Tests

Use temporary real Unix sockets for happy-path integration and injected probes for hard-to-force errors.

Cover:

- socket absent -> `[info] daemon not running`, overall may exit 0;
- live local Unix server -> `[ok] daemon running`;
- stale/refused socket -> fail;
- regular file at socket path -> fail;
- probe timeout -> fail without long sleep;
- permission/other OS error -> fail;
- doctor never unlinks a stale socket;
- probe sends no business request and does not mutate runtime URL state.

Where practical, refactor/reuse the daemon's existing bounded socket-inspection primitive so doctor and startup do not drift.

---

### 20. Doctor No-Network Guarantee

Add an explicit regression test that fails if doctor attempts external HTTP/client activity.

Possible seams:

- inject a client factory that raises if called;
- monkeypatch external HTTP client construction/request to raise an assertion;
- prove doctor resolves configuration without calling full `Runtime.build`.

A valid doctor run must succeed with external network disabled.

---

### 21. Doctor Rendering Tests

Given deterministic check objects, assert:

```text
[ok] ...
[info] ...
[fail] ...
```

Cover:

- one check per line;
- no secret values;
- safe paths/reasons are readable;
- exit code is derived from statuses, not from daemon-running state alone.

Avoid large snapshot tests unless wording is intentionally declared stable.

---

### 22. README and Documentation Regression Tests

Assert README documents end-user installation:

```text
uv tool install .
```

Assert it still documents development/verification:

```text
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

Also assert documentation covers:

- `agent-search-gateway doctor`;
- `agent-search-gateway start --debug`;
- debug-log path;
- 5 MiB rotation and 3 backups at a useful user-facing level;
- debug logs may contain complete target URLs and should be treated as potentially sensitive local artifacts;
- prompt/page/model response bodies are not intentionally logged;
- normal business-command stdout remains final-output-only.

CI's `uv sync --locked` should not be replaced merely to mirror user installation.

---

### 23. Package and Tool-Install Verification

Keep existing package metadata coverage for the console script entry point and package discovery of any new modules.

Recommended isolated smoke test, if CI cost and environment permit:

```text
set temporary uv tool directory/bin directory
uv tool install .
<temporary-bin>/agent-search-gateway --help
```

The smoke test should prove:

- source-checkout tool installation succeeds;
- installed executable exposes `doctor` and `start --debug`;
- dev dependencies are not required to execute the installed CLI.

Do not replace the normal development test environment with tool installation. If clean isolation is awkward, keep this as manual/release verification instead of modifying a global tool installation in CI.

---

### 24. Acceptance Tests

Add one no-network end-to-end debug workflow using fake providers/runtime plus a temporary Unix socket where the current acceptance harness allows it.

Scenario:

```text
start daemon in debug mode
send keyword-search
provider returns two hits:
  one body accepted
  one body rejected by judge but URL retained
receive keyword-<request_id>.jsonl
stop daemon
```

Assert:

- socket response remains the existing success shape;
- result filename contains the same ID found in debug logs;
- debug log contains session markers, request lifecycle, provider activity, and both candidate decisions;
- JSONL contains only `url` and `abstract`;
- test body and credential sentinels are absent from logs;
- enabling gateway debug alone does not add low-level third-party HTTP DEBUG noise.

Add a lightweight doctor acceptance case:

```text
valid config/environment
writable temporary paths
no daemon socket
=> local checks healthy, daemon info not running, exit 0
```

---

### 25. Test Fixtures and Helpers

Recommended additions:

- fixed/colliding request-ID factory;
- in-memory log handler/captured stream helper;
- handler that fails only after successful setup;
- deterministic monotonic clock if exact elapsed values matter;
- injectable doctor filesystem probe operations;
- injectable doctor socket probe plus real Unix-socket happy-path coverage;
- reuse existing fake providers and fake LLM clients.

Avoid wall-clock sleeps and real provider credentials.

---

### 26. Verification Commands

Development verification remains:

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

If an isolated tool-install smoke check is added, use temporary uv tool/bin locations so it cannot modify the user's normal tool environment.

---

### 27. Acceptance Criteria

Implementation is ready only when:

- existing no-network tests stay green;
- normal start creates no debug-log artifact, while `start --debug` configures stderr plus rotating-file diagnostics;
- rotation is verified as 5 MiB with 3 backups through a small-threshold test seam;
- concurrent workflows prove request-ID isolation;
- search result filename token equals logged request ID;
- result JSONL public schema is unchanged;
- representative provider/candidate/stage paths have debug-event coverage;
- prompt/page/model body sentinels never appear in debug output;
- configured credential sentinel values never appear in debug output;
- complete target URL query values do appear, matching the chosen policy;
- expected failures have no traceback and unexpected internal failures have traceback only in debug mode;
- logging bootstrap failure blocks only debug startup;
- post-start logging sink failure does not change business semantics;
- doctor diagnoses config/filesystem/socket state without external API calls or persistent repairs;
- doctor returns 0 when there are no true failures even if daemon is not running, and 1 when any fail exists;
- README distinguishes `uv tool install .` for users from `uv sync --locked` for development/CI;
- CI remains on the locked development environment unless a separately isolated tool-install smoke test is intentionally added.
