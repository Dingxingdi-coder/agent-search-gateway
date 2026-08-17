# Agent Search Gateway CLI v0 Implementation Plan

**Goal:** 构建一个 Python 前台 daemon 与薄 CLI，通过 Unix domain socket 聚合多家搜索/抓取提供商和 OpenAI-compatible LLM，严格实现五字段 URL 状态、错误分类、并发配额、同 URL singleflight 与可测试的优雅停机。

**Architecture:** `docs/designs/architectures/agent-search-gateway-cli-v0.md`

**Error handling:** `docs/designs/error-handlings/agent-search-gateway-cli-v0.md`

**Testing:** `docs/designs/testings/agent-search-gateway-cli-v0.md`

---

## Implementation Boundaries

本计划只覆盖设计文档定义的 v0。不要加入自动启动 daemon、持久化 URL 状态、任意 URL 直接抓取、运行时 LLM failover、MCP transport、流式进度、`--json` 输出、插件动态加载或查询级缓存。

实现基线：

- Python `>=3.11`，使用 `uv` 管理环境与锁文件。
- 运行时依赖仅使用标准库与 `httpx`；测试依赖使用 `pytest`、`pytest-asyncio`、`ruff`、`mypy`。
- 默认测试不得访问真实网络；HTTP 测试使用 `httpx.MockTransport`，provider 测试使用脱敏 fixture。
- 公开 socket、配置、URL object 和结果 jsonl 契约按设计文档实现，不通过内部对象泄漏 provider-specific 字段。
- 所有 provider pipeline 并发执行，但结果按配置顺序提交到 URL Store，从而让 first-non-empty-wins 可重复、可测试。
- Provider adapter 只返回 candidate，不得导入或调用 `URLStore`。

为消除现有设计中的实现歧义，本计划锁定以下最小决策：

1. `cheap_check` 只拒绝空字符串或纯空白；不加入长度、HTML 标签或关键词启发式规则。
2. `available=false` 的 URL 在后续搜索中仍可输出，但跳过 provider body validation，且不再写入 `raw_content`/`content`；`abstract` 仍遵守 first-non-empty-wins。
3. Search provider 同时执行，pipeline 先在内存中形成完整结果；只有 pipeline 全部步骤成功后才原子式提交该 pipeline 的 records。任一 hit 的 judge 执行失败会丢弃该 provider pipeline 的全部暂存结果。
4. LLM 配置按字段继承；`extra_body` 是一个整体 mapping，子级存在时替换父级，不做 deep merge，并在解析时复制，避免不同 invocation 共享可变对象。
5. 增加可选顶层 `[retry]` 表，字段为 `max_attempts`、`base_delay_seconds`、`max_delay_seconds`、`request_timeout_seconds`；缺省值分别为 `3`、`0.25`、`2.0`、`30.0`。这是满足“configurable retry”的最小配置扩展。
6. 同一 `(normalized_url, normalized_focus)` 的并发请求共享完整结果或异常；同 URL 不同 focus 使用 per-URL lock 串行执行，因此各自生成 request-local summary，但不会并行执行 body preparation、safety 或 focus-summary。
7. Search jsonl 对 normalized URL 去重，保留配置顺序下第一次出现的位置，输出 store 中最终的 `url` 与 `abstract`。
8. 固定内部 shutdown grace timeout 为 `10.0` 秒；可通过构造参数注入测试值，但不暴露 CLI flag 或配置项。
9. Provider-specific TOML 键由对应 adapter factory 校验。通用 loader 只解析共享键；未知 provider-specific 键必须在 daemon startup 时产生 `config_error`，不能静默忽略。

## Planned File Structure

```text
.
├── .github/workflows/ci.yml
├── .gitignore
├── .python-version
├── README.md
├── config.example.toml
├── pyproject.toml
├── uv.lock
├── src/agent_search_gateway/
│   ├── __init__.py
│   ├── cli.py
│   ├── concurrency.py
│   ├── config.py
│   ├── daemon.py
│   ├── errors.py
│   ├── models.py
│   ├── observability.py
│   ├── paths.py
│   ├── protocol.py
│   ├── result_writer.py
│   ├── retry.py
│   ├── runtime.py
│   ├── url_normalization.py
│   ├── url_store.py
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── markdown_parser.py
│   │   ├── prompts.py
│   │   └── stages.py
│   ├── orchestrators/
│   │   ├── __init__.py
│   │   ├── fetch.py
│   │   └── search.py
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── contracts.py
│   │   ├── defaults.py
│   │   ├── http.py
│   │   ├── registry.py
│   │   ├── llm/
│   │   │   ├── __init__.py
│   │   │   └── openai_chat.py
│   │   └── web/
│   │       ├── __init__.py
│   │       ├── anysearch.py
│   │       ├── brave.py
│   │       ├── exa.py
│   │       ├── firecrawl.py
│   │       ├── linkup.py
│   │       ├── tavily.py
│   │       └── tinyfish.py
│   └── scheduler/
│       ├── __init__.py
│       └── fetch.py
└── tests/
    ├── conftest.py
    ├── support/
    │   ├── __init__.py
    │   ├── controlled.py
    │   └── fakes.py
    ├── acceptance/
    ├── cli/
    ├── daemon/
    ├── docs/
    ├── fixtures/providers/
    ├── integration/
    ├── orchestrators/
    ├── providers/
    ├── runtime/
    ├── scheduler/
    └── unit/
```

主要职责：

| File/Area | Responsibility |
|---|---|
| `errors.py` | 稳定 error code、typed execution/input/config/protocol failure 与 unavailable 常量 |
| `models.py` | 五字段 `URLRecord`、公开 search record、socket response 等无副作用数据类型 |
| `config.py` | TOML/env 解析、继承、provider capability 与 secret 校验 |
| `concurrency.py` | provider capacity gate、per-key lock、exact-key singleflight |
| `retry.py` / `providers/http.py` | 可注入 sleep 的指数退避与 JSON HTTP 执行边界 |
| `providers/contracts.py` | keyword/fetch/LLM contracts 和 candidate types |
| `providers/registry.py` / `providers/defaults.py` | capability 元数据、factory 注册与内置 adapter 集合 |
| `llm/*` | prompt、restricted Markdown parser、judge/safety/clean/focus stage |
| `orchestrators/search.py` | keyword/LLM search pipeline 隔离、合并与 result writer 调用 |
| `scheduler/fetch.py` | 单 URL 单 provider 尝试、capacity-aware provider 选择和结果分类 |
| `orchestrators/fetch.py` | admission、URL state、content preparation、safety、focus 与 singleflight |
| `protocol.py` | NDJSON codec、typed request/response validation、Unix socket client |
| `daemon.py` | socket server、dispatch、active request tracking、shutdown lifecycle |
| `cli.py` | `argparse`、stdout/stderr/exit code 和 foreground start |
| `tests/support/*` | 真实 contract fake 与 event-controlled concurrency helpers；避免 mock-heavy tests |

## Locked Type And Interface Vocabulary

后续任务必须保持以下名字与字段一致：

- `NormalizedURL = NewType("NormalizedURL", str)`
- `URLRecord(url, raw_content, content, abstract, available)`，frozen dataclass，公开字段恰好五个。
- `SearchRecord(url, abstract)`
- `KeywordSearchHit(url, title, snippet, raw_content, content)`
- `URLFetchCandidate(raw_content, content)`
- `StageDecision(ok, reason)`；semantic rejection 由 `ok=False` 表达，不抛异常。
- `LLMInvocation(provider, model, extra_body)`
- `RetryPolicy(max_attempts, base_delay_seconds, max_delay_seconds, request_timeout_seconds)`
- `FetchOutcome(kind, candidate, failures)`，`kind` 只允许 `accepted`、`semantic_failure`、`execution_failure`。
- `SuccessResponse(ok=True, text)` 与 `ErrorResponse(ok=False, error, message)`。
- `KeywordSearchProvider.search(query)`、`URLFetchProvider.fetch(url)`、`LLMClient.complete_text(...)`、`LLMClient.complete_json(...)` 全部为 async contract。
- `SearchOrchestrator.keyword_search(query)`、`SearchOrchestrator.llm_search(prompt)`、`FetchOrchestrator.url_fetch(url, focus)` 成功时返回用户可见文本，失败时抛 typed `GatewayError`；只有 daemon boundary 将其转换为 socket response。

### Task 1: Bootstrap Python Package And Test Harness

**Files:**
- Create: `.python-version`
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `src/agent_search_gateway/__init__.py`
- Create: `tests/conftest.py`
- Test: `tests/unit/test_package_metadata.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Python First Version)

- [ ] **Step 1: Write the failing test**

创建 `test_package_exposes_version`：导入 `agent_search_gateway.__version__`，断言值为 `0.1.0`。先建立仅含 pytest 配置和依赖声明的 `pyproject.toml`，不要先写 package implementation。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv sync
uv run pytest tests/unit/test_package_metadata.py::test_package_exposes_version -v
```

Expected:

```text
FAIL with ModuleNotFoundError or missing __version__
```

- [ ] **Step 3: Describe the minimal implementation**

```text
create src-layout package
set __version__ = "0.1.0"
configure pytest asyncio_mode=auto
configure ruff and mypy for Python 3.11+
generate uv.lock with runtime dependency httpx and development dependencies pytest/pytest-asyncio/ruff/mypy
```

不要创建 CLI placeholder、daemon placeholder 或未被测试使用的抽象。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_package_metadata.py::test_package_exposes_version -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

统一 `pyproject.toml` 中 Python target，删除未使用依赖，运行：

```bash
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

Expected:

```text
Lint, type check, and tests PASS
```

### Task 2: Stable Error Taxonomy And User-Facing Constants

**Files:**
- Create: `src/agent_search_gateway/errors.py`
- Test: `tests/unit/test_errors.py`
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (Error Model, Error Codes, Unavailable Message)

- [ ] **Step 1: Write the failing test**

创建 `test_error_contract_contains_exact_codes_and_unavailable_message`，断言 `ErrorCode` 包含设计文档中的全部稳定值，`UNAVAILABLE_MESSAGE` 完全等于指定文本，并验证 `GatewayError` 保留 code/message 而不把 semantic rejection 当异常类别。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_errors.py::test_error_contract_contains_exact_codes_and_unavailable_message -v
```

Expected:

```text
FAIL because errors module and stable codes do not exist
```

- [ ] **Step 3: Describe the minimal implementation**

```text
define ErrorCode(StrEnum):
  BAD_REQUEST, EMPTY_QUERY, INVALID_URL, URL_NOT_ADMITTED
  NO_KEYWORD_SEARCH_PROVIDERS, NO_LLM_SEARCH_PROVIDERS, NO_URL_FETCH_PROVIDERS
  ALL_PROVIDERS_FAILED, LLM_STAGE_FAILED, PROTOCOL_ERROR, CONFIG_ERROR
  DAEMON_SHUTTING_DOWN

define GatewayError(code, message)
define focused subclasses only where boundary handling differs:
  InputFailure, ExecutionFailure, ProtocolFailure, ConfigFailure

define exact UNAVAILABLE_MESSAGE constant
```

