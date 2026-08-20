---
created: 2026-08-20
updated: 2026-08-20
notes: "基于 debug-doctor-uv-tool-install 的 architecture、error-handling 与 testing 设计；当前基线为 99 passed, 2 skipped。"
---

# Debug Tracing, Doctor, and uv Tool Installation Implementation Plan

> **For agent:** REQUIRED SKILL: `executing-plans`

**Goal:** 为前台 daemon 增加可持久化、可关联、不会泄漏认证信息或正文载荷的详细 DEBUG 追踪；增加完全本地、确定性、无网络的 `doctor` 诊断命令；把最终用户安装流程改为 `uv tool install .`，同时保留开发与 CI 的 `uv sync --locked`。

**Architecture:** 保持现有 Unix-socket NDJSON 协议和业务输出契约不变。`start --debug` 在创建 runtime 之前为 `agent_search_gateway` logger namespace 安装 stderr 与 rotating-file handlers；daemon 为每个业务请求生成一个 8 位十六进制 `request_id`，通过 `ContextVar` 传播到异步子任务，并显式传给 search/result writer 作为结果文件 token。`doctor` 复用现有 config resolver，并通过只读 filesystem/socket probes 汇总本地状态。Provider、LLM stage、retry、quota、scheduler 和 candidate 决策只记录操作元数据，不记录 query/prompt/page/model body。

**Tech Stack:** Python 3.12、`asyncio`、`argparse`、`contextvars`、标准库 `logging`/`RotatingFileHandler`、`pathlib`/Unix domain sockets、`httpx`、`pytest`/`pytest-asyncio`、`ruff`、`mypy`、`uv`。

**Design inputs:**

- `docs/designs/architectures/debug-doctor-uv-tool-install.md`
- `docs/designs/error-handlings/debug-doctor-uv-tool-install.md`
- `docs/designs/testings/debug-doctor-uv-tool-install.md`

---

## Implementation Boundaries

本计划只实现设计文档锁定的范围。不要顺手加入 per-request `--debug`、socket progress streaming、raw request/response dumps、TRACE mode、`doctor --fix`、provider/API-key live connectivity checks、daemon `status`、JSON doctor output、PyPI/Git URL 安装说明或运行时配置热重载。

必须保持以下外部契约：

- Socket request/response envelope 不增加 `request_id`、debug flag 或 progress event。
- `keyword-search` / `llm-search` 成功 stdout 仍然只有 absolute JSONL path。
- `url-fetch` 成功 stdout 仍然只有 content、summary 或稳定 unavailable 文本。
- JSONL 每行仍然只有 `url` 与 `abstract`。
- `start` / `stop` 是 lifecycle/control 操作，不分配业务 request ID。
- `doctor` 是本地 CLI 分支，不经过 daemon socket，不构造完整 `Runtime`，不访问外部网络。
- 普通 `start` 不创建、不打开、不追加 `debug.log`，也不启用高容量 DEBUG event。
- Debug bootstrap fail-closed；daemon 成功启动后的日志 sink 故障不得改变业务结果。
- Expected `GatewayError` 不记录 traceback；unexpected exception 只在 daemon debug mode 中记录 traceback，并继续返回现有 generic internal error。
- 完整 target URL（含 query）允许写入 debug log；query/prompt/focus/page/candidate/model-response body 和认证 header value 不得被有意记录。
- 最终用户安装使用 `uv tool install .`；开发和 CI 仍使用 `uv sync --locked` 与 `uv run ...`。不要把 CI 的 locked development environment 替换成 tool install。

当前验证基线：

```text
uv run pytest -q
99 passed, 2 skipped
```

---

## Locked Internal Interfaces

为避免执行阶段重新做架构决策，本计划锁定以下最小接口。

### Request ID and Context

```python
RequestIdFactory = Callable[[], str]
ResultKind = Literal["keyword", "llm"]


def generate_request_id() -> str: ...
def validate_request_id(value: str) -> str: ...
def current_request_id() -> str | None: ...
@contextmanager
def bind_request_id(request_id: str) -> Iterator[None]: ...
def result_filename(kind: ResultKind, request_id: str) -> str: ...


class RequestIdRegistry:
    def __init__(
        self,
        results_dir: Path,
        *,
        factory: RequestIdFactory = generate_request_id,
        max_attempts: int = 256,
    ) -> None: ...

    @contextmanager
    def reserve(self, *, may_write_search_result: bool) -> Iterator[str]: ...
```

Rules:

- `validate_request_id` accepts exactly `[0-9a-f]{8}`.
- `reserve()` checks the active in-process set atomically before any `await`.
- Search reservations also reject an ID when either `keyword-<id>.jsonl` or `llm-<id>.jsonl` already exists.
- Exhaustion or malformed factory output is an internal invariant failure, not a new public `ErrorCode`.
- Reservation release and `ContextVar` reset occur in `finally` on success, typed error, unexpected error and cancellation.

### Search and Result Writing

```python
async def SearchOrchestrator.keyword_search(
    self,
    query: str,
    *,
    request_id: str,
) -> str: ...

async def SearchOrchestrator.llm_search(
    self,
    prompt: str,
    *,
    request_id: str,
) -> str: ...


def ResultWriter.write_results(
    self,
    kind: ResultKind,
    records: Iterable[SearchRecord],
    *,
    request_id: str,
) -> Path: ...
```

`ResultWriter` 不再生成随机 token，也不在 `FileExistsError` 后改名重试。它对一个已接受的 request ID 只尝试一个 exact target，并保留 exclusive-create 作为最终 race guard。

### Structured Logging

```python
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: bool | BaseException = False,
    **fields: object,
) -> None: ...


def configure_debug_logging(
    log_file: Path,
    *,
    stderr: TextIO,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
    file_handler_factory: FileHandlerFactory | None = None,
) -> DebugLoggingSession: ...
```

`DebugLoggingSession` owns only本次配置安装的 handlers，支持 `add_secrets(...)` 和 idempotent `close()`，并恢复 logger 原有 level/propagate state。Formatter 在最终 traceback/message 渲染后做 secret redaction，并把 newline/tab/control characters 转义为单行值。

### Doctor

```python
class DoctorStatus(StrEnum):
    OK = "ok"
    INFO = "info"
    FAIL = "fail"

@dataclass(frozen=True, slots=True)
class DoctorCheck:
    status: DoctorStatus
    message: str

@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def exit_code(self) -> int: ...

async def run_doctor(
    paths: RuntimePaths,
    *,
    environ: Mapping[str, str],
    directory_probe: DirectoryProbe = probe_directory_writable,
    socket_probe: SocketProbe = probe_unix_socket,
) -> DoctorReport: ...

def render_doctor(report: DoctorReport, stream: TextIO) -> None: ...
```

`run_doctor` 返回全部可获得检查；只有 `FAIL` 影响 exit code。`daemon not running` 是 `INFO`。

---

## Stable Debug Event Vocabulary

实现可以添加必要字段，但不要为同一语义创造多套 event name。

| Area | Required event names | Required/safe fields |
|---|---|---|
| Daemon session | `session_started`, `session_stopped` | `pid`, `debug` |
| Business request | `workflow_started`, `workflow_completed`, `workflow_failed`, `workflow_cancelled`, `workflow_rejected` | `command`, `elapsed_ms`, `error_code`, `error_type` |
| Provider pipeline | `provider_started`, `provider_completed`, `provider_failed` | `provider`, `stage`, counts, `elapsed_ms` |
| HTTP/retry | `http_attempt_started`, `http_attempt_completed`, `http_retrying`, `http_failed` | `provider`, `stage`, endpoint URL, `attempt`, `status`, `delay_ms`, `elapsed_ms`, failure category |
| Quota/scheduler | `quota_waiting`, `quota_acquired`, `quota_released`, `scheduler_waiting`, `provider_selected`, `provider_fallback` | `provider`, quota kind, `in_use`, `limit`, candidate count |
| LLM semantic stage | `llm_stage_started`, `llm_stage_completed`, `llm_stage_failed`, `llm_stage_cancelled` | `provider`, `stage`, `model`, char counts, decision, short reason, `elapsed_ms` |
| Search/fetch candidates | `candidate_accepted`, `candidate_rejected`, `body_accepted`, `body_rejected`, `body_skipped` | full `url`, source/provider, char counts, stable reason |
| Singleflight/lock | `singleflight_leader`, `singleflight_joined`, `url_lock_acquired` | full `url`, focus-present boolean, wait time |
| Persistence | `results_written` | `kind`, `path`, `results` |

Every business event receives `request=<8hex>` from the formatter. Lifecycle/startup events render `request=-`. Do not log query/prompt/focus/page/candidate/model-output strings as fields; use only presence flags and character counts.

---

## Planned File Impact

New production modules:

```text
src/agent_search_gateway/request_ids.py
src/agent_search_gateway/socket_probe.py
src/agent_search_gateway/doctor.py
```

New focused tests:

```text
tests/unit/test_request_ids.py
tests/unit/test_observability_logging.py
tests/unit/test_socket_probe.py
tests/daemon/test_daemon_request_ids.py
tests/daemon/test_daemon_debug.py
tests/doctor/__init__.py
tests/doctor/test_config.py
tests/doctor/test_filesystem.py
tests/doctor/test_socket.py
tests/doctor/test_rendering.py
tests/acceptance/test_debug_and_doctor.py
```

