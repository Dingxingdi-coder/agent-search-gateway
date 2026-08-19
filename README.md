# agent-search-gateway

`agent-search-gateway` is a local foreground daemon plus a thin CLI for aggregated keyword search, LLM-assisted search, and fetching URLs that were admitted by search.

Version 0.1 keeps all URL admission and body state in daemon memory. Restarting the daemon clears that state. The daemon communicates over a local Unix-domain socket, so this version targets Unix-like environments. It does not provide persistent state, remote daemon access, or automatic recovery for URLs that have been marked unavailable.

## Install and sync

The project is managed with `uv` and a locked dependency graph:

```bash
uv sync --locked
```

The console entry point is installed as `agent-search-gateway`.

## Configuration

The daemon reads:

```text
~/.config/agent-search-gateway-cli/config.toml
```

Start from `config.example.toml`, then set each provider's `api_key_env` to the name of an environment variable available to the daemon process. The configuration file stores environment-variable names only; credentials themselves remain in the environment.

Runtime files are stored under:

```text
~/.cache/agent-search-gateway-cli/daemon.sock
~/.cache/agent-search-gateway-cli/results/
```

### Web provider capabilities

| Provider | Keyword search | URL fetch |
|---|---:|---:|
| Tavily | yes | yes |
| Firecrawl | yes | yes |
| Exa | yes | yes |
| Linkup | yes | yes |
| Brave | yes | no |
| AnySearch | yes | no |
| TinyFish | yes | yes |

Web search and fetch stages for the same provider share one concurrency quota. LLM provider transports have their own independent quotas.

## Commands

Run the daemon in the foreground:

```bash
agent-search-gateway start
```

Stop the daemon:

```bash
agent-search-gateway stop
```

Run keyword search:

```bash
agent-search-gateway keyword-search "query text"
```

Run LLM-assisted search:

```bash
agent-search-gateway llm-search "research prompt"
```

Fetch a URL that has already been admitted by a successful search. The final positional focus is optional:

```bash
agent-search-gateway url-fetch "https://example.com/article" "pricing details"
```

`keyword-search` and `llm-search` print the absolute path of a newly created JSONL result file. Each line contains exactly two public fields:

```json
{"url":"https://example.com/article","abstract":"Short search abstract"}
```

A URL must first be admitted by keyword or LLM search before `url-fetch` can use it. Admission, cached body content, and unavailable state are in memory only. A daemon restart therefore requires searching again before fetching the same URL.

Successful command output is written to stdout as plain text only. Errors are written to stderr. The CLI does not print protocol envelopes, progress messages, or tracebacks to stdout.

## Verification

Run the default no-network checks with:

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

The default test suite uses fakes and mock transports and does not require provider credentials.

## Opt-in live integration checks

Live connectivity tests are disabled by default. To opt in, set:

```text
WEB_SEARCH_RUN_INTEGRATION=1
TAVILY_API_KEY=...
OPENAI_API_KEY=...
```

`OPENAI_MODEL` may optionally select the chat-completions model used by the OpenAI-compatible connectivity check. These integration checks validate connectivity and basic response shape only; normal CI does not enable them.