不要为 semantic rejection 创建可被误捕获为 execution failure 的异常。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_errors.py::test_error_contract_contains_exact_codes_and_unavailable_message -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

减少重复 message factory，只保留稳定 code 和必要 metadata；运行：

```bash
uv run ruff check src/agent_search_gateway/errors.py tests/unit/test_errors.py
uv run mypy src/agent_search_gateway/errors.py tests/unit/test_errors.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 3: Runtime Paths

**Files:**
- Create: `src/agent_search_gateway/paths.py`
- Test: `tests/unit/test_paths.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Runtime Model, Result Jsonl Files, Config Contract)

- [ ] **Step 1: Write the failing test**

创建 `test_runtime_paths_are_derived_from_home_without_global_mutation`，给定临时 home，断言 config、socket、results 分别位于 `.config/agent-search-gateway-cli/config.toml`、`.cache/agent-search-gateway-cli/daemon.sock`、`.cache/agent-search-gateway-cli/results/`。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_paths.py::test_runtime_paths_are_derived_from_home_without_global_mutation -v
```

Expected:

```text
FAIL because RuntimePaths is not implemented
```

- [ ] **Step 3: Describe the minimal implementation**

```text
RuntimePaths.from_home(home):
  config_file = home / ".config/agent-search-gateway-cli/config.toml"
  socket_file = home / ".cache/agent-search-gateway-cli/daemon.sock"
  results_dir = home / ".cache/agent-search-gateway-cli/results"

RuntimePaths.default():
  delegate to Path.home()
```

不加入 XDG override、PID file 或 log file；测试通过依赖注入隔离 home。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_paths.py::test_runtime_paths_are_derived_from_home_without_global_mutation -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

将路径 dataclass 设为 frozen，避免运行时被意外修改；运行：

```bash
uv run ruff check src/agent_search_gateway/paths.py tests/unit/test_paths.py
uv run mypy src/agent_search_gateway/paths.py tests/unit/test_paths.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 4: URL Normalization Contract

**Files:**
- Create: `src/agent_search_gateway/url_normalization.py`
- Test: `tests/unit/test_url_normalization.py`
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (URL Is Invalid)

- [ ] **Step 1: Write the failing test**

创建参数化测试 `test_normalize_url_enforces_http_contract`：覆盖 trim、HTTP/HTTPS、仅 host 小写、path/query/fragment 原样保留，以及 empty、FTP、mailto、缺 host、畸形 port 的拒绝。错误必须是 `InputFailure(ErrorCode.INVALID_URL, ...)`。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_url_normalization.py::test_normalize_url_enforces_http_contract -v
```

Expected:

```text
FAIL because normalize_url is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
strip input
urlsplit
require scheme in {http, https}
require parsed hostname
lowercase hostname only
preserve userinfo, explicit port, path, query, fragment
urlunsplit
return NormalizedURL
translate ValueError from malformed port/host into INVALID_URL
```

不要删除 fragment、排序 query、补 trailing slash、移除默认 port 或做网络/DNS 检查。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_url_normalization.py::test_normalize_url_enforces_http_contract -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

提取仅用于 netloc 重建的小 helper，保持主流程可读；运行：

```bash
uv run ruff check src/agent_search_gateway/url_normalization.py tests/unit/test_url_normalization.py
uv run mypy src/agent_search_gateway/url_normalization.py tests/unit/test_url_normalization.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 5: Five-Field URL Record And Store State Machine

**Files:**
- Create: `src/agent_search_gateway/models.py`
- Create: `src/agent_search_gateway/url_store.py`
- Test: `tests/unit/test_url_store.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Five-Field URL Object, First Non-Empty Wins)
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (URL State Mutation Rules)

- [ ] **Step 1: Write the failing test**

创建 `test_url_store_preserves_public_shape_and_first_write_state_machine`：断言 `URLRecord` 恰好五字段；只有 non-empty abstract 可创建记录；空字段可后填；非空 `abstract/raw_content/content` 不可覆盖；`mark_unavailable` 单向变化；读取返回不可变 snapshot；focus 不进入 record。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_url_store.py::test_url_store_preserves_public_shape_and_first_write_state_machine -v
```

Expected:

```text
FAIL because URLRecord and URLStore are missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
frozen URLRecord with exact fields:
  url, raw_content="", content="", abstract, available=True

URLStore internal dict[NormalizedURL, URLRecord]

admit(url, abstract, raw_content="", content=""):
  reject empty abstract
  create when absent
  otherwise replace snapshot with first_non_empty(existing, candidate)

merge_body(url, raw_content, content):
  require existing record
  fill only empty fields
  never change available

mark_unavailable(url):
  replace record with available=False

get(url):
  return frozen snapshot or None
```

所有方法在一次同步 event-loop turn 内完成 check-and-replace，不暴露内部 dict。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_url_store.py::test_url_store_preserves_public_shape_and_first_write_state_machine -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

提取一个私有 `first_non_empty` helper，避免三个字段复制逻辑分叉；运行：

```bash
uv run ruff check src/agent_search_gateway/models.py src/agent_search_gateway/url_store.py tests/unit/test_url_store.py
uv run mypy src/agent_search_gateway/models.py src/agent_search_gateway/url_store.py tests/unit/test_url_store.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 6: Provider Contracts, Capability Registry, And Test Fakes

**Files:**
- Create: `src/agent_search_gateway/providers/__init__.py`
- Create: `src/agent_search_gateway/providers/contracts.py`
- Create: `src/agent_search_gateway/providers/registry.py`
- Create: `tests/support/__init__.py`
- Create: `tests/support/fakes.py`
- Create: `tests/support/controlled.py`
- Test: `tests/providers/test_registry.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Adapter Protocols And Registry, Component Catalog)

- [ ] **Step 1: Write the failing test**

创建 `test_registry_exposes_exact_capabilities_and_contract_types`：注册 search-only、fetch-only、dual-stage adapter factory，断言 capability 查询与按 stage 选择正确；fake provider 可返回 configured values 或抛 configured `ExecutionFailure`，并记录调用参数；adapter contract 不接收 `URLStore`。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/providers/test_registry.py::test_registry_exposes_exact_capabilities_and_contract_types -v
```

Expected:

```text
FAIL because provider contracts and registry do not exist
```

- [ ] **Step 3: Describe the minimal implementation**

```text
define KeywordSearchHit and URLFetchCandidate frozen dataclasses
define runtime-checkable Protocols:
  KeywordSearchProvider(name, search)
  URLFetchProvider(name, fetch)
  LLMClient(name, complete_text, complete_json, aclose)

define ProviderCapabilities(search, fetch)
define WebProviderRegistration(name, capabilities, factory, allowed_config_keys)
define ProviderRegistry.register/get/capabilities/list_in_registration_order

FakeKeywordSearchProvider/FakeURLFetchProvider/FakeLLMClient:
  real async methods
  configured result or typed failure
  append each invocation to calls list

ControlledProvider:
  use asyncio.Event for entered/release coordination
  never sleep
```

Registry 不加载 entry points，不扫描模块，不做 dynamic plugin discovery。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/providers/test_registry.py::test_registry_exposes_exact_capabilities_and_contract_types -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

将 registration order 保存在单一结构中，避免 capabilities 与 factory 两套 source of truth；运行：

```bash
uv run ruff check src/agent_search_gateway/providers tests/support tests/providers/test_registry.py
uv run mypy src/agent_search_gateway/providers tests/support tests/providers/test_registry.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 7: Web Provider Configuration Resolution

**Files:**
- Create: `src/agent_search_gateway/config.py`
- Test: `tests/unit/test_config_web_providers.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Provider Stage Enablement, Secret Handling, Config Contract)

- [ ] **Step 1: Write the failing test**

创建参数化测试 `test_resolve_web_provider_config_or_fail_startup`，覆盖：default/override concurrency、enabled stage 的 `api_key_env` 与 env value、unsupported stage、未知 enabled provider、缺 secret、非正 concurrency、disabled provider 不初始化，以及 provider-specific options 被保留并交给 registration 校验。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_config_web_providers.py::test_resolve_web_provider_config_or_fail_startup -v
```

Expected:

```text
FAIL because web provider config resolution is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
parse TOML with tomllib
read [web_providers].default_max_concurrency
for each [web_providers.<name>]:
  resolve enable_search/enable_fetch false by default
  if neither enabled: keep disabled metadata only
  require registry entry for enabled provider
  require requested stages supported by capabilities
  resolve max_concurrency override or default and require > 0
  require api_key_env and environment value for enabled provider
  wrap secret in SecretValue whose repr is redacted
  split shared keys from provider-specific options
  reject provider-specific keys not listed by registration
return ordered resolved web configs
```

不要允许 config 直接指定 Python module/class。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_config_web_providers.py::test_resolve_web_provider_config_or_fail_startup -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

拆分纯解析与 environment/capability validation，确保错误统一映射为 `ConfigFailure(CONFIG_ERROR)`；运行：

```bash
uv run ruff check src/agent_search_gateway/config.py tests/unit/test_config_web_providers.py
uv run mypy src/agent_search_gateway/config.py tests/unit/test_config_web_providers.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 8: LLM Configuration, Stage Inheritance, And Example Config

**Files:**
- Modify: `src/agent_search_gateway/config.py` (LLM config dataclasses and resolver)
- Create: `config.example.toml`
- Test: `tests/unit/test_config_llm.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Configuration Inheritance For LLM Fallback, Multiple Named LLM Providers, Config Contract)

- [ ] **Step 1: Write the failing test**

创建 `test_resolve_llm_config_preserves_independent_search_entries_and_fetch_inheritance`，断言：LLM concurrency default/override；引用 provider 必须存在且 secret 可用；仅支持 `protocol=openai`、`api_endpoint=chat_completions`；每个 `[[search_llm.providers]]` 保持独立 provider/model/extra_body；缺 model 只从 global model 回退；judge/safety/content-clean/focus-summary 按 stage→fetch_llm→global 逐字段继承；`extra_body` 替换且 deep-copy；空 search list 可启动。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_config_llm.py::test_resolve_llm_config_preserves_independent_search_entries_and_fetch_inheritance -v
```

Expected:

```text
FAIL because LLM config resolution is incomplete
```

- [ ] **Step 3: Describe the minimal implementation**

```text
define LLMProviderConfig and LLMInvocation
resolve [llm_providers].default_max_concurrency
resolve only referenced LLM providers as enabled
validate protocol and endpoint capability
resolve global_default_llm
resolve each search_llm entry independently in source order
resolve fetch stage fields:
  stage field if present
  else fetch_llm field
  else global_default_llm field
for extra_body:
  choose first defined mapping as a whole
  deep-copy mapping into each LLMInvocation
require all four fetch LLM stages to resolve provider + model at startup
write config.example.toml with all seven web providers, named OpenAI-compatible provider, search entries, fetch stages, and [retry]
```

Provider-specific web keys in example config：TinyFish 使用 `search_api_url` 与 `fetch_api_url`；其他 provider 使用 `api_url`。示例只写 env variable 名，不写真实 key。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_config_llm.py::test_resolve_llm_config_preserves_independent_search_entries_and_fetch_inheritance -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

用一个 field-level resolver 统一四个 fetch stages，禁止分别实现四套继承逻辑；运行：

```bash
uv run ruff check src/agent_search_gateway/config.py tests/unit/test_config_llm.py
uv run mypy src/agent_search_gateway/config.py tests/unit/test_config_llm.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 9: Provider Capacity Gates And Quota Manager