Existing files remain authoritative for business semantics; instrumentation is added at their existing boundaries rather than through wrapper-only test code.

---

### Task 1: Add Derived Debug Log Paths Without Breaking RuntimePaths Construction

**Files:**

- Modify: `src/agent_search_gateway/paths.py`
- Modify: `tests/unit/test_paths.py`
- Reference: `docs/designs/architectures/debug-doctor-uv-tool-install.md` (Rotating Debug Log in Cache)

- [ ] **Step 1: Extend the path contract test first**

Update `test_runtime_paths_are_derived_from_home_without_global_mutation` to assert:

```python
assert paths.logs_dir == tmp_path / ".cache/agent-search-gateway-cli/logs"
assert paths.debug_log_file == paths.logs_dir / "debug.log"
```

Add a second assertion block constructing `RuntimePaths(config_file=..., socket_file=..., results_dir=...)` manually and verify the log path derives from `socket_file.parent`. This prevents adding a fourth required dataclass field and breaking current tests/fakes.

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```bash
uv run pytest tests/unit/test_paths.py -v
```

Expected:

```text
FAIL because RuntimePaths has no logs_dir/debug_log_file properties
```

- [ ] **Step 3: Implement only the two derived properties**

In `RuntimePaths` add:

```python
@property
def logs_dir(self) -> Path:
    return self.socket_file.parent / "logs"

@property
def debug_log_file(self) -> Path:
    return self.logs_dir / "debug.log"
```

Do not create directories here and do not add XDG overrides or a mutable field.

- [ ] **Step 4: Verify GREEN and regression safety**

Run:

```bash
uv run pytest tests/unit/test_paths.py -v
uv run pytest tests/cli/test_cli.py tests/daemon -q
```

Expected:

```text
Path tests PASS; existing manual RuntimePaths construction and daemon tests remain green
```

- [ ] **Step 5: Refactor/check without expanding scope**

Run:

```bash
uv run ruff check src/agent_search_gateway/paths.py tests/unit/test_paths.py
uv run mypy src/agent_search_gateway/paths.py tests/unit/test_paths.py
```

Expected: both commands pass.

- [ ] **Step 6: Commit**

```bash
git add src/agent_search_gateway/paths.py tests/unit/test_paths.py
git commit -m "feat: add debug runtime paths"
```

---

### Task 2: Implement Request IDs, ContextVar Propagation, and Collision Reservations

**Files:**

- Create: `src/agent_search_gateway/request_ids.py`
- Create: `tests/unit/test_request_ids.py`
- Reference: `docs/designs/architectures/debug-doctor-uv-tool-install.md` (One Request ID Per Business Workflow; ContextVar)
- Reference: `docs/designs/error-handlings/debug-doctor-uv-tool-install.md` (Request-ID Failure Rules)

- [ ] **Step 1: Write token-shape and context reset tests**

Create tests that assert:

```python
assert re.fullmatch(r"[0-9a-f]{8}", generate_request_id())
assert current_request_id() is None
with bind_request_id("11111111"):
    assert current_request_id() == "11111111"
assert current_request_id() is None
```

Also cover nested bindings and reset after an exception raised inside the context manager.

- [ ] **Step 2: Write async child-task isolation tests**

Use two controlled coroutines, bind `11111111` and `22222222`, create child tasks with `asyncio.create_task`/`gather`, interleave their releases, and assert each task and child sees only its own ID. After both complete, assert the parent context is `None`.

- [ ] **Step 3: Write reservation/collision tests**

Use an injected iterator factory such as:

```python
factory = iter(["11111111", "11111111", "22222222", "33333333"]).__next__
```

Cover:

- nested active reservation rejects the repeated first value and yields `22222222`;
- an existing `keyword-33333333.jsonl` or `llm-33333333.jsonl` causes a search reservation to regenerate;
- URL-fetch-style reservation (`may_write_search_result=False`) only checks active IDs;
- reservation release allows reuse after the context exits;
- malformed factory output and bounded exhaustion raise an internal `RuntimeError` rather than looping forever;
- `result_filename("keyword", "a1b2c3d4")` is exact and invalid kind/ID is rejected.

- [ ] **Step 4: Run the new tests and confirm RED**

Run:

```bash
uv run pytest tests/unit/test_request_ids.py -v
```

Expected:

```text
FAIL with ModuleNotFoundError because request_ids.py does not exist
```

- [ ] **Step 5: Implement the minimal module**

Implement:

```text
_REQUEST_ID: ContextVar[str | None] with default None
validate_request_id using one compiled full-match regex
generate_request_id -> secrets.token_hex(4)
bind_request_id -> ContextVar.set/reset in finally
result_filename -> validated kind + ID
RequestIdRegistry._active: set[str]
RequestIdRegistry.reserve:
  for at most max_attempts:
    candidate = validate factory output
    reject if active
    if may_write_search_result:
      reject if either result filename exists under results_dir
    add candidate before yielding
    remove candidate in finally
  raise RuntimeError on exhaustion
```

Do not use an `asyncio.Lock`: generation/check/add contains no `await` and executes atomically in one event-loop turn.

- [ ] **Step 6: Verify GREEN, types, and cancellation-safe context behavior**

Run:

```bash
uv run pytest tests/unit/test_request_ids.py -v
uv run ruff check src/agent_search_gateway/request_ids.py tests/unit/test_request_ids.py
uv run mypy src/agent_search_gateway/request_ids.py tests/unit/test_request_ids.py
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/agent_search_gateway/request_ids.py tests/unit/test_request_ids.py
git commit -m "feat: add request id context"
```

---

### Task 3: Make the Daemon Own One Request ID and Reuse It for Search Result Files

**Files:**

- Modify: `src/agent_search_gateway/result_writer.py`
- Modify: `src/agent_search_gateway/orchestrators/search.py`
- Modify: `src/agent_search_gateway/daemon.py`
- Modify: `tests/unit/test_result_writer.py`
- Create: `tests/daemon/test_daemon_request_ids.py`
- Modify: `tests/daemon/test_daemon_dispatch.py`
- Modify: `tests/daemon/test_daemon_shutdown.py`
- Modify: `tests/orchestrators/test_keyword_search_pipeline.py`
- Modify: `tests/orchestrators/test_keyword_search_state.py`
- Modify: `tests/orchestrators/test_llm_search.py`
- Modify: `tests/acceptance/test_gateway_workflows.py`
- Modify: `tests/support/acceptance.py`

- [ ] **Step 1: Rewrite ResultWriter tests to express the new contract**

Replace random-name expectations with caller-owned IDs:

```python
first = writer.write_results("keyword", records, request_id="11111111")
second = writer.write_results("keyword", records, request_id="22222222")
assert first.name == "keyword-11111111.jsonl"
assert second.name == "keyword-22222222.jsonl"
```

Add tests that assert:

- invalid IDs fail before file creation;
- JSONL still contains only `url` and `abstract`;
- an existing exact target raises `FileExistsError` and is not overwritten;
- an injected write failure removes only a file created by that call and never returns a path;
- `request_id` is not written into records.

- [ ] **Step 2: Run ResultWriter tests and confirm RED**

Run:

```bash
uv run pytest tests/unit/test_result_writer.py -v
```

Expected:

```text
FAIL because write_results does not accept request_id and still generates its own token
```

- [ ] **Step 3: Add daemon propagation tests before implementation**

Create `tests/daemon/test_daemon_request_ids.py` with deterministic factories and fake orchestrators that record `current_request_id()` plus explicit search `request_id` arguments. Cover:

- keyword, LLM and URL fetch each receive one ID allocated before orchestration;
- keyword/LLM explicit argument equals the ambient context ID;
- shutdown allocates no ID;
- context is reset after success, typed `GatewayError`, unexpected exception and task cancellation;
- two concurrent socket requests with `11111111` and `22222222` remain isolated while blocked/interleaved;
- an existing result filename forces regeneration before the search starts;
- a repeated active ID is regenerated before the second workflow starts.

- [ ] **Step 4: Run the daemon request-ID tests and confirm RED**

Run:

```bash
uv run pytest tests/daemon/test_daemon_request_ids.py -v
```

Expected:

```text
FAIL because ForegroundDaemon does not allocate/bind request IDs or pass them to search orchestrators
```

- [ ] **Step 5: Implement exact-target ResultWriter behavior**

Change `write_results` to require keyword-only `request_id` and:

```text
validate kind and ID
serialize/validate all records before opening the target
target = results_dir / result_filename(kind, request_id)
mkdir results_dir
open target once with mode="x", UTF-8, newline="\n"
write the pre-serialized lines
on a write/flush/close failure after successful create:
  best-effort unlink that exact newly-created target
  re-raise the original error
return target.resolve()
```

Do not catch `FileExistsError` to generate a second token.

- [ ] **Step 6: Thread explicit IDs through search orchestration**

Change signatures to:

```python
async def keyword_search(self, query: str, *, request_id: str) -> str
async def llm_search(self, prompt: str, *, request_id: str) -> str
```