**Files:**
- Create: `src/agent_search_gateway/concurrency.py`
- Test: `tests/runtime/test_quota_manager.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Provider Quotas)
- Docs: `docs/designs/testings/agent-search-gateway-cli-v0.md` (Provider Quotas)

- [ ] **Step 1: Write the failing test**

创建 `test_web_search_and_fetch_share_capacity_while_llm_capacity_is_separate`：用 events 占满一个 web provider gate，证明同 provider fetch 无法进入；同名 LLM gate 可进入；释放后 waiting web call 进入；并断言 max concurrency 从未超限。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/runtime/test_quota_manager.py::test_web_search_and_fetch_share_capacity_while_llm_capacity_is_separate -v
```

Expected:

```text
FAIL because capacity gates are missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
CapacityGate:
  track limit, in_use, asyncio.Condition
  lease() waits until in_use < limit
  try_lease() atomically returns lease or None
  release notifies waiters

ProviderQuotaManager:
  one web CapacityGate per enabled web provider, shared by search/fetch
  one distinct LLM CapacityGate per named LLM provider
  get_web(name), get_llm(name)
  wait_until_any_web_available(candidate_names)
```

不要使用 `Semaphore._value` 等私有属性；capacity-aware scheduler 只能依赖公开的 `try_lease`/condition API。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/runtime/test_quota_manager.py::test_web_search_and_fetch_share_capacity_while_llm_capacity_is_separate -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

确保 cancellation 时 lease 在 `finally` 中释放，加入断言防止 double release；运行：

```bash
uv run ruff check src/agent_search_gateway/concurrency.py tests/runtime/test_quota_manager.py
uv run mypy src/agent_search_gateway/concurrency.py tests/runtime/test_quota_manager.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 10: Exact-Key Singleflight And Per-Key Lock Pool

**Files:**
- Modify: `src/agent_search_gateway/concurrency.py` (singleflight and keyed locks)
- Test: `tests/runtime/test_singleflight.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Per-URL Singleflight)
- Docs: `docs/designs/testings/agent-search-gateway-cli-v0.md` (Per-URL Singleflight)

- [ ] **Step 1: Write the failing test**

创建 `test_singleflight_shares_same_key_result_and_exception_but_allows_different_keys`：同 key 两个调用只执行一次 factory 并共享值；异常也共享且清理 in-flight entry；不同 key 可由 events 证明并发。另断言 `PerKeyLockPool` 同 key 串行、不同 key 并行。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/runtime/test_singleflight.py::test_singleflight_shares_same_key_result_and_exception_but_allows_different_keys -v
```

Expected:

```text
FAIL because SingleflightGroup and PerKeyLockPool are missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
SingleflightGroup.do(key, factory):
  under internal lock, reuse existing Future or install leader Future
  leader executes factory outside internal lock
  set same result/exception for followers
  remove entry only after Future completed
  propagate cancellation without orphaning followers

PerKeyLockPool.acquire(key):
  reference-count lock entries
  serialize same key
  remove lock when owner/waiters count reaches zero
```

不要缓存已完成结果；它只合并当前并发调用。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/runtime/test_singleflight.py::test_singleflight_shares_same_key_result_and_exception_but_allows_different_keys -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

将 lifecycle cleanup 放在一个 helper 中，覆盖 success/error/cancel 三条路径；运行：

```bash
uv run ruff check src/agent_search_gateway/concurrency.py tests/runtime/test_singleflight.py
uv run mypy src/agent_search_gateway/concurrency.py tests/runtime/test_singleflight.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 11: Configurable Exponential Retry Engine

**Files:**
- Create: `src/agent_search_gateway/retry.py`
- Modify: `src/agent_search_gateway/config.py` (`RetryPolicy` resolution)
- Test: `tests/unit/test_retry.py`
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (LLM Stage Errors)

- [ ] **Step 1: Write the failing test**

创建 `test_retry_engine_uses_configured_attempts_and_exponential_delays_without_sleeping`：operation 前两次抛 retryable exception、第三次成功；注入 fake sleep 记录 `0.25, 0.5`；验证 non-retryable exception 不重试、耗尽后抛最后一个 typed failure、配置非法时 startup config error。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_retry.py::test_retry_engine_uses_configured_attempts_and_exponential_delays_without_sleeping -v
```

Expected:

```text
FAIL because RetryPolicy and retry_async are missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
resolve optional [retry] using locked defaults
validate max_attempts >= 1 and delays/timeouts > 0

retry_async(policy, operation, is_retryable, sleep):
  for attempt in 1..max_attempts:
    try operation
    catch exception:
      if not retryable or final attempt: raise
      delay = min(base_delay * 2 ** (attempt - 1), max_delay)
      await sleep(delay)
```

不加入 jitter、Retry-After 或 provider-specific override，除非后续设计明确要求。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_retry.py::test_retry_engine_uses_configured_attempts_and_exponential_delays_without_sleeping -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

让 retry engine 不依赖 HTTP/LLM 类型，只依赖 predicate；运行：

```bash
uv run ruff check src/agent_search_gateway/retry.py src/agent_search_gateway/config.py tests/unit/test_retry.py
uv run mypy src/agent_search_gateway/retry.py src/agent_search_gateway/config.py tests/unit/test_retry.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 12: HTTP JSON Boundary And Secret-Safe Observability

**Files:**
- Create: `src/agent_search_gateway/observability.py`
- Create: `src/agent_search_gateway/providers/http.py`
- Test: `tests/providers/test_http_executor.py`
- Test: `tests/unit/test_observability.py`
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (Logging, retries, provider execution failure)

- [ ] **Step 1: Write the failing test**

创建参数化 contract test `test_http_executor_classifies_retryable_status_and_never_logs_secrets_or_content`：用 `MockTransport` 覆盖 timeout/transport、408、429、5xx 重试，400 不重试，invalid JSON 为 protocol execution failure；caplog 中不得出现 API key、Authorization header 或完整 body，只允许 provider/stage/subject/短 reason。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/providers/test_http_executor.py::test_http_executor_classifies_retryable_status_and_never_logs_secrets_or_content -v
```

Expected:

```text
FAIL because HttpJsonExecutor and redaction are missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
SecretValue:
  value available only through explicit reveal()
  repr/str return redacted marker

SecretRedactingFilter:
  replace configured secret byte/string occurrences in log messages

HttpJsonExecutor.request_json(method, url, headers, json_body):
  call httpx.AsyncClient.request with configured timeout
  retry transport/timeouts and status 408/429/500-599
  fail immediately for other 4xx
  require JSON object/list as requested by caller
  convert network/status/decode failures to ExecutionFailure with provider/stage metadata
  never embed headers or body in error text
```

Provider-specific shape validation仍放在 adapter parser。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/providers/test_http_executor.py::test_http_executor_classifies_retryable_status_and_never_logs_secrets_or_content -v
uv run pytest tests/unit/test_observability.py -v
uv run pytest -v
```

Expected:

```text
Target tests PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

统一 HTTP error construction 与 log metadata，避免各 adapter 拼接原始 response；运行：

```bash
uv run ruff check src/agent_search_gateway/observability.py src/agent_search_gateway/providers/http.py tests/providers/test_http_executor.py tests/unit/test_observability.py
uv run mypy src/agent_search_gateway/observability.py src/agent_search_gateway/providers/http.py tests/providers/test_http_executor.py tests/unit/test_observability.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 13: OpenAI-Compatible Chat-Completions Adapter

**Files:**
- Create: `src/agent_search_gateway/providers/llm/__init__.py`
- Create: `src/agent_search_gateway/providers/llm/openai_chat.py`
- Test: `tests/providers/test_openai_chat.py`
- Create: `tests/fixtures/providers/openai/chat_text.json`
- Create: `tests/fixtures/providers/openai/chat_json.json`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (OpenAI-Compatible Chat Completions First, Direct HTTP)

- [ ] **Step 1: Write the failing test**

创建参数化测试 `test_openai_chat_client_builds_invocation_and_parses_text_or_json`：断言 base URL 追加 `/v1/chat/completions`；body 使用 invocation model/messages；每个 invocation 的 `extra_body` 独立合并；保留 provider-specific nested fields；拒绝 `extra_body` 覆盖 `model/messages`；解析 `choices[0].message.content`；JSON mode 解析 object；invalid/empty transient response 按 retry policy 重试；耗尽后为 `ExecutionFailure`。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/providers/test_openai_chat.py::test_openai_chat_client_builds_invocation_and_parses_text_or_json -v
```

Expected:

```text
FAIL because OpenAIChatCompletionsClient is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
OpenAIChatCompletionsClient(name, api_url, secret, quota_gate, http_executor):
  endpoint = rstrip(api_url, "/") + "/v1/chat/completions"

complete_text(invocation, messages):
  validate invocation.provider == self.name
  reject reserved keys in extra_body
  body = {model, messages, **extra_body}
  acquire LLM provider quota
  retry request + response-shape parse as one operation
  return non-empty choices[0].message.content

complete_json(...):
  call same internal completion path
  json.loads(content)
  require JSON object

aclose():
  close owned httpx client exactly once
```

不要实现 runtime provider failover 或 Responses/Anthropic/Gemini protocol。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/providers/test_openai_chat.py::test_openai_chat_client_builds_invocation_and_parses_text_or_json -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

让 text/JSON 共用唯一 request builder 和 response extractor；运行：

```bash
uv run ruff check src/agent_search_gateway/providers/llm tests/providers/test_openai_chat.py
uv run mypy src/agent_search_gateway/providers/llm tests/providers/test_openai_chat.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 14: Restricted Markdown Parser For LLM Search

**Files:**
- Create: `src/agent_search_gateway/llm/__init__.py`
- Create: `src/agent_search_gateway/llm/markdown_parser.py`
- Test: `tests/unit/test_markdown_parser.py`
- Docs: `docs/designs/testings/agent-search-gateway-cli-v0.md` (Restricted Markdown Parser For LLM Search)

- [ ] **Step 1: Write the failing test**

创建参数化测试 `test_parse_restricted_search_markdown_accepts_only_result_blocks`：解析重复 `## Result`；只读取块内单一 `URL:`/`Abstract:`；忽略块外 links/text；empty abstract 丢弃；missing/duplicate required field、invalid URL 或无可识别结构产生 `ParserFailure`；成功但零个 non-empty records 可返回空 list。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_markdown_parser.py::test_parse_restricted_search_markdown_accepts_only_result_blocks -v
```

Expected:

```text
FAIL because restricted Markdown parser is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
scan line-by-line
start block only on exact stripped heading "## Result"
within block accept one URL and one Abstract field
on next Result heading or EOF finalize block
missing or duplicate required field => ParserFailure
normalize URL during finalization; invalid URL => ParserFailure
empty trimmed abstract => omit record without failure
ignore all non-field lines and all content outside result blocks
return list[SearchRecord]
```

不要使用通用 Markdown AST 或从任意 link 猜测结果。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_markdown_parser.py::test_parse_restricted_search_markdown_accepts_only_result_blocks -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

用单一 block finalizer 保持 EOF 与 next-heading 行为一致；运行：

```bash
uv run ruff check src/agent_search_gateway/llm/markdown_parser.py tests/unit/test_markdown_parser.py
uv run mypy src/agent_search_gateway/llm/markdown_parser.py tests/unit/test_markdown_parser.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 15: Cheap Check And Judge Stage