Validate the ID near method entry and call:

```python
self._result_writer.write_results(kind, records, request_id=request_id)
```

Update all direct orchestrator tests so repeated searches use distinct fixed IDs; keep all existing business assertions unchanged.

- [ ] **Step 7: Allocate, reserve, bind, and release at daemon dispatch**

In `ForegroundDaemon.__init__`, accept an injectable `request_id_factory` and construct one `RequestIdRegistry(paths.results_dir, factory=...)`.

Refactor dispatch into a focused business helper:

```text
if ShutdownRequest:
  preserve existing control path, no request ID
else:
  reserve ID before any workflow work
  search requests reserve with may_write_search_result=True
  url fetch reserves with False
  bind ContextVar
  perform shutting-down rejection / active-task tracking / invocation
  pass request_id explicitly only to keyword_search and llm_search
  always remove active task, reset context, and release reservation
```

Keep socket envelopes and public responses unchanged.

- [ ] **Step 8: Update contract-realistic fakes and existing tests**

Update fake search method signatures in daemon/shutdown tests and support fixtures to accept keyword-only `request_id`. Do not add test-only production hooks. Direct orchestrator tests pass explicit fixed IDs; acceptance tests obtain IDs from the daemon-generated result path rather than assuming random filenames.

- [ ] **Step 9: Verify targeted GREEN and the full suite**

Run:

```bash
uv run pytest tests/unit/test_result_writer.py tests/daemon/test_daemon_request_ids.py -v
uv run pytest tests/orchestrators tests/daemon tests/acceptance -q
uv run pytest -q
```

Expected:

```text
All tests PASS; result JSON schema and socket protocol remain unchanged
```

- [ ] **Step 10: Refactor/check**

Keep ID ownership singular: daemon generates/reserves; ContextVar is diagnostic; ResultWriter consumes an explicit functional argument. Run:

```bash
uv run ruff check src/agent_search_gateway/request_ids.py src/agent_search_gateway/result_writer.py src/agent_search_gateway/orchestrators/search.py src/agent_search_gateway/daemon.py tests/unit/test_result_writer.py tests/daemon/test_daemon_request_ids.py
uv run mypy src tests
```

Expected: both pass.

- [ ] **Step 11: Commit**

```bash
git add src/agent_search_gateway/result_writer.py src/agent_search_gateway/orchestrators/search.py src/agent_search_gateway/daemon.py tests
git commit -m "feat: correlate requests with result files"
```

---

### Task 4: Build Structured One-Line Logging, Rotation, and Final-Stage Secret Redaction

**Files:**

- Modify: `src/agent_search_gateway/observability.py`
- Create: `tests/unit/test_observability_logging.py`
- Modify: `tests/providers/test_http_executor.py`

- [ ] **Step 1: Write formatter/event contract tests**

Create focused tests around `log_event` and the formatter. Bind `request_id="11111111"`, emit an event containing provider, semantic stage, full URL with query values, integer counts and a reason containing newline/tab characters. Assert:

```text
one physical output line
starts with DEBUG request=11111111
contains provider/stage/event fields
contains the complete target URL query string
newline/tab values are escaped, not emitted physically
lifecycle event outside a request renders request=-
```

Only assert field order that the formatter explicitly declares stable.

- [ ] **Step 2: Write redaction and traceback tests**

Cover:

- one and multiple `SecretValue` instances;
- empty secrets are ignored;
- a secret appearing in a normal message field is redacted;
- a secret appearing only inside an exception message/traceback is redacted after traceback formatting;
- raw exception traceback is represented as an escaped one-line field;
- existing `SecretRedactingFilter` API remains usable by current provider tests.

- [ ] **Step 3: Write handler lifecycle and rotation tests**

Using temporary paths and an injected small `max_bytes`, assert:

- project logger level becomes DEBUG and `propagate=False` only for the active session;
- exactly one stderr and one rotating file handler are installed;
- production defaults are 5 MiB and 3 backups;
- file mode appends across session restart;
- rotation keeps `debug.log`, `.1`, `.2`, `.3` and no `.4`;
- `httpx` and `httpcore` logger levels are unchanged;
- `close()` removes/closes only owned handlers and restores prior project logger state;
- configure-close-configure does not duplicate handlers or leak file descriptors.

- [ ] **Step 4: Write logging-failure tests**

Use injected handler factories rather than chmod assumptions. Cover:

- directory/file/handler setup failure raises `ConfigFailure(CONFIG_ERROR)` and removes partial handlers;
- a handler that succeeds at setup then fails on emit writes one best-effort emergency line directly to the injected/original stderr;
- handler failure does not raise back through `logger.debug(...)` or recurse into the broken logger.

- [ ] **Step 5: Run the tests and confirm RED**

Run:

```bash
uv run pytest tests/unit/test_observability_logging.py -v
```

Expected:

```text
FAIL because structured formatter, debug logging session, rotation, and sink-failure handling do not exist
```

- [ ] **Step 6: Implement a centralized redactor and safe formatter**

Extend `observability.py` with:

```text
SecretRedactor:
  maintain unique non-empty secret strings
  add SecretValue instances after config resolution
  redact a final rendered string

KeyValueFormatter.format(record):
  obtain current_request_id() or "-"
  render LEVEL + request + validated event fields
  normalize scalar values deterministically
  JSON-escape/quote strings when needed so one physical line is guaranteed
  append escaped traceback when exc_info exists
  pass the final complete line through SecretRedactor
```

Refactor `SecretRedactingFilter` to reuse the same redactor without removing its existing public behavior.

- [ ] **Step 7: Implement event and handler/session APIs**

Implement:

```text
log_event(logger, level, event, exc_info=False, **fields)
SafeRotatingFileHandler.handleError -> direct emergency stderr write
DebugLoggingSession.add_secrets / close
configure_debug_logging:
  create logs directory
  build stderr handler
  build RotatingFileHandler(mode="a", encoding="utf-8", maxBytes=5 MiB, backupCount=3)
  attach the same formatter/redactor to both
  tag handlers as owned
  configure only logging.getLogger("agent_search_gateway")
  on any setup exception, close/remove partial handlers, restore logger, raise ConfigFailure
```

Do not call `logging.basicConfig()` and do not change the root/httpx/httpcore loggers.

- [ ] **Step 8: Verify GREEN and existing secret-safety tests**

Run:

```bash
uv run pytest tests/unit/test_observability_logging.py tests/providers/test_http_executor.py -v
uv run ruff check src/agent_search_gateway/observability.py tests/unit/test_observability_logging.py
uv run mypy src/agent_search_gateway/observability.py tests/unit/test_observability_logging.py
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/agent_search_gateway/observability.py tests/unit/test_observability_logging.py tests/providers/test_http_executor.py
git commit -m "feat: add structured debug logging"
```

---

### Task 5: Wire `start --debug` into CLI and Daemon Session Lifecycle

**Files:**

- Modify: `src/agent_search_gateway/cli.py`
- Modify: `src/agent_search_gateway/daemon.py`
- Modify: `tests/cli/test_cli.py`
- Create: `tests/daemon/test_daemon_debug.py`

- [ ] **Step 1: Add parser and CLI bootstrap tests first**

Extend CLI tests to assert:

- `start` parses with `debug=False`;
- `start --debug` parses with `debug=True`;
- `--debug` is rejected for stop/search/fetch/doctor (doctor will be added later; keep that assertion for Task 13 if parser choice is not yet present);
- normal start passes `debug=False` and no logging session to the daemon factory;
- debug start configures logging before invoking the daemon factory;
- debug logging bootstrap failure prints the safe `ConfigFailure` message to stderr, exits 1, and never invokes the daemon factory;
- normal start under the same unusable debug path remains unaffected and creates no `debug.log`.

Use dependency injection for `logging_configurer`; do not patch global stderr or home.

- [ ] **Step 2: Run CLI tests and confirm RED**

Run:

```bash
uv run pytest tests/cli/test_cli.py -v
```

Expected:

```text
FAIL because start has no --debug option and run_command does not configure a debug logging session
```

- [ ] **Step 3: Add daemon session-marker tests**

Create tests using temporary paths and a fake runtime. Assert:

- a successful debug daemon bind logs exactly one `session_started` with `pid` and `debug=true`;
- orderly shutdown logs exactly one `session_stopped`;
- a second debug run appends markers rather than truncating the previous file;
- normal daemon mode emits no debug session markers and does not create/open `debug.log`;
- config/runtime failure before socket bind does not emit a successful `session_started` marker;
- logging session closes after daemon termination, including startup failure.

- [ ] **Step 4: Run daemon debug tests and confirm RED**

Run:

```bash
uv run pytest tests/daemon/test_daemon_debug.py -v
```

Expected:

```text
FAIL because ForegroundDaemon has no debug/logging-session lifecycle
```

- [ ] **Step 5: Change the daemon-factory contract explicitly**

Define the injected factory with keyword arguments:

```python
DaemonFactory = Callable[..., DaemonLike]

daemon_factory(
    paths,
    debug=args.debug,
    logging_session=logging_session,
)
```

Update fake factories in CLI tests to accept these keywords. The default factory constructs:

```python
ForegroundDaemon(paths, debug=debug, logging_session=logging_session)
```

- [ ] **Step 6: Configure and close logging around foreground start**

In the `start` branch only:

```text
logging_session = None
if args.debug:
  logging_session = configure_debug_logging(paths.debug_log_file, stderr=stderr)
try:
  daemon = daemon_factory(...)
  await daemon.start()
finally:
  if logging_session is not None:
    logging_session.close()
```

Map bootstrap `ConfigFailure` through the existing startup stderr/non-zero path. Do not configure debug logging for business CLI processes.

- [ ] **Step 7: Register resolved secrets before building provider clients**

In the default daemon runtime construction path:

```text
load TOML
resolve config
if logging_session exists:
  collect non-None web secrets and all resolved LLM secrets
  logging_session.add_secrets(...)
build Runtime
```

Never log secret values while collecting them. Custom test runtime factories need no special secret hook.

- [ ] **Step 8: Emit session markers at the correct lifecycle points**

After runtime creation and successful Unix-server bind, but before exposing `ready`, emit `session_started`. During one-time cleanup, before the logging session is closed by CLI, emit `session_stopped` when a session was actually started. Track booleans so repeated shutdown/cleanup cannot duplicate either marker.

- [ ] **Step 9: Verify GREEN and normal-mode regression**

Run:

```bash
uv run pytest tests/cli/test_cli.py tests/daemon/test_daemon_debug.py -v
uv run pytest tests/daemon -q
```

Expected:

```text
Debug startup/session tests PASS; ordinary CLI and daemon lifecycle behavior remains unchanged
```

- [ ] **Step 10: Refactor/check**

Run:

```bash
uv run ruff check src/agent_search_gateway/cli.py src/agent_search_gateway/daemon.py tests/cli/test_cli.py tests/daemon/test_daemon_debug.py
uv run mypy src tests
```

Expected: both pass.

- [ ] **Step 11: Commit**

```bash
git add src/agent_search_gateway/cli.py src/agent_search_gateway/daemon.py tests/cli/test_cli.py tests/daemon/test_daemon_debug.py
git commit -m "feat: wire debug daemon startup"
```

---

### Task 6: Add Request-Scoped Daemon Lifecycle Events and Debug-Only Tracebacks

**Files:**

- Modify: `src/agent_search_gateway/daemon.py`
- Modify: `tests/daemon/test_daemon_request_ids.py`
- Modify: `tests/daemon/test_daemon_dispatch.py`
- Modify: `tests/daemon/test_daemon_shutdown.py`
- Modify: `tests/daemon/test_daemon_debug.py`

- [ ] **Step 1: Write request lifecycle log tests**

For each business request type, assert the same ID appears in:

```text
workflow_started
workflow_completed or workflow_failed/workflow_cancelled/workflow_rejected
orchestrator-observed ContextVar
search explicit request_id where applicable
```

Assert `elapsed_ms` is a non-negative integer. Inject a monotonic clock into `ForegroundDaemon` if exact deterministic values are useful.

- [ ] **Step 2: Write failure-policy tests**

Cover:

- expected `GatewayError` -> one concise `workflow_failed` event with `error_code`, unchanged `ErrorResponse`, no traceback;
- shutting-down request -> `workflow_rejected`, unchanged `DAEMON_SHUTTING_DOWN` response;
- cancellation -> `workflow_cancelled`, cancellation re-raised, context/reservation reset;
- unexpected exception in debug mode -> structured first line plus escaped traceback and existing generic internal response;
- the same unexpected exception in normal mode -> only short exception type, no sensitive exception text and no traceback;
- query/prompt/focus strings are not logged by daemon lifecycle fields; only command and timing metadata are present;
- shutdown/control and malformed frames have no business request ID.

- [ ] **Step 3: Run focused tests and confirm RED**

Run:

```bash
uv run pytest tests/daemon/test_daemon_request_ids.py tests/daemon/test_daemon_debug.py -v
```

Expected:

```text
FAIL because daemon dispatch has request IDs but no structured lifecycle events or debug-only traceback policy
```

- [ ] **Step 4: Add a focused command-name helper and timer**

Map typed requests to stable command values:

```text
keyword-search
llm-search
url-fetch
```

Do not derive names from class repr. Capture monotonic start immediately after binding the request context and before the first event.

- [ ] **Step 5: Implement lifecycle logging around existing dispatch semantics**

Inside the reserved/bound business request block:

```text
log workflow_started
if shutting_down:
  log workflow_rejected
  return existing ErrorResponse
register active task
try invoke workflow
except CancelledError:
  log workflow_cancelled
  raise
except GatewayError:
  log workflow_failed without exc_info
  return unchanged ErrorResponse
except Exception:
  if debug:
    log workflow_failed with exc_info=True and error_type only
  else:
    preserve concise logger.error(type only)
  return existing protocol_error/Internal daemon error
else:
  log workflow_completed
  return SuccessResponse
finally:
  remove active task
```

The outer context managers perform context reset and reservation release.

- [ ] **Step 6: Verify GREEN and unchanged public responses**

Run:

```bash
uv run pytest tests/daemon -v
uv run pytest tests/cli/test_cli.py tests/acceptance/test_gateway_workflows.py -q
```

Expected: all pass; stdout/socket assertions remain exact.

- [ ] **Step 7: Refactor/check**

Keep lifecycle logging in one helper rather than duplicating three request branches. Run:

```bash
uv run ruff check src/agent_search_gateway/daemon.py tests/daemon
uv run mypy src/agent_search_gateway/daemon.py tests/daemon
```

Expected: both pass.

- [ ] **Step 8: Commit**

```bash
git add src/agent_search_gateway/daemon.py tests/daemon
git commit -m "feat: log daemon request lifecycle"
```

---

### Task 7: Trace Retry Attempts, HTTP Boundaries, Provider Quotas, and Runtime Assembly

**Files:**

- Modify: `src/agent_search_gateway/retry.py`
- Modify: `src/agent_search_gateway/concurrency.py`
- Modify: `src/agent_search_gateway/providers/http.py`
- Modify: `src/agent_search_gateway/providers/openai_chat.py`
- Modify: `src/agent_search_gateway/runtime.py`
- Modify: `tests/unit/test_retry.py`
- Modify: `tests/runtime/test_quota_manager.py`
- Modify: `tests/providers/test_http_executor.py`
- Modify: `tests/providers/test_openai_chat.py`
- Modify: `tests/runtime/test_runtime_assembly.py`

- [ ] **Step 1: Add backward-compatible retry-hook tests**

Extend `retry_async` tests to cover optional callbacks:

```python
before_attempt(attempt: int)
on_retry(attempt: int, exc: BaseException, delay: float)
```

Assert calls occur in exact order for two failures then success, non-retryable errors produce no retry callback, cancellation is re-raised, and existing callers that provide no hooks retain current behavior.

- [ ] **Step 2: Add quota event tests**

Use an in-memory debug handler and controlled gates. Assert:

- immediate acquisition emits `quota_acquired`/`quota_released` with provider, kind, in-use and limit metadata;
- a saturated gate emits `quota_waiting` once before acquisition;
- `try_lease()` failure is observable without altering capacity;
- cancellation releases an acquired lease and never exceeds the configured limit;
- gates constructed directly without provider metadata still work and need not emit project events.

- [ ] **Step 3: Expand HTTP executor tests before implementation**

Using `MockTransport`, fake sleep and a deterministic clock, assert:

- each HTTP attempt has provider/stage/endpoint/attempt metadata;
- success logs status and elapsed time;
- 408/429/5xx and transport failure log concise retry metadata with correct attempt/delay;
- terminal 4xx and exhausted retry preserve existing failure taxonomy;
- invalid JSON remains `ProtocolFailure` and never logs response text;
- distinctive Authorization/body sentinels never appear;
- no raw `httpx.Request` object is attached to log fields.

- [ ] **Step 4: Expand OpenAI adapter tests**

Assert outer protocol retries emit model, attempt and `extra_body` **keys only**, while message contents/model responses/secret values remain absent. Quota semantics and parsed return values must remain unchanged.

- [ ] **Step 5: Run focused tests and confirm RED**

Run:

```bash
uv run pytest tests/unit/test_retry.py tests/runtime/test_quota_manager.py tests/providers/test_http_executor.py tests/providers/test_openai_chat.py -v
```

Expected:

```text
FAIL because retry hooks and detailed quota/HTTP/LLM adapter events are missing
```

- [ ] **Step 6: Add optional retry hooks without changing retry policy**

Modify `retry_async` only to invoke `before_attempt` at the start of each loop and `on_retry` after computing delay but before sleeping. Do not add jitter, Retry-After support or provider-specific retry policies.

- [ ] **Step 7: Give quota gates optional operational metadata**

Extend `CapacityGate` with optional `provider`, `quota_kind`, `logger` and monotonic clock fields. `ProviderQuotaManager` supplies metadata for its web/LLM gates; direct test/client construction remains source-compatible through defaults. Emit DEBUG events only through `log_event`.

- [ ] **Step 8: Instrument HttpJsonExecutor at the semantic HTTP boundary**

For each attempt:

```text
log http_attempt_started before request
on response: status + elapsed
on retryable status/transport: preserve WARNING severity and emit http_retrying
on terminal status/transport/decode failure: emit http_failed with category only
```

Log endpoint URL, never headers/json body/response body. Keep existing exception mapping and retry counts.

- [ ] **Step 9: Instrument OpenAI protocol retries without exposing messages**

Use retry hooks around the existing response-shape/JSON retry loop. Safe fields are provider, generic transport stage `llm`, model, attempt, message count, aggregate input character count, output character count, and sorted `extra_body` keys. Never log message contents, decoded model content or Authorization value.

- [ ] **Step 10: Add one runtime assembly summary event**

After successful `Runtime.build`, emit a debug event with enabled provider names/counts and quota limits. Do not include config tables, endpoint secrets or `SecretValue.reveal()` output.

- [ ] **Step 11: Verify GREEN and business-semantics regression**

Run:

```bash
uv run pytest tests/unit/test_retry.py tests/runtime/test_quota_manager.py tests/providers/test_http_executor.py tests/providers/test_openai_chat.py tests/runtime/test_runtime_assembly.py -v
uv run pytest tests/providers tests/runtime -q
```

Expected: all pass.

- [ ] **Step 12: Refactor/check**

Run:

```bash
uv run ruff check src/agent_search_gateway/retry.py src/agent_search_gateway/concurrency.py src/agent_search_gateway/providers/http.py src/agent_search_gateway/providers/openai_chat.py src/agent_search_gateway/runtime.py tests/unit/test_retry.py tests/runtime tests/providers/test_http_executor.py tests/providers/test_openai_chat.py
uv run mypy src tests
```

Expected: both pass.

- [ ] **Step 13: Commit**

```bash
git add src/agent_search_gateway/retry.py src/agent_search_gateway/concurrency.py src/agent_search_gateway/providers/http.py src/agent_search_gateway/providers/openai_chat.py src/agent_search_gateway/runtime.py tests
git commit -m "feat: trace transport retries and quotas"
```

---

### Task 8: Add Semantic LLM Stage Events Without Logging Prompts or Outputs

**Files:**

- Modify: `src/agent_search_gateway/llm/stages.py`
- Modify: `src/agent_search_gateway/observability.py`
- Modify: `tests/unit/test_llm_judge_stage.py`
- Modify: `tests/unit/test_llm_stages.py`
- Modify: `tests/providers/test_openai_chat.py`

- [ ] **Step 1: Add stage event tests first**

For `judge`, `safety`, `content_clean`, `focus_summary`, and `llm_search_markdown`, assert:

- start event includes semantic `stage`, provider and model;
- safe input/output character counts are present;
- completion includes `ok` plus a normalized short reason for decision stages, or output length for text stages;
- elapsed time is non-negative;
- client/parse failure emits `llm_stage_failed` with error type/category but no raw response;
- cancellation emits `llm_stage_cancelled` and is re-raised.

- [ ] **Step 2: Add explicit payload-sentinel assertions**

Use distinct values:

```text
[TEST_USER_BODY]
[TEST_PAGE_BODY]
[TEST_FOCUS_BODY]
[TEST_MODEL_BODY]
```

Place them in prompt, content, focus and fake model output. Assert none appear in logs while lengths/provider/model/stage do appear.

- [ ] **Step 3: Add decision-reason normalization tests**

A reason containing newline, tabs and a long suffix must become one short field. Add a small `normalize_log_reason(value, max_chars=160)` helper in observability and test truncation/escaping. The helper must not be used to make arbitrary body logging acceptable.

- [ ] **Step 4: Run focused tests and confirm RED**

Run:

```bash
uv run pytest tests/unit/test_llm_judge_stage.py tests/unit/test_llm_stages.py -v
```

Expected:

```text
FAIL because LLMStages does not emit semantic stage events
```

- [ ] **Step 5: Add one shared stage wrapper pattern**

Add optional `logger` and monotonic clock constructor arguments to `LLMStages` with production defaults. Implement small private helpers for:

```text
stage start
stage completion
stage failure/cancellation
elapsed_ms
```

Keep prompt builders and output validators unchanged. Do not create a general workflow framework.

- [ ] **Step 6: Instrument each public stage at its existing boundary**

Use only:

- provider/model/stage;
- input and output character counts;
- focus-present/focus-character count, never focus text;
- decision boolean and normalized reason;
- elapsed/error type.

Do not log `messages`, payload mappings or returned text.

- [ ] **Step 7: Verify GREEN and unchanged LLM semantics**

Run:

```bash
uv run pytest tests/unit/test_llm_judge_stage.py tests/unit/test_llm_stages.py tests/providers/test_openai_chat.py -v
uv run pytest tests/unit tests/providers -q
```

Expected: all pass.

- [ ] **Step 8: Refactor/check**

Run:

```bash
uv run ruff check src/agent_search_gateway/llm/stages.py src/agent_search_gateway/observability.py tests/unit/test_llm_judge_stage.py tests/unit/test_llm_stages.py
uv run mypy src/agent_search_gateway/llm/stages.py src/agent_search_gateway/observability.py tests/unit/test_llm_judge_stage.py tests/unit/test_llm_stages.py
```

Expected: both pass.

- [ ] **Step 9: Commit**

```bash
git add src/agent_search_gateway/llm/stages.py src/agent_search_gateway/observability.py tests/unit/test_llm_judge_stage.py tests/unit/test_llm_stages.py tests/providers/test_openai_chat.py
git commit -m "feat: trace llm stages"
```

---