**Files:**
- Create: `src/agent_search_gateway/llm/prompts.py`
- Create: `src/agent_search_gateway/llm/stages.py`
- Test: `tests/unit/test_llm_judge_stage.py`
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (Judge)

- [ ] **Step 1: Write the failing test**

创建 `test_judge_distinguishes_semantic_rejection_from_execution_failure`：`cheap_check` 对 empty/whitespace 为 false、其他文本为 true；judge 调用 resolved invocation 的 `complete_json`；`{"ok": false}` 返回 `StageDecision(ok=False)`；client execution failure 原样作为 execution failure 传播；缺失/non-bool `ok` 为 execution failure，不得变成 semantic rejection。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_llm_judge_stage.py::test_judge_distinguishes_semantic_rejection_from_execution_failure -v
```

Expected:

```text
FAIL because LLMStages.judge is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
cheap_check(candidate): return bool(candidate.strip())

judge(candidate):
  messages = judge prompt + candidate
  payload = client_registry.complete_json(resolved judge invocation, messages)
  require payload["ok"] is bool
  reason = optional short string
  return StageDecision(ok, reason)
```

Prompt 只要求判断抓取内容是否为目标网页的可用正文；不要把 provider/error classification 写进 prompt。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_llm_judge_stage.py::test_judge_distinguishes_semantic_rejection_from_execution_failure -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

提取严格 boolean decision parser，为 safety 重用；运行：

```bash
uv run ruff check src/agent_search_gateway/llm tests/unit/test_llm_judge_stage.py
uv run mypy src/agent_search_gateway/llm tests/unit/test_llm_judge_stage.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 16: Safety, Content-Clean, Focus-Summary, And LLM-Search Prompts

**Files:**
- Modify: `src/agent_search_gateway/llm/prompts.py` (remaining prompt builders)
- Modify: `src/agent_search_gateway/llm/stages.py` (remaining stage methods)
- Test: `tests/unit/test_llm_stages.py`
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (Safety, Content Clean, Focus Summary, LLM Search)

- [ ] **Step 1: Write the failing test**

创建参数化测试 `test_llm_stages_use_resolved_invocations_and_validate_outputs`：safety 只接受 boolean `ok`；content-clean 与 focus-summary 必须返回 non-empty text；focus 被包含在 prompt 且结果不写入任何 store；LLM-search prompt 明确要求 restricted `## Result`/`URL:`/`Abstract:` 格式；每个 stage 使用自己的 resolved provider/model/extra_body；client failure 不做跨 provider fallback。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_llm_stages.py::test_llm_stages_use_resolved_invocations_and_validate_outputs -v
```

Expected:

```text
FAIL because remaining LLM stages are missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
safety(content):
  complete_json with safety invocation
  parse strict StageDecision

content_clean(raw_content):
  complete_text with content_clean invocation
  strip and require non-empty

focus_summary(content, focus):
  complete_text with focus_summary invocation
  include both content and normalized focus
  strip and require non-empty

llm_search_markdown(invocation, prompt):
  build format-constrained messages
  complete_text using the entry invocation
```

所有 invalid output 在 adapter retry 耗尽后映射为 execution failure；stage 不返回 full-content fallback。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_llm_stages.py::test_llm_stages_use_resolved_invocations_and_validate_outputs -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

每种 prompt 使用纯函数 builder，stage 只负责 invocation 与 output validation；运行：

```bash
uv run ruff check src/agent_search_gateway/llm tests/unit/test_llm_stages.py
uv run mypy src/agent_search_gateway/llm tests/unit/test_llm_stages.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 17: Search Result Jsonl Writer

**Files:**
- Create: `src/agent_search_gateway/result_writer.py`
- Test: `tests/unit/test_result_writer.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Result Jsonl Files, Result Jsonl Contract)

- [ ] **Step 1: Write the failing test**

创建 `test_result_writer_creates_unique_compact_jsonl_with_only_public_fields`：同 kind 连续写两次获得不同 absolute path；空结果也创建文件；每行仅 `url`、`abstract`；UTF-8 compact JSON；目录自动创建；文件名分别使用 `keyword-` 或 `llm-` prefix。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_result_writer.py::test_result_writer_creates_unique_compact_jsonl_with_only_public_fields -v
```

Expected:

```text
FAIL because ResultWriter is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
ResultWriter(results_dir)

write_results(kind, records):
  ensure results_dir exists
  generate cryptographically random short token
  open unique target with exclusive creation; retry on collision
  for each SearchRecord:
    write compact ensure_ascii=False JSON object + newline
  return target.resolve()
```

不写 raw/content/available/provider metadata，不复用 query cache。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_result_writer.py::test_result_writer_creates_unique_compact_jsonl_with_only_public_fields -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

将 record serialization 独立为纯函数并校验 non-empty abstract；运行：

```bash
uv run ruff check src/agent_search_gateway/result_writer.py tests/unit/test_result_writer.py
uv run mypy src/agent_search_gateway/result_writer.py tests/unit/test_result_writer.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 18: Typed NDJSON Requests, Responses, And Streaming Codec

**Files:**
- Create: `src/agent_search_gateway/protocol.py`
- Modify: `src/agent_search_gateway/models.py` (request/response dataclasses)
- Test: `tests/unit/test_protocol_codec.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Request / Response Contract)
- Docs: `docs/designs/testings/agent-search-gateway-cli-v0.md` (NDJSON Framing)

- [ ] **Step 1: Write the failing test**

创建 `test_ndjson_codec_buffers_partial_bytes_and_splits_multiple_requests`：partial bytes 在 newline 前不产出；一次 read 中多个 lines 各自产出 typed request；invalid JSON、unknown type、缺/错字段生成 `ErrorResponse(bad_request)`；`shutdown` 有效；response encoding 恰好单行并以 `\n` 结尾。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_protocol_codec.py::test_ndjson_codec_buffers_partial_bytes_and_splits_multiple_requests -v
```

Expected:

```text
FAIL because NDJSON codec and protocol models are missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
define request dataclasses:
  KeywordSearchRequest(query)
  LLMSearchRequest(prompt)
  URLFetchRequest(url, focus)
  ShutdownRequest()

define SuccessResponse/ErrorResponse

NDJSONDecoder.feed(bytes):
  append buffer
  split complete newline-delimited frames
  decode UTF-8 + JSON object
  validate exact request type and field types
  return list[Request | ErrorResponse]

encode_response(response):
  compact JSON + b"\n"
```

Decoder 错误不修改 URL state；不要接受 arbitrary extra request types。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_protocol_codec.py::test_ndjson_codec_buffers_partial_bytes_and_splits_multiple_requests -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

使用单一 request validation dispatch table，避免 client/server schema 分叉；运行：

```bash
uv run ruff check src/agent_search_gateway/protocol.py src/agent_search_gateway/models.py tests/unit/test_protocol_codec.py
uv run mypy src/agent_search_gateway/protocol.py src/agent_search_gateway/models.py tests/unit/test_protocol_codec.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 19: Unix Socket Client And Response Validation

**Files:**
- Modify: `src/agent_search_gateway/protocol.py` (socket client)
- Test: `tests/unit/test_socket_client.py`
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (Missing Daemon, Malformed Socket Response)

- [ ] **Step 1: Write the failing test**

创建参数化测试 `test_socket_client_sends_one_line_and_rejects_malformed_response`：请求只写一行；读取到第一条完整 response 后关闭；invalid JSON、EOF before newline、缺 required field、错误 `ok` type 均抛 `ProtocolFailure(PROTOCOL_ERROR)`；socket missing/refused 单独抛 `DaemonUnavailable`，供 CLI 区分 stop 与 workflow。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/unit/test_socket_client.py::test_socket_client_sends_one_line_and_rejects_malformed_response -v
```

Expected:

```text
FAIL because send_request is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
send_request(socket_path, typed_request):
  asyncio.open_unix_connection
  write compact encoded request + newline
  drain
  read until newline
  if EOF before newline: ProtocolFailure
  parse JSON object
  validate SuccessResponse or ErrorResponse exact required fields
  close writer in finally

translate FileNotFoundError/ConnectionRefusedError into DaemonUnavailable
```

不自动启动 daemon，不读取第二条 response，不在 protocol 层打印。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/unit/test_socket_client.py::test_socket_client_sends_one_line_and_rejects_malformed_response -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

复用 Task 18 的 response parser，保证 encoding/validation 单一来源；运行：

```bash
uv run ruff check src/agent_search_gateway/protocol.py tests/unit/test_socket_client.py
uv run mypy src/agent_search_gateway/protocol.py tests/unit/test_socket_client.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 20: Keyword Search Pipeline Completion And Failure Isolation

**Files:**
- Create: `src/agent_search_gateway/orchestrators/__init__.py`
- Create: `src/agent_search_gateway/orchestrators/search.py`
- Test: `tests/orchestrators/test_keyword_search_pipeline.py`
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (Keyword Search Provider Pipeline)

- [ ] **Step 1: Write the failing test**

创建参数化测试 `test_keyword_search_uses_pipeline_completion_rules`：empty query→`empty_query`；无 provider→`no_keyword_search_providers`；一个 provider execution failure 不阻止成功 pipeline；成功但 empty hits 仍算 completed 并写 empty jsonl；全部 pipeline execution failure→`all_providers_failed` 且不写文件/状态；所有 provider 在自己的 web quota 下调用。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/orchestrators/test_keyword_search_pipeline.py::test_keyword_search_uses_pipeline_completion_rules -v
```

Expected:

```text
FAIL because SearchOrchestrator.keyword_search is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
normalize query by strip; reject empty
require at least one enabled keyword provider
launch one async pipeline per provider under that provider's web quota
pipeline:
  call provider.search(query)
  validate returned list and hit field types
  build temporary pipeline records only
collect outcomes concurrently but retain configured provider order
completed = pipelines returning normally, including empty
if completed empty: raise ALL_PROVIDERS_FAILED
commit completed pipeline records in configured order
write keyword jsonl and return absolute path string
```

本任务先使用无 body 的 hits；body validation 在下一任务加入。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/orchestrators/test_keyword_search_pipeline.py::test_keyword_search_uses_pipeline_completion_rules -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

提取 `_run_keyword_pipeline`，让 orchestration 只处理并发、分类与 commit；运行：

```bash
uv run ruff check src/agent_search_gateway/orchestrators/search.py tests/orchestrators/test_keyword_search_pipeline.py
uv run mypy src/agent_search_gateway/orchestrators/search.py tests/orchestrators/test_keyword_search_pipeline.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 21: Keyword Search Merge, Body Validation, And Repeat Semantics