### Task 9: Trace Keyword and LLM Search Pipelines, Candidate Decisions, and Result Persistence

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/search.py`
- Modify: `tests/orchestrators/test_keyword_search_pipeline.py`
- Modify: `tests/orchestrators/test_keyword_search_state.py`
- Modify: `tests/orchestrators/test_llm_search.py`
- Modify: `tests/support/fakes.py`

- [ ] **Step 1: Add keyword-provider timeline tests**

Using existing fake/controlled providers under a bound request ID, cover:

- provider start/completion with configured provider name, stage `search`, hit count and elapsed time;
- partial typed provider failure while another succeeds;
- invalid provider return type/field type categorized as provider failure without logging offending body;
- all-provider failure summary remains the existing `ALL_PROVIDERS_FAILED` behavior.

- [ ] **Step 2: Add per-hit decision tests**

For each branch in `_stage_keyword_hit`, assert a URL-scoped event:

- empty title/snippet -> `candidate_rejected reason=empty_abstract`;
- valid URL/no body -> `candidate_accepted` plus `body_skipped reason=no_body`;
- stored unavailable URL -> body skipped without judge invocation;
- whitespace body -> `body_rejected reason=cheap_check`;
- judge `ok=false` -> `body_rejected reason=judge_rejected` plus normalized decision reason;
- judge accepted -> `body_accepted` with raw/content character counts;
- duplicate/admission keeps deterministic first-write behavior and records a dedup/admission reason without changing output order.

Use a URL containing `?id=42&mode=test` and assert it appears in full.

- [ ] **Step 3: Add LLM-search pipeline tests**

Assert each invocation logs provider/model/stage, output length and parsed-record count, while malformed restricted markdown logs a concise provider failure without raw model output. Partial invocation failure remains successful when another completes.

- [ ] **Step 4: Add persistence and payload-safety assertions**

Assert `results_written` contains kind, absolute path, record count and the bound request ID in the filename. Inject query/prompt/page/model sentinels and assert none appear in logs. JSONL remains exactly `url`/`abstract`.

- [ ] **Step 5: Run focused tests and confirm RED**

Run:

```bash
uv run pytest tests/orchestrators/test_keyword_search_pipeline.py tests/orchestrators/test_keyword_search_state.py tests/orchestrators/test_llm_search.py -v
```

Expected:

```text
FAIL because SearchOrchestrator does not emit provider/candidate/result events
```

- [ ] **Step 6: Add optional logger/clock dependencies and focused helpers**

Extend `SearchOrchestrator.__init__` with optional logger/monotonic defaults. Add private helpers only for elapsed calculation and candidate event emission; keep existing pipeline/commit code structure.

- [ ] **Step 7: Instrument keyword and LLM paths without altering completion rules**

Log around provider/invocation execution, each semantic branch, commit/dedup and final ResultWriter call. Preserve:

- concurrent pipeline execution;
- configured-order commit;
- whole-provider rollback on judge execution failure;
- success for completed empty pipeline;
- existing URLStore first-non-empty behavior.

- [ ] **Step 8: Verify GREEN and no output drift**

Run:

```bash
uv run pytest tests/orchestrators -v
uv run pytest tests/acceptance/test_gateway_workflows.py -v
```

Expected: all pass; result files and response strings are unchanged apart from deterministic request-ID names already introduced.

- [ ] **Step 9: Refactor/check**

Run:

```bash
uv run ruff check src/agent_search_gateway/orchestrators/search.py tests/orchestrators tests/support/fakes.py
uv run mypy src/agent_search_gateway/orchestrators/search.py tests/orchestrators tests/support/fakes.py
```

Expected: both pass.

- [ ] **Step 10: Commit**

```bash
git add src/agent_search_gateway/orchestrators/search.py tests/orchestrators tests/support/fakes.py
git commit -m "feat: trace search pipelines"
```

---

### Task 10: Trace Fetch Singleflight, URL Locks, Scheduler Fallback, and State Decisions

**Files:**

- Modify: `src/agent_search_gateway/concurrency.py`
- Modify: `src/agent_search_gateway/scheduler/fetch.py`
- Modify: `src/agent_search_gateway/orchestrators/fetch.py`
- Modify: `tests/runtime/test_singleflight.py`
- Modify: `tests/scheduler/test_fetch_capacity.py`
- Modify: `tests/scheduler/test_fetch_outcomes.py`
- Modify: `tests/orchestrators/test_url_fetch_admission.py`
- Modify: `tests/orchestrators/test_url_fetch_flow.py`
- Modify: `tests/orchestrators/test_url_fetch_singleflight.py`

- [ ] **Step 1: Add backward-compatible singleflight role tests**

Extend `SingleflightGroup.do` tests for optional synchronous callbacks:

```python
on_leader: Callable[[], None] | None
on_follower: Callable[[], None] | None
```

Assert the leader callback runs once in the leader caller context, each follower callback runs in that follower context, the factory still executes once, and result/error/cancellation semantics remain unchanged.

- [ ] **Step 2: Add fetch outer-state and lock tests**

Under fixed request contexts, cover events for:

- normalized full URL and focus-present/focus-character count;
- exact-key leader versus follower;
- per-URL lock wait/acquire time;
- URL not admitted, already unavailable, cached content, raw-only, and provider-fetch-required branches;
- no provider failure, semantic unavailable mutation, accepted body merge, safety rejection, focus summary path.

Do not assert or log focus/page content.

- [ ] **Step 3: Add scheduler timeline tests**

Use controlled quotas/providers to prove:

- all candidates busy -> `scheduler_waiting` without wall-clock sleep;
- capacity-available provider -> `provider_selected`;
- execution failure -> `provider_fallback` and next provider attempted;
- cheap-check/judge rejection -> candidate/body rejection metadata and later provider may succeed;
- accepted candidate logs raw/content character counts;
- first success still stops the scheduler;
- one job never runs providers in parallel.

- [ ] **Step 4: Add the critical singleflight-correlation regression**

Run two outer requests with different request IDs but identical `(URL, focus)`:

```text
A becomes leader and blocks inside provider
B joins as follower
release provider
```

Assert:

- A logs `singleflight_leader` and the one physical provider attempt under A's ID;
- B logs `singleflight_joined` and its own daemon completion under B's ID;
- provider work is not duplicated or relabeled under B;
- after completion both contexts reset.

Also prove same URL/different focus requests serialize and different URLs may run concurrently.

- [ ] **Step 5: Run focused tests and confirm RED**

Run:

```bash
uv run pytest tests/runtime/test_singleflight.py tests/scheduler/test_fetch_capacity.py tests/scheduler/test_fetch_outcomes.py tests/orchestrators/test_url_fetch_admission.py tests/orchestrators/test_url_fetch_flow.py tests/orchestrators/test_url_fetch_singleflight.py -v
```

Expected:

```text
FAIL because singleflight roles and detailed fetch/scheduler events are missing
```

- [ ] **Step 6: Add optional role callbacks to SingleflightGroup**

Invoke callbacks only after leader/follower determination and outside the internal guard. Keep them synchronous so they observe the caller's current `ContextVar` and cannot delay/alter shared execution.

- [ ] **Step 7: Instrument FetchScheduler without changing classification**

Add optional logger/clock defaults. Emit wait/selection/attempt/fallback/outcome events around the existing sequential loop. Preserve exact `FetchOutcome` semantics and failure collection.

- [ ] **Step 8: Instrument FetchOrchestrator around existing branches**

Add outer singleflight and URL-lock events, store branch metadata, accepted/unavailable state mutations and final output-size metadata. Rely on Task 8 for content-clean/safety/focus LLM stage details rather than duplicating prompt/output logging.

- [ ] **Step 9: Verify GREEN and concurrency semantics**

Run:

```bash
uv run pytest tests/runtime/test_singleflight.py tests/scheduler tests/orchestrators/test_url_fetch_admission.py tests/orchestrators/test_url_fetch_flow.py tests/orchestrators/test_url_fetch_singleflight.py -v
uv run pytest tests/runtime tests/scheduler tests/orchestrators -q
```

Expected: all pass.

- [ ] **Step 10: Refactor/check**

Run:

```bash
uv run ruff check src/agent_search_gateway/concurrency.py src/agent_search_gateway/scheduler/fetch.py src/agent_search_gateway/orchestrators/fetch.py tests/runtime/test_singleflight.py tests/scheduler tests/orchestrators/test_url_fetch_admission.py tests/orchestrators/test_url_fetch_flow.py tests/orchestrators/test_url_fetch_singleflight.py
uv run mypy src tests
```

Expected: both pass.

- [ ] **Step 11: Commit**

```bash
git add src/agent_search_gateway/concurrency.py src/agent_search_gateway/scheduler/fetch.py src/agent_search_gateway/orchestrators/fetch.py tests/runtime/test_singleflight.py tests/scheduler tests/orchestrators
git commit -m "feat: trace fetch scheduling"
```

---

### Task 11: Extract a Shared, Bounded, Read-Only Unix Socket Probe

**Files:**

- Create: `src/agent_search_gateway/socket_probe.py`
- Create: `tests/unit/test_socket_probe.py`
- Modify: `src/agent_search_gateway/daemon.py`
- Modify: `tests/daemon/test_daemon_dispatch.py`

- [ ] **Step 1: Write socket probe contract tests**

Define expected states and cover with temporary real Unix sockets plus injected connector failures:

```text
missing
live
refused/stale
not_socket
timeout
os_error
```

Assert a live probe opens and closes a local connection but sends zero bytes. Capture `(st_dev, st_ino)` identity for existing socket paths so callers can protect against replacement races.

- [ ] **Step 2: Run the new tests and confirm RED**

Run:

```bash
uv run pytest tests/unit/test_socket_probe.py -v
```

Expected:

```text
FAIL with ModuleNotFoundError because socket_probe.py does not exist
```

- [ ] **Step 3: Implement a typed probe result**

Use a frozen dataclass plus enum, for example:

```python
class SocketState(StrEnum): ...

@dataclass(frozen=True, slots=True)
class SocketProbeResult:
    state: SocketState
    identity: tuple[int, int] | None = None
    reason: str = ""
```

`probe_unix_socket(path, timeout_seconds, connector)` must:

```text
lstat path
return missing if absent
return not_socket if mode is not Unix socket
remember identity
connect under asyncio.wait_for
close writer cleanly without sending data
classify refused, timeout and safe OS errors
```

Do not unlink or repair anything in this module.

- [ ] **Step 4: Refactor daemon startup to consume the probe**

Preserve current behavior:

- missing -> proceed;
- live -> `ConfigFailure` already running;
- refused/stale -> re-`lstat`, compare identity, then unlink only if the same socket remains;
- timeout -> startup failure and preserve path;
- not-socket/OS error -> explicit safe startup failure;
- never unlink a replacement path.

Remove duplicate connection-probe code/constants from `daemon.py` after tests pass.

- [ ] **Step 5: Verify focused GREEN and daemon regressions**

Run:

```bash
uv run pytest tests/unit/test_socket_probe.py tests/daemon/test_daemon_dispatch.py -v
uv run pytest tests/daemon -q
```

Expected: all pass, including live-socket rejection, stale recovery and timeout preservation.

- [ ] **Step 6: Refactor/check**

Run:

```bash
uv run ruff check src/agent_search_gateway/socket_probe.py src/agent_search_gateway/daemon.py tests/unit/test_socket_probe.py tests/daemon/test_daemon_dispatch.py
uv run mypy src/agent_search_gateway/socket_probe.py src/agent_search_gateway/daemon.py tests/unit/test_socket_probe.py tests/daemon/test_daemon_dispatch.py
```

Expected: both pass.

- [ ] **Step 7: Commit**

```bash
git add src/agent_search_gateway/socket_probe.py src/agent_search_gateway/daemon.py tests/unit/test_socket_probe.py tests/daemon/test_daemon_dispatch.py
git commit -m "refactor: share unix socket probing"
```

---

### Task 12: Implement Doctor Configuration and Filesystem Checks Without Persistent Repairs

**Files:**

- Create: `src/agent_search_gateway/doctor.py`
- Create: `tests/doctor/__init__.py`
- Create: `tests/doctor/test_config.py`
- Create: `tests/doctor/test_filesystem.py`
- Create: `tests/doctor/test_no_network.py`

- [ ] **Step 1: Write config diagnostic tests**

Cover:

- missing config -> one `FAIL`, but collection continues;
- malformed TOML -> safe parse `FAIL` and no resolution attempt;
- valid config plus stub environment -> configuration `OK`;
- unknown/unsupported provider, invalid concurrency, missing referenced LLM provider and missing env -> existing resolver message surfaced as safe `FAIL`;
- successful resolution may emit `OK` lines naming required environment variables, but never values;
- distinctive secret values are absent from every check message and repr.

Tests must call the real `load_toml`, `build_default_registry` and `resolve_config` paths rather than reimplementing their rules.

- [ ] **Step 2: Write filesystem diagnostic tests**

Cover each checked location (socket/cache parent, results dir, logs dir):

- existing writable directory -> `OK` after a transient probe file is removed;
- missing but creatable directory -> `OK` without leaving that directory behind;
- nearest existing parent is a regular file -> `FAIL`;
- expected directory already exists as non-directory -> `FAIL`;
- injected probe create/write failure -> `FAIL`;
- injected cleanup failure -> `FAIL`;
- doctor creates no persistent `debug.log`, result file, socket, directory or config change.

Use an injectable `directory_probe` for failures that would be unreliable under root/CI permissions.

- [ ] **Step 3: Write aggregation/no-network tests**

Assert a config failure does not prevent filesystem checks and multiple failures are all returned. Monkeypatch `Runtime.build`, `httpx.AsyncClient` construction and provider factories to raise `AssertionError` if called; a valid static doctor collection must still complete.

- [ ] **Step 4: Run doctor tests and confirm RED**

Run:

```bash
uv run pytest tests/doctor/test_config.py tests/doctor/test_filesystem.py tests/doctor/test_no_network.py -v
```

Expected:

```text
FAIL with ModuleNotFoundError because doctor.py does not exist
```

- [ ] **Step 5: Implement check/report value types**

Add `DoctorStatus`, `DoctorCheck`, and `DoctorReport.exit_code` (`1` iff any status is `FAIL`, otherwise `0`). Keep messages as already-sanitized human text; no secrets or raw exception repr.

- [ ] **Step 6: Implement configuration checks through existing sources of truth**

```text
if config missing:
  add fail and continue