**Files:**
- Modify: `src/agent_search_gateway/orchestrators/search.py` (keyword body/state flow)
- Test: `tests/orchestrators/test_keyword_search_state.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Keyword Search Data Flow, Repeat Searches, Unavailable URLs)
- Docs: `docs/designs/testings/agent-search-gateway-cli-v0.md` (Keyword Search coverage)

- [ ] **Step 1: Write the failing test**

创建 scenario test `test_keyword_search_validates_body_then_commits_deterministic_first_write_state`，覆盖：`abstract=snippet or title`；empty abstract 丢弃且不 admission；normalized duplicate 去重；content 优先作为 validation candidate，content 通过时 raw/content 都可写；仅 raw 时验证 raw；semantic rejection 只跳过 body；judge execution failure 丢弃整个 provider pipeline；first non-empty wins；已有 unavailable URL 仍输出 stored abstract 但跳过 body validation；重复 query 再调 providers并产生新 path；并发 provider 结果按配置顺序 commit。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/orchestrators/test_keyword_search_state.py::test_keyword_search_validates_body_then_commits_deterministic_first_write_state -v
```

Expected:

```text
FAIL because keyword body validation and merge semantics are incomplete
```

- [ ] **Step 3: Describe the minimal implementation**

```text
for each hit in one provider pipeline:
  normalize URL
  abstract = strip(snippet) or strip(title)
  if empty: skip
  inspect current URL snapshot
  if current.available is false:
    stage record without body validation
    continue
  candidate = content if non-empty else raw_content
  if candidate exists:
    if not cheap_check(candidate): mark candidate body rejected, keep record
    else decision = await judge(candidate)
      if decision.ok: stage raw_content/content fields
      else: keep record without body fields
  stage hit only; do not mutate store yet

when pipeline completes:
  commit staged hits in provider/config order
  URLStore.admit with first-write semantics
  construct deduped SearchRecord from final store snapshot
```

若 judge 抛 execution failure，pipeline 暂存 records 全部作废。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/orchestrators/test_keyword_search_state.py::test_keyword_search_validates_body_then_commits_deterministic_first_write_state -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

统一 candidate 选择与 body field staging helper，避免 search/fetch 对“both fields”规则不一致；运行：

```bash
uv run ruff check src/agent_search_gateway/orchestrators/search.py tests/orchestrators/test_keyword_search_state.py
uv run mypy src/agent_search_gateway/orchestrators/search.py tests/orchestrators/test_keyword_search_state.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 22: LLM Search Orchestration

**Files:**
- Modify: `src/agent_search_gateway/orchestrators/search.py` (`llm_search`)
- Test: `tests/orchestrators/test_llm_search.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (LLM Search Data Flow)
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (LLM Search Provider Pipeline)

- [ ] **Step 1: Write the failing test**

创建参数化测试 `test_llm_search_runs_independent_entries_and_isolates_pipeline_failures`：empty prompt→`empty_query`；无 entries→`no_llm_search_providers`；全部 entries 并发调用且各自 model/extra_body 不串用；execution 或 parser failure 只失败该 entry；成功解析零结果仍算 completed；全部失败→`all_providers_failed`；结果只 admission url/abstract；duplicate first-write；输出顺序按 entry 配置顺序；每次调用新 jsonl。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/orchestrators/test_llm_search.py::test_llm_search_runs_independent_entries_and_isolates_pipeline_failures -v
```

Expected:

```text
FAIL because SearchOrchestrator.llm_search is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
strip prompt; validate non-empty
require at least one LLM search invocation
for each invocation in config order, concurrently:
  markdown = LLMStages.llm_search_markdown(invocation, prompt)
  records = parse_restricted_search_markdown(markdown)
  return completed pipeline records
catch ExecutionFailure or ParserFailure per entry and log short metadata
if no completed entries: ALL_PROVIDERS_FAILED
commit completed records in config order through URLStore.admit
build deduped output from final store snapshots
write llm jsonl and return path
```

LLM search 不调用 judge/safety，也不写 body fields。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/orchestrators/test_llm_search.py::test_llm_search_runs_independent_entries_and_isolates_pipeline_failures -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

复用 keyword search 的 ordered pipeline outcome collector，但保持两种 pipeline 的 parsing/validation 独立；运行：

```bash
uv run ruff check src/agent_search_gateway/orchestrators/search.py tests/orchestrators/test_llm_search.py
uv run mypy src/agent_search_gateway/orchestrators/search.py tests/orchestrators/test_llm_search.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 23: Fetch Scheduler Outcome Classification

**Files:**
- Create: `src/agent_search_gateway/scheduler/__init__.py`
- Create: `src/agent_search_gateway/scheduler/fetch.py`
- Modify: `src/agent_search_gateway/models.py` (`FetchOutcome`)
- Test: `tests/scheduler/test_fetch_outcomes.py`
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (URL Fetch Provider Pipeline, Final No-Success Rule)

- [ ] **Step 1: Write the failing test**

创建参数化测试 `test_fetch_scheduler_classifies_execution_semantic_and_accepted_outcomes`：provider execution failure 后尝试下一个；empty/malformed result 为 execution failure；cheap_check/judge `ok=false` 为 semantic failure；judge execution failure 为 execution failure；later provider 可成功；全部 execution→`execution_failure`；至少一个 semantic 且无成功→`semantic_failure`；first success 返回 candidate 并停止。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/scheduler/test_fetch_outcomes.py::test_fetch_scheduler_classifies_execution_semantic_and_accepted_outcomes -v
```

Expected:

```text
FAIL because FetchScheduler is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
remaining providers = enabled URL fetch providers
semantic_failure_seen = false
execution_failures = []

while remaining:
  select one provider (capacity-aware selection added next task)
  call provider.fetch(normalized_url)
  require non-empty raw_content
  candidate_for_validation = content if non-empty else raw_content
  if cheap_check false:
    semantic_failure_seen = true
    continue
  try judge(candidate)
  catch ExecutionFailure:
    append failure
    continue
  if judge.ok false:
    semantic_failure_seen = true
    continue
  return FetchOutcome.accepted(candidate)

if semantic_failure_seen: semantic_failure
else: execution_failure with collected failures
```

同一个 job 内永远只执行一个 provider attempt。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/scheduler/test_fetch_outcomes.py::test_fetch_scheduler_classifies_execution_semantic_and_accepted_outcomes -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

将 attempt classification 提取为一个返回 typed outcome 的 helper，主 scheduler 只循环；运行：

```bash
uv run ruff check src/agent_search_gateway/scheduler/fetch.py src/agent_search_gateway/models.py tests/scheduler/test_fetch_outcomes.py
uv run mypy src/agent_search_gateway/scheduler/fetch.py src/agent_search_gateway/models.py tests/scheduler/test_fetch_outcomes.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 24: Capacity-Aware Fetch Provider Selection

**Files:**
- Modify: `src/agent_search_gateway/scheduler/fetch.py` (provider selection)
- Test: `tests/scheduler/test_fetch_capacity.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Fetch Provider Scheduler)
- Docs: `docs/designs/testings/agent-search-gateway-cli-v0.md` (Provider Quotas)

- [ ] **Step 1: Write the failing test**

创建 `test_fetch_scheduler_uses_available_provider_without_parallel_attempts_for_one_job`：provider A quota 已饱和时，job 选择可用 B；若都饱和则等待任一可用；不同 URL jobs 可分别进入不同 providers；单个 URL job 的 active provider count 永远不超过 1；尝试顺序在同时可用时遵循 registry order。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/scheduler/test_fetch_capacity.py::test_fetch_scheduler_uses_available_provider_without_parallel_attempts_for_one_job -v
```

Expected:

```text
FAIL because scheduler waits on fixed provider order or lacks capacity selection
```

- [ ] **Step 3: Describe the minimal implementation**

```text
for unattempted provider names in registration order:
  ask quota manager for first immediately available lease
  if lease found:
    remove provider from unattempted
    execute exactly one attempt under lease
  else:
    await quota_manager.wait_until_any_web_available(unattempted)
    retry selection

never create concurrent attempt tasks for one fetch job
```

Cancellation 必须释放 lease，且不能把未执行 provider 标记为 attempted。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/scheduler/test_fetch_capacity.py::test_fetch_scheduler_uses_available_provider_without_parallel_attempts_for_one_job -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

把 selection loop 与 attempt evaluator 分离，避免 capacity 逻辑侵入 semantic classification；运行：

```bash
uv run ruff check src/agent_search_gateway/scheduler/fetch.py tests/scheduler/test_fetch_capacity.py
uv run mypy src/agent_search_gateway/scheduler/fetch.py tests/scheduler/test_fetch_capacity.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 25: URL Fetch Admission And Cached-State Paths

**Files:**
- Create: `src/agent_search_gateway/orchestrators/fetch.py`
- Test: `tests/orchestrators/test_url_fetch_admission.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (URL Fetch Data Flow)
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (Input Validation Errors, URL Already Unavailable)

- [ ] **Step 1: Write the failing test**

创建参数化测试 `test_url_fetch_enforces_admission_and_uses_cached_fields`：invalid URL→`invalid_url`；未 admission→`url_not_admitted`；unavailable 直接返回稳定 message 且不调 provider/LLM；已有 content 跳过 fetch/content-clean 但运行 safety；已有 raw 且 content empty 运行 content-clean、first-write content、再 safety；无 body 且无 fetch providers→`no_url_fetch_providers`。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/orchestrators/test_url_fetch_admission.py::test_url_fetch_enforces_admission_and_uses_cached_fields -v
```

Expected:

```text
FAIL because FetchOrchestrator is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
url_fetch(url, focus):
  normalized_url = normalize_url(url)
  normalized_focus = strip(focus) or None
  snapshot = store.get(normalized_url)
  if missing: URL_NOT_ADMITTED
  if available false: return UNAVAILABLE_MESSAGE

  refresh snapshot under per-URL lock
  if content exists:
    run safety then return content/focus continuation
  elif raw_content exists:
    cleaned = content_clean(raw_content)
    store.merge_body(content=cleaned)
    refresh snapshot
    run safety then return continuation
  elif no fetch providers:
    NO_URL_FETCH_PROVIDERS
  else:
    delegate missing-body path to next task
```

Safety execution failure 不改变 available；safety semantic rejection 在下一任务落状态。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/orchestrators/test_url_fetch_admission.py::test_url_fetch_enforces_admission_and_uses_cached_fields -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

所有 store read 在可能 await 后重新读取 snapshot，避免使用 stale mutable reference；运行：

```bash
uv run ruff check src/agent_search_gateway/orchestrators/fetch.py tests/orchestrators/test_url_fetch_admission.py
uv run mypy src/agent_search_gateway/orchestrators/fetch.py tests/orchestrators/test_url_fetch_admission.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 26: URL Fetch Provider, LLM, Safety, And Focus Flow

**Files:**
- Modify: `src/agent_search_gateway/orchestrators/fetch.py` (complete workflow)
- Test: `tests/orchestrators/test_url_fetch_flow.py`
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (URL Fetch Provider Pipeline, LLM Stage Errors, Allowed Mutations)

- [ ] **Step 1: Write the failing test**