else:
  load_toml
  resolve_config(data, build_default_registry(), environ)
  on ConfigFailure:
    add fail with safe message
  on success:
    add configuration ok
    collect unique api_key_env names from resolved web/LLM configs
    add env-name-only ok lines
```

Do not build runtime or clients.

- [ ] **Step 7: Implement non-persistent directory writability checks**

For an existing directory, create an exclusive uniquely named probe file, close it and remove it immediately. For a missing target, walk to the nearest existing parent, reject wrong types, and probe only that existing parent; do not `mkdir` the target. Cleanup failure is a failing check.

- [ ] **Step 8: Verify GREEN and side-effect freedom**

Run:

```bash
uv run pytest tests/doctor/test_config.py tests/doctor/test_filesystem.py tests/doctor/test_no_network.py -v
uv run ruff check src/agent_search_gateway/doctor.py tests/doctor
uv run mypy src/agent_search_gateway/doctor.py tests/doctor
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/agent_search_gateway/doctor.py tests/doctor
git commit -m "feat: add doctor static checks"
```

---

### Task 13: Add Doctor Socket Diagnostics, Rendering, Exit Codes, and CLI Routing

**Files:**

- Modify: `src/agent_search_gateway/doctor.py`
- Modify: `src/agent_search_gateway/cli.py`
- Create: `tests/doctor/test_socket.py`
- Create: `tests/doctor/test_rendering.py`
- Modify: `tests/cli/test_cli.py`
- Modify: `tests/docs/test_documented_config.py`

- [ ] **Step 1: Write doctor socket tests**

Using the shared `probe_unix_socket`, cover:

- missing -> `[info] daemon not running`, non-failing;
- live local server -> `[ok] daemon running`;
- stale/refused -> `[fail]` and path remains untouched;
- timeout -> `[fail]` quickly, no long sleep;
- regular file -> `[fail]` not a Unix socket;
- permission/other OS error -> `[fail]` with safe reason;
- probe sends no business request and does not mutate URL state.

- [ ] **Step 2: Write rendering/exit tests**

Given deterministic checks, assert exact prefixes and one check per line:

```text
[ok] ...
[info] ...
[fail] ...
```

Assert exit 0 for only OK/INFO and exit 1 for any FAIL. Newline-containing reasons must be normalized to one line.

- [ ] **Step 3: Extend CLI tests before implementation**

Assert:

- parser includes `doctor` and help includes `start --debug`;
- `doctor` takes no daemon arguments and rejects `--debug`;
- doctor branch invokes an injected local runner and never invokes socket `client` or daemon factory;
- all checks render to stdout and return report exit code;
- unexpected doctor implementation exception prints one concise `[fail] doctor internal error` to stderr and exits 1 without traceback;
- existing business command stdout/stderr remains unchanged.

- [ ] **Step 4: Run focused tests and confirm RED**

Run:

```bash
uv run pytest tests/doctor/test_socket.py tests/doctor/test_rendering.py tests/cli/test_cli.py -v
```

Expected:

```text
FAIL because doctor socket aggregation/rendering and CLI command do not exist
```

- [ ] **Step 5: Complete `run_doctor` with socket aggregation**

After config/filesystem checks, call the injected/shared socket probe and map its typed state to one `DoctorCheck`. Never unlink or repair the socket. Return a `DoctorReport` containing all checks in a deterministic order.

- [ ] **Step 6: Implement one-line rendering**

`render_doctor` maps statuses to lowercase bracketed prefixes and writes exactly one physical line per check. Sanitize OS reasons through the existing one-line utility; do not include traceback or secrets.

- [ ] **Step 7: Route doctor locally in CLI**

Add parser choice `doctor`. In `run_command`, handle it before request construction/socket client use:

```text
report = await doctor_runner(paths, environ=...)
render_doctor(report, stdout)
return report.exit_code
```

Inject runner/environment in tests; production defaults to current `os.environ`. Catch only unexpected implementation exceptions at the CLI boundary and return the documented concise internal failure.

- [ ] **Step 8: Update command/documentation contract test minimally**

Change the expected parser subcommand set from five to six and include `doctor`. Do not yet add README installation assertions; those belong to Task 14.

- [ ] **Step 9: Verify GREEN and complete CLI regression**

Run:

```bash
uv run pytest tests/doctor tests/cli/test_cli.py tests/docs/test_documented_config.py -v
uv run pytest tests/cli tests/daemon -q
```

Expected: all pass.

- [ ] **Step 10: Refactor/check**

Run:

```bash
uv run ruff check src/agent_search_gateway/doctor.py src/agent_search_gateway/cli.py tests/doctor tests/cli/test_cli.py tests/docs/test_documented_config.py
uv run mypy src tests
```

Expected: both pass.

- [ ] **Step 11: Commit**

```bash
git add src/agent_search_gateway/doctor.py src/agent_search_gateway/cli.py tests/doctor tests/cli/test_cli.py tests/docs/test_documented_config.py
git commit -m "feat: expose doctor command"
```

---

### Task 14: Document `uv tool install .`, Preserve Locked Development, and Add Isolated Install Smoke Coverage

**Files:**

- Modify: `README.md`
- Modify: `tests/docs/test_documented_config.py`
- Create: `tests/docs/test_installation_contract.py`
- Modify: `.github/workflows/ci.yml`
- Inspect only: `pyproject.toml`

- [ ] **Step 1: Write documentation contract tests first**

Create assertions that README contains an end-user install section with:

```bash
uv tool install .
agent-search-gateway doctor
agent-search-gateway start
agent-search-gateway start --debug
```

Also assert a clearly separate development section still contains:

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

- [ ] **Step 2: Add debug documentation assertions**

Assert README documents:

- `~/.cache/agent-search-gateway-cli/logs/debug.log`;
- 5 MiB current file and 3 backups;
- full target URLs may be persisted and logs should be treated as sensitive local artifacts;
- query/prompt/page/model-response bodies and authentication values are not intentionally logged;
- business command stdout remains final-output-only;
- doctor is local/no-network and daemon-not-running is informational.

- [ ] **Step 3: Add CI workflow contract assertions**

Read `.github/workflows/ci.yml` and assert:

- the normal verification job still contains `uv sync --locked`, ruff, mypy and pytest;
- tool installation is a separate smoke step using temporary `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR`;
- CI does not install the tool into a global/user directory.

- [ ] **Step 4: Run docs tests and confirm RED**

Run:

```bash
uv run pytest tests/docs/test_documented_config.py tests/docs/test_installation_contract.py -v
```

Expected:

```text
FAIL because README still presents uv sync --locked as the user install flow and lacks debug/doctor documentation
```

- [ ] **Step 5: Rewrite README installation versus development sections**

Document this order for end users:

```text
clone repository
cd repository
uv tool install .
copy/edit config and export required environment variables
agent-search-gateway doctor
agent-search-gateway start [--debug]
```

Explain that `uv tool install .` creates an isolated CLI runtime and does not replace the lockfile-driven development environment. Do not imply that tool install consumes `uv.lock` as its runtime environment.

- [ ] **Step 6: Document doctor and debug operational behavior**

Include command examples, log location/rotation, session/request correlation, result filename correlation, payload exclusions, full-URL warning and final-output-only stdout. Keep limitations aligned with the design; do not promise live provider checks or automatic fixes.

- [ ] **Step 7: Add a separate isolated CI smoke step**

Keep the existing locked verify steps unchanged, then add:

```yaml
- name: Smoke-test uv tool installation
  env:
    UV_TOOL_DIR: ${{ runner.temp }}/agent-search-gateway-tools
    UV_TOOL_BIN_DIR: ${{ runner.temp }}/agent-search-gateway-bin
  run: |
    uv tool install .
    "$UV_TOOL_BIN_DIR/agent-search-gateway" --help
    "$UV_TOOL_BIN_DIR/agent-search-gateway" start --help