创建 scenario test `test_url_fetch_mutates_state_only_for_accepted_or_semantically_rejected_results`：accepted candidate 在 validation 后写 raw/content；raw-only 经过 content-clean；pure execution outcome→`all_providers_failed` 且 available 保持 true；semantic no-success→mark unavailable + success message；safety false→mark unavailable；safety execution→`llm_stage_failed` 且不 mark；content-clean execution→`llm_stage_failed`；focus 先 safety 后 summary、只返回 summary、不缓存；focus failure 不回退 full content。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/orchestrators/test_url_fetch_flow.py::test_url_fetch_mutates_state_only_for_accepted_or_semantically_rejected_results -v
```

Expected:

```text
FAIL because missing-body workflow and state classification are incomplete
```

- [ ] **Step 3: Describe the minimal implementation**

```text
if no stored body:
  outcome = scheduler.fetch_until_accepted(url)
  if execution_failure:
    raise ALL_PROVIDERS_FAILED
  if semantic_failure:
    store.mark_unavailable(url)
    return UNAVAILABLE_MESSAGE
  if accepted:
    store.merge_body(raw_content=candidate.raw_content, content=candidate.content)

refresh snapshot
if content empty and raw non-empty:
  try cleaned = content_clean(raw)
  catch ExecutionFailure: raise LLM_STAGE_FAILED
  store.merge_body(content=cleaned)

refresh snapshot; require final non-empty content
try decision = safety(content)
catch ExecutionFailure: raise LLM_STAGE_FAILED
if decision.ok false:
  store.mark_unavailable(url)
  return UNAVAILABLE_MESSAGE

if focus is None: return content
try return focus_summary(content, focus)
catch ExecutionFailure: raise LLM_STAGE_FAILED
```

任何 execution failure 都不得把 URL 设为 unavailable。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/orchestrators/test_url_fetch_flow.py::test_url_fetch_mutates_state_only_for_accepted_or_semantically_rejected_results -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

提取 `_prepare_content`、`_safety_check`、`_render_focus_or_content`，但不要创建通用 workflow framework；运行：

```bash
uv run ruff check src/agent_search_gateway/orchestrators/fetch.py tests/orchestrators/test_url_fetch_flow.py
uv run mypy src/agent_search_gateway/orchestrators/fetch.py tests/orchestrators/test_url_fetch_flow.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 27: URL Fetch Singleflight Integration

**Files:**
- Modify: `src/agent_search_gateway/orchestrators/fetch.py` (singleflight keys and per-URL serialization)
- Test: `tests/orchestrators/test_url_fetch_singleflight.py`
- Docs: `docs/designs/testings/agent-search-gateway-cli-v0.md` (Per-URL Singleflight)

- [ ] **Step 1: Write the failing test**

创建 `test_url_fetch_singleflight_shares_exact_request_and_serializes_different_focus`：两个并发同 URL/no-focus 只调一次 fetch provider 并共享 content；两个并发同 `(URL, focus)` 共享 summary 或同一 error；同 URL 不同 focus 串行并各自产生不同 summary；same-URL workflow 的 content-clean/safety/focus 不重叠；不同 URL 可并行。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/orchestrators/test_url_fetch_singleflight.py::test_url_fetch_singleflight_shares_exact_request_and_serializes_different_focus -v
```

Expected:

```text
FAIL because fetch workflow lacks exact-key singleflight or per-URL serialization
```

- [ ] **Step 3: Describe the minimal implementation**

```text
public url_fetch:
  normalize URL and focus before constructing key
  exact_key = (normalized_url, normalized_focus)
  return request_singleflight.do(exact_key, lambda: _serialized_url_fetch(...))

_serialized_url_fetch:
  async with per_url_lock_pool.acquire(normalized_url):
    refresh URL snapshot
    execute complete Task 25/26 workflow
```

同 key leader 的 typed exception 传播给 followers；完成后不缓存 exception/result，后续非并发调用按当前 URL state 重新执行。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/orchestrators/test_url_fetch_singleflight.py::test_url_fetch_singleflight_shares_exact_request_and_serializes_different_focus -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

保证 normalization 只执行一次，singleflight key 与 lock key 使用同一 `NormalizedURL`；运行：

```bash
uv run ruff check src/agent_search_gateway/orchestrators/fetch.py tests/orchestrators/test_url_fetch_singleflight.py
uv run mypy src/agent_search_gateway/orchestrators/fetch.py tests/orchestrators/test_url_fetch_singleflight.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 28: Tavily Search And Fetch Adapter

**Files:**
- Create: `src/agent_search_gateway/providers/web/__init__.py`
- Create: `src/agent_search_gateway/providers/web/tavily.py`
- Create: `tests/fixtures/providers/tavily/search.json`
- Create: `tests/fixtures/providers/tavily/extract.json`
- Test: `tests/providers/web/test_tavily.py`
- Docs: `https://docs.tavily.com/documentation/api-reference/introduction`

- [ ] **Step 1: Write the failing test**

创建参数化 contract test `test_tavily_adapter_conforms_to_registered_search_and_fetch_contracts`：验证 bearer auth 与 configured base URL；search 调 `/search`，映射 `url/title/content` 为 URL/title/snippet，optional `raw_content` 为 body；fetch 调 `/extract`，单 URL result 映射 non-empty raw content；empty/malformed shape 为 execution failure；adapter 不接触 URL Store。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/providers/web/test_tavily.py::test_tavily_adapter_conforms_to_registered_search_and_fetch_contracts -v
```

Expected:

```text
FAIL because TavilyAdapter is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
TavilyAdapter(name, api_url, secret, http_executor)
search(query): POST {api_url}/search with minimal query and raw-content option
parse results[] into KeywordSearchHit
  snippet = provider search summary field
  raw_content = optional extracted page body
  content = "" unless provider has a distinct cleaned full-body field
fetch(url): POST {api_url}/extract for one URL
parse matching result into URLFetchCandidate(raw_content, content="")
```

Fixture 必须脱敏且只保留 parser 所需字段。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/providers/web/test_tavily.py::test_tavily_adapter_conforms_to_registered_search_and_fetch_contracts -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

分离 request builder 与 pure parser，避免 fixture test 触发网络；运行：

```bash
uv run ruff check src/agent_search_gateway/providers/web/tavily.py tests/providers/web/test_tavily.py
uv run mypy src/agent_search_gateway/providers/web/tavily.py tests/providers/web/test_tavily.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 29: Firecrawl Search And Fetch Adapter

**Files:**
- Create: `src/agent_search_gateway/providers/web/firecrawl.py`
- Create: `tests/fixtures/providers/firecrawl/search.json`
- Create: `tests/fixtures/providers/firecrawl/scrape.json`
- Test: `tests/providers/web/test_firecrawl.py`
- Docs: `https://docs.firecrawl.dev/api-reference/endpoint/search`
- Docs: `https://docs.firecrawl.dev/api-reference/endpoint/scrape`

- [ ] **Step 1: Write the failing test**

创建 `test_firecrawl_adapter_maps_v2_search_and_scrape_payloads`：验证 bearer auth；search `/v2/search` 使用 web source 与 minimal scrape formats，映射 title/description/url，markdown→content、rawHtml→raw_content；fetch `/v2/scrape` 映射同一字段；只有 markdown 时用其作为 non-empty raw_content 以满足 fetch contract；`success=false`、missing data 或 empty body 为 execution failure。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/providers/web/test_firecrawl.py::test_firecrawl_adapter_maps_v2_search_and_scrape_payloads -v
```

Expected:

```text
FAIL because FirecrawlAdapter is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
build endpoint from configured api_url without duplicating /v2
search:
  POST /v2/search
  request url/title/description plus markdown/rawHtml formats
  parse data.web[]
fetch:
  POST /v2/scrape with one URL and markdown/rawHtml formats
  raw = rawHtml or markdown
  cleaned = markdown or ""
  require raw non-empty
```

不要把 screenshots、links、images 或 metadata 加入 core contracts。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/providers/web/test_firecrawl.py::test_firecrawl_adapter_maps_v2_search_and_scrape_payloads -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

复用一个 page-body mapper 处理 search/scrape；运行：

```bash
uv run ruff check src/agent_search_gateway/providers/web/firecrawl.py tests/providers/web/test_firecrawl.py
uv run mypy src/agent_search_gateway/providers/web/firecrawl.py tests/providers/web/test_firecrawl.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 30: Exa Search And Contents Adapter

**Files:**
- Create: `src/agent_search_gateway/providers/web/exa.py`
- Create: `tests/fixtures/providers/exa/search.json`
- Create: `tests/fixtures/providers/exa/contents.json`
- Test: `tests/providers/web/test_exa.py`
- Docs: `https://exa.ai/docs/reference/search`
- Docs: `https://exa.ai/docs/reference/get-contents`

- [ ] **Step 1: Write the failing test**

创建 `test_exa_adapter_maps_search_and_contents_without_deprecated_fields`：验证 `x-api-key` auth；search POST `/search` 且 content options 位于 `contents`；snippet 优先 first highlight、再 summary、再 title；text 映射为 raw/content；fetch POST `/contents` 使用 URLs 和 text，按 matching URL 解析；per-URL status error/empty text 为 execution failure；不得发送 deprecated `context/livecrawl/tokensNum`。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/providers/web/test_exa.py::test_exa_adapter_maps_search_and_contents_without_deprecated_fields -v
```

Expected:

```text
FAIL because ExaAdapter is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
search(query):
  POST {api_url}/search with query and contents.text=true, contents.highlights=true
  parse results[]
  body text is provider-cleaned; map raw_content=text and content=text
fetch(url):
  POST {api_url}/contents with urls=[url], text=true
  find normalized matching result
  check statuses for that URL
  require non-empty text
  return raw_content=text, content=text
```

只解析 core 所需字段，不保留 scores/images/cost metadata。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/providers/web/test_exa.py::test_exa_adapter_maps_search_and_contents_without_deprecated_fields -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

将 URL matching 与 status classification 放在 pure helper；运行：

```bash
uv run ruff check src/agent_search_gateway/providers/web/exa.py tests/providers/web/test_exa.py
uv run mypy src/agent_search_gateway/providers/web/exa.py tests/providers/web/test_exa.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 31: Linkup Search And Fetch Adapter

**Files:**
- Create: `src/agent_search_gateway/providers/web/linkup.py`
- Create: `tests/fixtures/providers/linkup/search.json`
- Create: `tests/fixtures/providers/linkup/fetch.json`
- Test: `tests/providers/web/test_linkup.py`
- Docs: `https://docs.linkup.so/pages/documentation/endpoints/search/reference`
- Docs: `https://docs.linkup.so/pages/documentation/endpoints/fetch/reference`

- [ ] **Step 1: Write the failing test**

创建 `test_linkup_adapter_maps_search_results_and_fetch_markdown`：验证 bearer auth；search POST `/v1/search` 使用 `q`、minimal depth、`outputType=searchResults`，映射 `name/url/content` 为 title/url/snippet，不把 search snippet 当 full body；fetch POST `/v1/fetch` 请求 markdown 与 raw HTML，映射 rawHtml or markdown→raw_content、markdown→content；missing/empty fields 为 execution failure。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/providers/web/test_linkup.py::test_linkup_adapter_maps_search_results_and_fetch_markdown -v
```

Expected:

```text
FAIL because LinkupAdapter is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
search(query):
  POST {api_url}/v1/search
  parse results[] objects of supported text type
  KeywordSearchHit(url, title=name, snippet=content, body fields empty)
fetch(url):
  POST {api_url}/v1/fetch with includeRawHtml=true, extractImages=false
  raw = rawHtml or markdown
  content = markdown or ""
  require raw non-empty
```

不要接入 Research/Tasks endpoint。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/providers/web/test_linkup.py::test_linkup_adapter_maps_search_results_and_fetch_markdown -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

将 endpoint join 与 response parser 分离；运行：

```bash
uv run ruff check src/agent_search_gateway/providers/web/linkup.py tests/providers/web/test_linkup.py
uv run mypy src/agent_search_gateway/providers/web/linkup.py tests/providers/web/test_linkup.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 32: Brave Keyword Search Adapter

**Files:**
- Create: `src/agent_search_gateway/providers/web/brave.py`
- Create: `tests/fixtures/providers/brave/search.json`
- Test: `tests/providers/web/test_brave.py`
- Docs: `https://api-dashboard.search.brave.com/api-reference/web/search/get`

- [ ] **Step 1: Write the failing test**

创建 `test_brave_adapter_maps_web_results_as_search_only_provider`：GET configured `/res/v1/web/search`，使用 `q` 与 `X-Subscription-Token`；映射 `web.results[].url/title/description`，body fields empty；extra snippets 不扩展 public contract；missing web results 可返回 empty list；malformed result 为 execution failure；registry capability 为 search=true/fetch=false。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/providers/web/test_brave.py::test_brave_adapter_maps_web_results_as_search_only_provider -v
```

Expected:

```text
FAIL because BraveAdapter is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
BraveAdapter.search(query):
  GET configured base + /res/v1/web/search
  query params q and minimal count
  auth X-Subscription-Token
  parse web.results[] into KeywordSearchHit
  snippet = description
  raw_content = content = ""
```

不实现 Brave local/news/image/LLM-context/fetch 功能。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/providers/web/test_brave.py::test_brave_adapter_maps_web_results_as_search_only_provider -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

保持 parser 只接受 web result shape；运行：

```bash
uv run ruff check src/agent_search_gateway/providers/web/brave.py tests/providers/web/test_brave.py
uv run mypy src/agent_search_gateway/providers/web/brave.py tests/providers/web/test_brave.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 33: AnySearch Keyword Search Adapter

**Files:**
- Create: `src/agent_search_gateway/providers/web/anysearch.py`
- Create: `tests/fixtures/providers/anysearch/search.json`
- Test: `tests/providers/web/test_anysearch.py`
- Docs: `https://www.anysearch.com/docs`

- [ ] **Step 1: Write the failing test**

创建 `test_anysearch_adapter_maps_unified_search_json_as_search_only_provider`：POST `/v1/search`，bearer auth，body 至少含 query、JSON format 与 bounded max results；根据官方 fixture 映射 URL/title/snippet；provider 返回的 full content 不自动进入 URL body，除非 fixture 明确区分为完整页面字段且 contract test覆盖；anonymous mode 不启用，因为设计要求 enabled provider secret；malformed schema 为 execution failure；capability search-only。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/providers/web/test_anysearch.py::test_anysearch_adapter_maps_unified_search_json_as_search_only_provider -v
```

Expected:

```text
FAIL because AnySearchAdapter is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
AnySearchAdapter.search(query):
  POST {api_url}/v1/search
  Authorization Bearer configured secret
  request JSON format and max_results
  parse only documented result collection
  map URL/title/snippet into KeywordSearchHit
  keep raw_content/content empty unless official response has unambiguous full-page fields covered by fixture
```

不要调用 MCP endpoint 或实现 vertical-tag routing；v0 只发通用 query。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/providers/web/test_anysearch.py::test_anysearch_adapter_maps_unified_search_json_as_search_only_provider -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

将官方 schema 访问集中在 parser helper，schema drift 产生清晰 execution failure；运行：

```bash
uv run ruff check src/agent_search_gateway/providers/web/anysearch.py tests/providers/web/test_anysearch.py
uv run mypy src/agent_search_gateway/providers/web/anysearch.py tests/providers/web/test_anysearch.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 34: TinyFish Search And Fetch Adapter

**Files:**
- Create: `src/agent_search_gateway/providers/web/tinyfish.py`
- Create: `tests/fixtures/providers/tinyfish/search.json`
- Create: `tests/fixtures/providers/tinyfish/fetch.json`
- Test: `tests/providers/web/test_tinyfish.py`
- Docs: `https://docs.tinyfish.ai/search-api/reference`
- Docs: `https://docs.tinyfish.ai/fetch-api/reference`

- [ ] **Step 1: Write the failing test**

创建 `test_tinyfish_adapter_uses_distinct_search_and_fetch_endpoints`：search 使用 configured `search_api_url`、GET query 与 `X-API-Key`；fetch 使用 configured `fetch_api_url`、POST `urls=[url]`、`format=markdown`；映射 search title/snippet/url；fetch matching result text→raw/content；batch response 中找不到 URL、per-URL error 或 empty text 为 execution failure；capability dual-stage。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/providers/web/test_tinyfish.py::test_tinyfish_adapter_uses_distinct_search_and_fetch_endpoints -v
```

Expected:

```text
FAIL because TinyFishAdapter is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
TinyFishAdapter(search_api_url, fetch_api_url, secret, http_executor)
search(query):
  GET search_api_url with query
  X-API-Key header
  parse structured results
fetch(url):
  POST fetch_api_url with urls=[url], format="markdown"
  find matching result
  require non-empty text
  return raw_content=text, content=text
```

不要启用 Agent/Browser APIs、proxy 参数或 multi-URL batching。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/providers/web/test_tinyfish.py::test_tinyfish_adapter_uses_distinct_search_and_fetch_endpoints -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

统一 matching URL normalization，但不改变 provider 返回 URL 字符串的 public normalization 规则；运行：

```bash
uv run ruff check src/agent_search_gateway/providers/web/tinyfish.py tests/providers/web/test_tinyfish.py
uv run mypy src/agent_search_gateway/providers/web/tinyfish.py tests/providers/web/test_tinyfish.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 35: Built-In Provider Registry And Runtime Assembly

**Files:**
- Create: `src/agent_search_gateway/providers/defaults.py`
- Create: `src/agent_search_gateway/runtime.py`
- Test: `tests/runtime/test_runtime_assembly.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (First-Version Adapter Set, Provider Stage Enablement, Component Catalog)

- [ ] **Step 1: Write the failing test**

创建 `test_runtime_assembly_builds_enabled_adapters_with_shared_quotas_and_closes_clients`：内置 registry 恰含 Tavily/Firecrawl/Exa/Linkup/Brave/AnySearch/TinyFish 的正确 capabilities；只实例化 enabled stages；同 web provider search/fetch 共用 quota 与 adapter/client；named LLM provider 各有独立 client/quota；unsupported stage startup fails；`Runtime.aclose()` 关闭每个 client 恰一次；secret 不出现在 repr/log。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/runtime/test_runtime_assembly.py::test_runtime_assembly_builds_enabled_adapters_with_shared_quotas_and_closes_clients -v
```

Expected:

```text
FAIL because built-in registrations and Runtime assembly are missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
build_default_registry():
  register seven web provider factories with exact capabilities/config keys

Runtime.build(resolved_config):
  create ProviderQuotaManager
  create one HttpJsonExecutor/client per enabled web adapter
  instantiate adapter once and expose it in search/fetch tuples as supported
  create one OpenAIChatCompletionsClient per referenced LLM provider
  create LLMStages with resolved invocations
  create URLStore, ResultWriter, SearchOrchestrator, FetchScheduler, FetchOrchestrator

Runtime.aclose():
  close unique adapter/LLM clients once
```

Runtime assembly 是唯一知道具体 adapter classes 的 core 文件；orchestrators 只依赖 contracts。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/runtime/test_runtime_assembly.py::test_runtime_assembly_builds_enabled_adapters_with_shared_quotas_and_closes_clients -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

去重 adapter/client ownership list，避免 search/fetch 双重 close；运行：

```bash
uv run ruff check src/agent_search_gateway/providers/defaults.py src/agent_search_gateway/runtime.py tests/runtime/test_runtime_assembly.py
uv run mypy src/agent_search_gateway/providers/defaults.py src/agent_search_gateway/runtime.py tests/runtime/test_runtime_assembly.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 36: Foreground Daemon Startup, Socket Server, And Dispatch

**Files:**
- Create: `src/agent_search_gateway/daemon.py`
- Test: `tests/daemon/test_daemon_dispatch.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Foreground Daemon, start data flow)
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (Daemon Request Decode Failure)

- [ ] **Step 1: Write the failing test**

创建 `test_daemon_loads_runtime_binds_socket_and_dispatches_typed_requests`：临时 paths 与 fake runtime；startup 创建 result/socket parent；config/runtime failure 为 `config_error` 并不留 socket；bind conflict 为 startup failure；真实 Unix socket 接收 keyword/llm/fetch request 并返回 single-line response；bad JSON/unknown type 返回 `bad_request`；typed `GatewayError` 转 ErrorResponse；unexpected exception 转 concise execution/protocol response 且记录日志、不泄漏内容。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/daemon/test_daemon_dispatch.py::test_daemon_loads_runtime_binds_socket_and_dispatches_typed_requests -v
```

Expected:

```text
FAIL because ForegroundDaemon is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
ForegroundDaemon.start(paths):
  load and validate config
  Runtime.build
  ensure socket parent/results dir
  asyncio.start_unix_server(handle_connection, socket path)
  enter serve loop in foreground

handle_connection(reader, writer):
  use NDJSONDecoder over reads
  for each decoded item:
    bad request item -> write ErrorResponse
    typed request -> dispatch
  one response line per request
  close connection on EOF

dispatch:
  keyword -> search_orchestrator.keyword_search
  llm -> search_orchestrator.llm_search
  url_fetch -> fetch_orchestrator.url_fetch
  shutdown delegated to Task 37
  map GatewayError to stable ErrorResponse
```

不要 auto-remove stale socket；bind error 必须可见。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/daemon/test_daemon_dispatch.py::test_daemon_loads_runtime_binds_socket_and_dispatches_typed_requests -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

分离 startup、connection framing、request dispatch 三个 focused methods，避免单一大函数；运行：

```bash
uv run ruff check src/agent_search_gateway/daemon.py tests/daemon/test_daemon_dispatch.py
uv run mypy src/agent_search_gateway/daemon.py tests/daemon/test_daemon_dispatch.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 37: Graceful Shutdown Lifecycle

**Files:**
- Modify: `src/agent_search_gateway/daemon.py` (shutdown coordinator and active task tracking)
- Test: `tests/daemon/test_daemon_shutdown.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Graceful Stop Command, stop data flow)
- Docs: `docs/designs/testings/agent-search-gateway-cli-v0.md` (Daemon Lifecycle)

- [ ] **Step 1: Write the failing test**

创建 event-driven test `test_shutdown_rejects_new_work_waits_or_cancels_active_requests_and_cleans_up`：首次 shutdown 设置 state；新 workflow 返回 `daemon_shutting_down`；重复 shutdown 成功并等待同一 coordinator；active request 在 grace 内完成则不取消；注入 timeout waiter 模拟超时后取消；provider clients close；socket server close 且文件删除；返回 `Daemon stopped.`；restart 使用新空 URL Store。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/daemon/test_daemon_shutdown.py::test_shutdown_rejects_new_work_waits_or_cancels_active_requests_and_cleans_up -v
```