```

Optionally grep help for `doctor`/`--debug`; do not run doctor against the CI home config.

- [ ] **Step 8: Verify source metadata already supports tool install**

Inspect `[project.scripts]` and package discovery in `pyproject.toml`. Modify it only if the installed executable or new modules are not included; do not add runtime dependencies for standard-library logging/doctor code.

- [ ] **Step 9: Run docs tests and an isolated local smoke test**

Run:

```bash
uv run pytest tests/docs -v

tool_dir="$(mktemp -d)"
bin_dir="$(mktemp -d)"
UV_TOOL_DIR="$tool_dir" UV_TOOL_BIN_DIR="$bin_dir" uv tool install .
"$bin_dir/agent-search-gateway" --help
"$bin_dir/agent-search-gateway" start --help
rm -rf "$tool_dir" "$bin_dir"
```

Expected:

```text
Docs tests PASS; isolated install succeeds; installed help exposes doctor and start --debug; no global tool directory is modified
```

- [ ] **Step 10: Refactor/check**

Run:

```bash
uv run ruff check tests/docs
uv run mypy tests/docs
```

Expected: both pass.

- [ ] **Step 11: Commit**

```bash
git add README.md tests/docs .github/workflows/ci.yml pyproject.toml
git commit -m "docs: document uv tool installation"
```

If `pyproject.toml` is unchanged, omit it from `git add`.

---

### Task 15: Add No-Network Debug/Doctor Acceptance Coverage and Run the Final Gate

**Files:**

- Create: `tests/acceptance/test_debug_and_doctor.py`
- Modify: `tests/support/acceptance.py`
- Modify: `tests/acceptance/test_gateway_workflows.py` only if shared helpers are extracted
- Modify: `tests/conftest.py` only for reusable temporary logging fixtures

- [ ] **Step 1: Write the end-to-end debug workflow first**

Use real CLI parsing/run-command wiring, a real temporary Unix socket, a real `ForegroundDaemon`, real orchestrators/store/result writer, and fake no-network providers/LLM. Inject a fixed request ID `a1b2c3d4`.

Scenario:

```text
start daemon with start --debug in a background task
wait for daemon.ready
send keyword-search over the real protocol
provider returns two hits:
  one body accepted by judge
  one body rejected by judge but URL retained
receive keyword-a1b2c3d4.jsonl
send stop
await start task
```

- [ ] **Step 2: Assert cross-layer correlation and public-contract preservation**

Assert:

- socket response remains `SuccessResponse(path)`;
- CLI-equivalent business stdout is only the absolute path;
- filename token equals the request ID in log events;
- debug log has session markers, workflow lifecycle, provider activity, both body decisions and `results_written`;
- JSONL contains only `url` and `abstract`;
- full test URL including query appears;
- query/page/model/credential sentinels do not appear;
- `httpx`/`httpcore` low-level DEBUG noise does not appear;
- normal mode equivalent run creates no debug log artifact.

- [ ] **Step 3: Write a doctor acceptance scenario**

With a real temporary valid config, stub environment, writable paths and no daemon socket:

```text
run agent-search-gateway doctor through run_command
expect config/filesystem OK lines
expect [info] daemon not running
expect exit 0
assert no persistent probe/debug/result/socket artifacts
```

Add a failing config variant and assert independent filesystem/socket lines still render and exit becomes 1.

- [ ] **Step 4: Run acceptance tests and confirm RED**

Run:

```bash
uv run pytest tests/acceptance/test_debug_and_doctor.py -v
```

Expected:

```text
FAIL at the first missing end-to-end correlation, log-safety, or doctor integration boundary
```

- [ ] **Step 5: Make only minimal integration fixes**

Fix wiring exposed by acceptance tests in the owning production modules. Do not add acceptance-only flags, fake providers or test modes to `src/`.

- [ ] **Step 6: Run all targeted feature tests**

Run:

```bash
uv run pytest tests/unit/test_request_ids.py tests/unit/test_observability_logging.py tests/unit/test_socket_probe.py -v
uv run pytest tests/daemon tests/orchestrators tests/scheduler tests/providers tests/doctor tests/docs tests/acceptance -q
```

Expected: all pass with no real network.

- [ ] **Step 7: Run the complete development verification gate**

Run:

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

Expected:

```text
Locked sync succeeds; lint, type check, and all default tests PASS; live integrations remain skipped unless explicitly enabled
```

- [ ] **Step 8: Repeat isolated tool-install smoke after the full suite**

Run:

```bash
tool_dir="$(mktemp -d)"
bin_dir="$(mktemp -d)"
UV_TOOL_DIR="$tool_dir" UV_TOOL_BIN_DIR="$bin_dir" uv tool install .
"$bin_dir/agent-search-gateway" --help
"$bin_dir/agent-search-gateway" start --help
rm -rf "$tool_dir" "$bin_dir"
```

Expected: installed CLI starts, help exposes `doctor` and `--debug`, and no development dependency is needed by the executable.

- [ ] **Step 9: Review generated artifacts and privacy assertions**

Inspect only temporary test outputs and confirm:

```text
normal mode: no debug.log
debug mode: debug.log plus at most .1/.2/.3 backups
no prompt/page/model/credential sentinel in any retained log
result JSONL schema unchanged
doctor left no repair artifacts
```

- [ ] **Step 10: Commit**

```bash
git add tests/acceptance tests/support/acceptance.py tests/conftest.py
git commit -m "test: cover debug and doctor workflows"
```

Omit unchanged files from `git add`.

---

## Self-Review

### Spec Coverage

| Design area | Plan tasks |
|---|---|
| Runtime log paths and normal-mode non-creation | 1, 4, 5, 15 |
| 8-hex request ID generation, active/file collision handling, cleanup | 2, 3, 6 |
| Search filename token equals request ID; JSONL schema unchanged | 3, 9, 15 |
| Structured one-line logs, stderr + 5 MiB/3-backup rotation | 4, 5, 15 |
| Bootstrap fail-closed and post-start sink fail-open for business logic | 4, 5 |
| Secret/traceback redaction and body exclusion | 4, 6–10, 15 |
| Expected failure versus unexpected traceback policy | 6–10 |
| Provider/HTTP/retry/quota/runtime tracing | 7 |
| Semantic LLM stage tracing | 8 |
| Keyword/LLM search provider and per-candidate tracing | 9 |
| Fetch scheduler, fallback, candidate, lock and singleflight tracing | 10 |
| Shared bounded socket inspection | 11 |
| Doctor config/environment/filesystem aggregation | 12 |
| Doctor socket states, rendering, exit code and local CLI route | 13 |
| `uv tool install .` user flow with locked dev/CI preserved | 14 |
| End-to-end no-network acceptance and final verification | 15 |

### Ordering and Dependency Review

- Paths precede logging bootstrap.
- Request IDs/context precede result naming and all request-scoped logging.
- Result-ID ownership is complete before instrumentation, so later logs can rely on one stable correlation key.
- Logging formatter/handlers precede daemon/provider/stage event emission.
- Transport and LLM instrumentation precede orchestrator acceptance tests that expect nested events.
- Shared socket probe is extracted before doctor consumes it, preventing daemon/doctor drift.
- Doctor core checks precede CLI exposure.
- README/CI changes occur only after commands exist and can be smoke-tested.
- Acceptance is last and uses real local boundaries without adding production test modes.

### Type and Contract Consistency

- `request_id` is ambient only for diagnostics and explicit for functional result naming.
- `SearchOrchestrator` is the only business API changed to accept explicit search request IDs; provider contracts remain unchanged.
- `ResultWriter` never generates or silently replaces a token.
- `URLFetchRequest` does not gain an ID field; its context remains daemon-local.
- `SuccessResponse`, `ErrorResponse`, `ErrorCode`, NDJSON framing and JSONL fields stay unchanged.
- Semantic rejection remains `StageDecision(ok=False)` / `FetchOutcome("semantic_failure")`; DEBUG does not reclassify it as execution failure.
- Doctor reuses `resolve_config` and `probe_unix_socket`; it does not duplicate provider validation or mutate socket/filesystem state.
- Normal mode remains independent of the debug log path.
- End-user tool installation is separate from lockfile-driven development and CI.

### Final Implementation Gate

Implementation is complete only after all of the following pass:

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

And the isolated distribution smoke check passes without modifying the user's normal uv tool directories:

```bash
tool_dir="$(mktemp -d)"
bin_dir="$(mktemp -d)"
UV_TOOL_DIR="$tool_dir" UV_TOOL_BIN_DIR="$bin_dir" uv tool install .
"$bin_dir/agent-search-gateway" --help
"$bin_dir/agent-search-gateway" start --help
rm -rf "$tool_dir" "$bin_dir"
```