Expected:

```text
FAIL because daemon shutdown state machine is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
track active workflow Tasks; exclude shutdown handlers
on workflow dispatch:
  if shutting_down: DAEMON_SHUTTING_DOWN
  register current task until finally

begin_shutdown():
  under state lock, create/reuse one shutdown_task
  set shutting_down=True immediately

shutdown coordinator:
  await snapshot of active workflow tasks with fixed 10s grace via injectable waiter
  on timeout cancel remaining and await cancellation completion
  await runtime.aclose()
  server.close(); await server.wait_closed()
  unlink socket if it is this daemon's socket
  set stopped event

shutdown request handler awaits coordinator then returns stable success
```

Cleanup 必须放在 `finally`，即使 client close 某一项失败也继续其余 cleanup 并记录短错误。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/daemon/test_daemon_shutdown.py::test_shutdown_rejects_new_work_waits_or_cancels_active_requests_and_cleans_up -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

用单一 shutdown task 保证 repeated stop 幂等，避免多个 cleanup races；运行：

```bash
uv run ruff check src/agent_search_gateway/daemon.py tests/daemon/test_daemon_shutdown.py
uv run mypy src/agent_search_gateway/daemon.py tests/daemon/test_daemon_shutdown.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 38: CLI Commands, Rendering, And Exit Codes

**Files:**
- Create: `src/agent_search_gateway/cli.py`
- Modify: `pyproject.toml` (`[project.scripts] agent-search-gateway = "agent_search_gateway.cli:main"`)
- Test: `tests/cli/test_cli.py`
- Docs: `docs/designs/architectures/agent-search-gateway-cli-v0.md` (Single CLI Entry Point, Plain Text Command Output)
- Docs: `docs/designs/error-handlings/agent-search-gateway-cli-v0.md` (User-Facing Output)

- [ ] **Step 1: Write the failing test**

创建参数化测试 `test_cli_renders_exact_stdout_stderr_and_exit_codes`：parser 有 start/stop/keyword-search/llm-search/url-fetch；empty query/prompt 与 invalid URL 在 client call 前失败；workflow missing daemon→stderr start instruction/non-zero；stop missing daemon→stdout `Daemon is not running.`/zero；success stdout 仅 text；ErrorResponse stderr 仅 message、stdout empty、non-zero；stop 发 shutdown；start 前台运行 daemon factory；focus positional optional且纯空白归一为 null。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/cli/test_cli.py::test_cli_renders_exact_stdout_stderr_and_exit_codes -v
```

Expected:

```text
FAIL because CLI entrypoint is missing
```

- [ ] **Step 3: Describe the minimal implementation**

```text
build_parser():
  start
  stop
  keyword-search QUERY
  llm-search PROMPT
  url-fetch URL [FOCUS]

async run_command(args, paths, client, daemon_factory):
  start -> await foreground daemon
  other commands -> validate local inputs, build typed request, send_request
  stop + DaemonUnavailable -> print success message, exit 0
  workflow + DaemonUnavailable -> print start instruction to stderr, exit non-zero
  SuccessResponse -> stdout text only
  ErrorResponse/ProtocolFailure/InputFailure -> stderr concise message only

main(argv=None):
  asyncio.run
  return/use SystemExit with stable numeric codes
```

不要打印 progress、JSON envelope、traceback 或额外提示到 stdout。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/cli/test_cli.py::test_cli_renders_exact_stdout_stderr_and_exit_codes -v
uv run pytest -v
```

Expected:

```text
Target test PASS, all tests PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

将 parser、async command execution、sync main 分离，测试不 patch `sys.argv` 或全局 streams；运行：

```bash
uv run ruff check src/agent_search_gateway/cli.py tests/cli/test_cli.py
uv run mypy src/agent_search_gateway/cli.py tests/cli/test_cli.py
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 39: No-Network End-To-End Acceptance Tests

**Files:**
- Create: `tests/acceptance/test_gateway_workflows.py`
- Test: `tests/acceptance/test_gateway_workflows.py`
- Docs: `docs/designs/testings/agent-search-gateway-cli-v0.md` (Acceptance Criteria)

- [ ] **Step 1: Write the failing test**

创建 `test_real_socket_workflows_match_public_contract_without_network`：在 temp home 启动真实 daemon/socket，但注入 fake runtime；通过真实 protocol client执行 keyword-search→读取 jsonl→url-fetch full content→focus summary→llm-search→stop；断言 jsonl 仅 URL/abstract、同 URL fetch provider 一次、stdout-equivalent text 精确、socket 删除；重启 daemon 后同 URL `url_not_admitted`，证明状态未持久化。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/acceptance/test_gateway_workflows.py::test_real_socket_workflows_match_public_contract_without_network -v
```

Expected:

```text
FAIL at the first incomplete integration boundary
```

- [ ] **Step 3: Describe the minimal implementation**

```text
start ForegroundDaemon as asyncio task with injected Runtime factory
wait for socket-ready event, not wall-clock sleep
send real typed NDJSON requests over Unix socket
use fake providers/LLM with deterministic outputs and call recording
inspect result files on disk
perform shutdown request and await daemon task
construct fresh runtime for restart and verify URL store empty
```

若测试暴露 contract glue bug，只修正最小连接代码，不新增 acceptance-only production hook；依赖注入应复用现有 constructors。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/acceptance/test_gateway_workflows.py::test_real_socket_workflows_match_public_contract_without_network -v
uv run pytest -v
```

Expected:

```text
Acceptance test PASS, all tests PASS without network
```

- [ ] **Step 5: Refactor (keep tests green)**

把 acceptance fixture 限定为 tests/support，不向 production package 添加 fake/provider test mode；运行：

```bash
uv run ruff check tests/acceptance tests/support
uv run mypy tests/acceptance tests/support
uv run pytest -v
```

Expected:

```text
All checks PASS
```

### Task 40: Executable Documentation, Opt-In Integration Tests, And CI

**Files:**
- Create: `README.md`
- Modify: `config.example.toml` (final verified provider matrix and comments)
- Create: `tests/docs/test_documented_config.py`
- Create: `tests/integration/test_live_tavily_and_openai.py`
- Create: `.github/workflows/ci.yml`
- Test: `tests/docs/test_documented_config.py`
- Docs: `docs/designs/testings/agent-search-gateway-cli-v0.md` (Opt-In Integration Tests, Acceptance Criteria)

- [ ] **Step 1: Write the failing test**

创建 `test_example_config_loads_with_stub_secrets_and_readme_commands_match_cli_help`：为 example 中 env names 注入 dummy values，断言 config 完整解析、provider capability 合法；从 README code blocks 提取五个命令并与 parser subcommands 对齐；集成测试模块在缺 `WEB_SEARCH_RUN_INTEGRATION=1` 时明确 skip，不访问网络。

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/docs/test_documented_config.py::test_example_config_loads_with_stub_secrets_and_readme_commands_match_cli_help -v
```

Expected:

```text
FAIL because README/final example config/validation test is incomplete
```

- [ ] **Step 3: Describe the minimal implementation**

```text
README sections:
  scope and v0 limitations
  uv install/sync
  config path and api_key_env setup
  provider capability matrix
  start/stop/keyword-search/llm-search/url-fetch examples
  stdout/stderr and result jsonl contract
  in-memory state and search-admission rule
  test/lint/typecheck commands
  opt-in integration variables

integration tests:
  module-level skip unless WEB_SEARCH_RUN_INTEGRATION=1
  Tavily basic search response shape when TAVILY_API_KEY exists
  OpenAI-compatible basic chat response shape when OPENAI_API_KEY exists
  no behavioral assertions beyond connectivity/schema

CI:
  install uv
  uv sync --locked
  ruff check
  mypy
  pytest excluding opt-in network by default
```

README 不承诺未来功能，不包含真实 secret，不要求 integration tests 通过 normal CI。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

Run:

```bash
uv run pytest tests/docs/test_documented_config.py::test_example_config_loads_with_stub_secrets_and_readme_commands_match_cli_help -v
uv run pytest -v
uv run ruff check .
uv run mypy src tests
```

Expected:

```text
Documentation contract test PASS; default suite performs no real network; lint/type check PASS
```

- [ ] **Step 5: Refactor (keep tests green)**

删除 README 与 config example 中重复或失效的参数说明，最终执行：

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

Expected:

```text
All default verification PASS with no provider credentials
```

## Self-Review

### Spec Coverage

| Spec area | Plan tasks |
|---|---|
| Python/uv package and thin CLI | 1, 3, 18, 19, 38 |
| Foreground daemon and Unix NDJSON protocol | 18, 19, 36, 37, 39 |
| Five-field URL object and first-write state | 4, 5, 21, 25–27 |
| Keyword search workflow | 20, 21, 28–35 |
| LLM search workflow | 8, 13, 14, 16, 22 |
| URL fetch admission/content/safety/focus | 15, 16, 23–27 |
| Execution failure vs semantic rejection | 2, 12, 15, 20–27, 36, 38 |
| Provider capability/config/secret validation | 6–8, 28–35 |
| Shared web quota and separate LLM quota | 9, 13, 20, 23, 24, 35 |
| Per-URL singleflight and different-URL concurrency | 10, 24, 27 |
| Graceful stop, repeated stop, timeout cancellation | 37, 38, 39 |
| Result jsonl and plain output | 17, 20–22, 38–40 |
| Adapter parser coverage for all first-version providers | 28–34 |
| No-network default tests and opt-in live checks | 1, 6, 9–40 |
| Logging and secret/content protection | 7, 12, 35–37, 40 |

当前三个设计文档中的显式 acceptance items 均有对应任务。Architecture 的 open question 采用本计划前述最小决策：`available=false` 后续搜索可见，但跳过 body validation/body mutation，不提供自动恢复。

### Type Consistency

- 全计划统一使用 `NormalizedURL`，没有出现第二个 canonical URL 类型。
- URL 状态始终由 `URLStore` 的 frozen `URLRecord` snapshot 管理；provider adapter 只返回 `KeywordSearchHit`/`URLFetchCandidate`。
- Semantic judgment 始终是 `StageDecision(ok=False)`；execution path 始终使用 typed failure 或 `FetchOutcome(kind="execution_failure")`。
- `LLMInvocation(provider, model, extra_body)` 在 config、LLM adapter、stage 与 LLM search 任务中字段名一致。
- Search/Fetch orchestrator 成功返回文本、失败抛 `GatewayError`；只有 daemon 转换为 `SuccessResponse`/`ErrorResponse`，CLI 只负责渲染。
- Quota、singleflight、per-URL lock 分工固定：quota 限 provider，singleflight 合并 exact request，per-URL lock 串行同 URL 的不同 request。

### Final Implementation Gate

实现完成前必须运行并通过：

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

默认命令不得发起真实 provider 网络请求，不得要求任何 API key。