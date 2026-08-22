# Testing: Academic Paper Search

## 1. Strategy

The academic-search feature should preserve the repository's current no-network default test philosophy. Most coverage belongs in pure normalization/aggregation tests and provider fixture tests. Live API checks remain opt-in smoke tests only.

The test pyramid is intentionally weighted toward paper identity/merge logic because a bad adapter usually removes one source, while a bad identity merge can silently corrupt results across all sources.

```text
                 acceptance
                    small
                     ▲
             orchestrator/runtime
                     ▲
              provider fixtures
                     ▲
       normalization / PaperAggregator
                   large
```

Primary goals:

- prove existing keyword-search, URL-fetch, and default LLM web-search behavior does not regress;
- prove paper identity normalization is deterministic and conservative;
- prove multi-source clustering handles transitive strong identifiers;
- prove merge results do not depend on async completion order;
- prove malformed individual provider records are isolated;
- prove partial provider/LLM branch failures do not discard valid results;
- prove optional Unpaywall resolution is invoked only after deduplication and never becomes a search requirement;
- prove result files remain atomic and backward-compatible for existing default scopes.

---

## 2. Test Layout

Recommended additions, following current repository organization:

```text
tests/
├── academic/
│   ├── test_identifier_normalization.py
│   ├── test_paper_aggregator_identity.py
│   ├── test_paper_aggregator_merge.py
│   └── test_oa_enrichment.py
├── providers/
│   ├── academic/
│   │   ├── test_arxiv.py
│   │   ├── test_semantic_scholar.py
│   │   ├── test_openalex.py
│   │   ├── test_dblp.py
│   │   ├── test_crossref.py
│   │   ├── test_core.py
│   │   └── test_unpaywall.py
│   └── test_http_executor.py
├── orchestrators/
│   ├── test_paper_search_pipeline.py
│   └── test_llm_search.py              # extend existing file or split scoped cases
├── runtime/
│   ├── test_academic_runtime_assembly.py
│   └── test_quota_manager.py           # extend existing quota tests
├── unit/
│   ├── test_paper_search_parser.py
│   ├── test_protocol_codec.py          # extend
│   ├── test_result_writer.py           # extend
│   └── test_config_academic_providers.py
├── cli/
│   └── test_cli.py                      # extend
├── acceptance/
│   └── test_gateway_workflows.py        # extend
└── fixtures/
    └── providers/
        └── academic/
            ├── arxiv-success.xml
            ├── dblp-success.xml
            ├── semantic-success.json
            ├── openalex-success.json
            ├── crossref-success.json
            ├── core-success.json
            └── unpaywall-success.json
```

Do not require all filenames to match this proposal exactly if existing test organization makes a nearby location clearer, but keep pure aggregation tests separate from provider-protocol tests.

---

## 3. Identifier Normalization Tests

### 3.1 DOI

Table-driven tests must show that equivalent forms normalize to one canonical DOI:

```text
https://doi.org/10.1000/ABC
http://dx.doi.org/10.1000/ABC
doi:10.1000/ABC
DOI: 10.1000/ABC
10.1000/abc

=> 10.1000/abc
```

Also test:

- surrounding whitespace;
- URL-encoded DOI path components where safe to decode;
- trailing punctuation introduced by citation text should not be stripped beyond explicitly supported DOI syntax;
- clearly invalid strings return no normalized DOI / are rejected according to caller contract;
- normalization is idempotent.

### 3.2 arXiv

Equivalent representations:

```text
2401.12345
2401.12345v1
arXiv:2401.12345v3
https://arxiv.org/abs/2401.12345v2
https://arxiv.org/pdf/2401.12345v4.pdf

=> 2401.12345
```

Include legacy arXiv identifier forms if adapter support requires them; otherwise document them as out of scope rather than partially normalizing them.

Test that version suffixes do not change work identity.

### 3.3 Source-Native IDs

Test canonicalization for:

- Semantic Scholar paper ID;
- OpenAlex `W...` with and without `https://openalex.org/` prefix;
- dblp record key / canonical record URL;
- CORE work ID.

Source-native identity must always be namespaced by source, so identical raw strings from different providers do not collide.

### 3.4 Bibliographic Fingerprint

Pure tests cover normalized title, author evidence, and year compatibility.

Must merge:

```text
"  Attention   Is All You Need "
"attention is all you need"
```

with compatible normalized author/year evidence.

Must not merge solely because strings are similar:

```text
"Attention Is All You Need"
"Attention Is All You Need: A Survey"
```

Strong identifier conflicts must override a matching weak fingerprint.

---

## 4. PaperAggregator Identity Tests

This is the most important test group.

### 4.1 Direct Strong-ID Merge

Cases:

```text
A DOI=10.x/foo
B DOI=10.x/foo
=> 1 record
```

```text
A arxiv=2401.1
B arxiv=2401.1
=> 1 record
```

```text
A source=openalex source_id=W1
B source=openalex source_id=W1
=> 1 record unless strong identifier contradiction exists
```

### 4.2 Transitive Bridge Merge

Required regression case:

```text
A: DOI=10.x/foo
B: arxiv=2401.1
C: DOI=10.x/foo, arxiv=2401.1

aggregate(A, B, C)
=> exactly 1 PaperRecord
```

Test the same candidates in multiple orders. The final identity set and fields must be equivalent.

### 4.3 Conflict Rejection

```text
A: source=openalex, id=W1, DOI=10.x/foo
B: source=openalex, id=W1, DOI=10.x/bar

=> B rejected as identifier_conflict
=> A cluster remains valid
```

Also test that two different explicit DOIs never merge due only to title/author/year fingerprint.

### 4.4 Conservative Weak Merge

Cases without strong IDs:

- exact normalized title + compatible author evidence + same year -> merge;
- exact title + same authors + one missing year -> merge only if policy explicitly permits missing-year compatibility;
- exact title + incompatible year -> separate;
- exact title + incompatible authors -> separate;
- similar but non-equal normalized title -> separate.

The test names should encode the false-merge avoidance invariant.

### 4.5 Stable Result Ordering

Given configured provider priority and discovery order, repeated aggregation must produce stable record order even when candidate task completion order differs.

Use permutations or a representative set of reordered inputs; do not rely on hash/dict incidental ordering.

---

## 5. PaperAggregator Merge Tests

### 5.1 Deterministic Field Selection

Construct the same logical paper from several providers with intentionally different fields:

```text
dblp:
  title/authors/venue, no abstract

arXiv:
  title/authors/abstract/pdf_url/arxiv_id

OpenAlex:
  title/abstract/citation_count/topics/DOI

Crossref:
  title/authors/venue/publisher-facing URL/DOI/citation_count

Semantic Scholar:
  abstract/citation_count/fields of study/DOI
```

Assert the final `PaperRecord` is identical for all input permutations.

### 5.2 Identifier Union

Assert all compatible known IDs survive merge:

```text
PaperIdentifiers(
  doi="...",
  arxiv_id="...",
  semantic_scholar_id="...",
  openalex_id="...",
  dblp_key="...",
  core_id="...",
)
```

No provider should be able to erase an identifier supplied by another source.

### 5.3 Citation Counts Preserve Provenance

Given:

```text
OpenAlex=130
Semantic Scholar=127
Crossref=91
```

assert:

```python
citation_counts == {
    "openalex": 130,
    "semantic_scholar": 127,
    "crossref": 91,
}
```

Do not test for a synthetic max/average count because the design intentionally avoids one.

### 5.4 Topics and Sources Stable Union

Duplicates and case/whitespace variants should normalize according to policy, with stable output order.

`source` provenance should not duplicate providers and should not depend on task completion order.

### 5.5 Abstract and PDF Precedence

Test that:

- a non-empty academic API abstract can beat an LLM-derived abstract according to explicit precedence;
- an empty abstract never replaces a non-empty one;
- an invalid PDF URL is discarded;
- trusted direct/OA provider PDF URL wins over weaker LLM-provided locator under the chosen policy;
- OA resolver enrichment does not replace a stronger verified direct PDF URL with a weaker landing URL.

---

## 6. Provider Adapter Fixture Tests

Each academic adapter should use local mock HTTP transport and static fixtures. Default unit/provider tests must make no external network requests.

Common assertions for all six discovery providers:

- expected HTTP method and endpoint;
- expected query parameters;
- expected authentication/contact headers/params without exposing actual secret values;
- mapped `source` and `source_id`;
- title/authors/abstract mapping;
- DOI/arXiv/source-ID extraction;
- publication/update date mapping;
- landing URL/PDF URL mapping;
- venue/topics/citation/OA fields when supported;
- one malformed item is skipped/rejected while valid siblings remain;
- malformed top-level response raises `ProtocolFailure` rather than returning empty results;
- HTTP failure propagates through shared executor semantics.

### 6.1 arXiv

Fixture coverage:

- Atom feed with multiple entries;
- versioned arXiv IDs normalize to work IDs;
- PDF link extraction;
- DOI when present;
- categories;
- malformed single entry isolation;
- malformed XML envelope failure.

No `feedparser` dependency is required unless implementation chooses it deliberately; tests should validate behavior, not library choice.

### 6.2 Semantic Scholar

Coverage:

- optional API key header when configured;
- no-key request path when not configured;
- `externalIds.DOI`;
- `paperId`;
- authors, publication date, citation count, fields of study;
- `openAccessPdf.url`;
- missing abstract/OA fields;
- invalid response envelope.

Do not copy the reference repository's fallback from a rejected API key to unauthenticated access unless separately designed. Configuration/HTTP behavior should be explicit and testable.

### 6.3 OpenAlex

Coverage:

- work ID normalization from full OpenAlex URL;
- DOI URL normalization;
- reconstruction of `abstract_inverted_index`;
- authorships;
- `primary_location` landing/PDF;
- `open_access` metadata;
- `cited_by_count`;
- concepts/topics;
- publication date.

Abstract reconstruction deserves its own pure unit test for ordering and empty indexes.

### 6.4 dblp

Coverage:

- XML search response;
- title/authors/venue/year/key/record URL;
- DOI extraction from electronic-edition fields when provided;
- missing abstract remains valid;
- malformed one-hit structure isolated;
- malformed XML envelope failure.

Do not add HTML scraping fallback in the first version unless it is separately approved; tests should enforce one explicit API contract rather than hidden transport fallback behavior.

### 6.5 Crossref

Coverage:

- works search envelope;
- DOI/title/authors/container title/date;
- citation count keyed as Crossref provenance;
- resource/link PDF URL when present;
- missing abstract is valid;
- contact `mailto` option if configured/designed;
- malformed envelope failure.

Do not inject fake epoch dates when no publication date exists; expected final value is `None`.

### 6.6 CORE

Coverage:

- API key header when configured/required;
- result ID/title/authors/abstract/DOI;
- publication date;
- repository/subjects/tags if mapped to venue/topics;
- download/full-text URL extraction;
- malformed record isolation;
- response envelope failure.

Do not copy provider-local retry loops; mock executor tests should prove retries remain centralized.

---

## 7. Unpaywall Resolver Tests

Use local JSON fixtures and mock transport.

Required cases:

```text
best_oa_location.url_for_pdf exists
=> OAResolution.pdf_url uses it
```

```text
best location has no PDF but has usable landing URL
=> landing URL retained, PDF may remain None
```

```text
best location absent; alternate OA locations contain PDF
=> use deterministic first acceptable fallback if implementation supports this policy
```

```text
HTTP 404
=> normal None/miss
```

```text
record is_oa=false
=> normal resolution/miss, no search failure
```

```text
timeout / terminal 429 / 5xx / malformed JSON
=> resolver failure surfaced to caller
```

Then test at orchestrator/enrichment layer that resolver failure is caught and the original paper remains in output.

### 7.1 Deduplicate Before Resolve

Critical call-count test:

```text
six discovery hits
all normalize to same DOI
=> aggregator returns one paper
=> resolver called exactly once
```

Also assert papers without DOI do not call Unpaywall.

---

## 8. Direct PaperSearchOrchestrator Tests

Mirror the existing keyword-search completion tests.

### 8.1 Input and Provider Presence

```text
paper_search("   ")
=> EMPTY_QUERY
```

```text
paper_search("query") with no providers
=> NO_ACADEMIC_SEARCH_PROVIDERS
```

### 8.2 Partial Provider Failure

```text
provider A -> ExecutionFailure
provider B -> valid hit
=> command succeeds; one paper result file
```

```text
provider A -> ExecutionFailure
provider B -> []
=> command succeeds; empty result file
```

```text
all providers -> failure
=> ALL_PROVIDERS_FAILED
=> no new paper result file
```

### 8.3 Aggregation Integration

Two or more fake providers return duplicates with complementary identifiers/metadata. Assert output contains one merged `PaperRecord` and stable provenance.

### 8.4 Academic Quotas

Use controlled fake providers/events to assert each provider acquires `get_academic(provider.name)` and never exceeds configured concurrency.

Existing web and LLM quota namespaces must remain independent.

### 8.5 URL Admission

For final paper with abstract:

```text
URLStore[paper.url].abstract == paper.abstract
```

For metadata-only paper:

```text
paper.abstract == ""
URLStore[paper.url].abstract == paper.title
```

Assert `paper.pdf_url` is not automatically present in `URLStore` unless it happens to equal the landing URL.

---

## 9. LLM Paper Parser Tests

Create a restricted paper grammar equivalent in strictness to the existing web parser.

Test:

- one valid `## Paper` block;
- repeated valid blocks;
- required field missing;
- optional field empty;
- multiple authors representation;
- DOI/arXiv normalization occurs at aggregation boundary, not by permissive parser guessing unless explicitly designed;
- invalid date/citation syntax;
- arbitrary Markdown outside allowed format;
- web `## Result` block rejected by paper parser;
- mixed `## Result` + `## Paper` response rejected rather than partially parsed;
- prompt/model raw text absent from error logs.

Parser failure should raise `ParserFailure`, matching existing restricted parser behavior.

---

## 10. Scoped LLM Search Tests

Extend current `tests/orchestrators/test_llm_search.py` behavior rather than replacing it.

### 10.1 Backward Compatibility

These two invocations must be behaviorally equivalent:

```bash
agent-search-gateway llm-search "prompt"
agent-search-gateway llm-search "prompt" --scope web
```

Assert:

- same web system prompt path;
- same existing parser;
- same provider invocation count;
- same URLStore admission semantics;
- same result filename kind;
- exact existing line schema `{"url","abstract"}` with no `type` field.

Existing tests for `llm_search` should continue passing with minimal/no fixture changes.

### 10.2 Paper Scope

For multiple configured LLM invocations:

- valid paper results from independent invocations are aggregated;
- duplicate papers are deduplicated by `PaperAggregator`;
- one parser failure does not fail completed invocations;
- all invocation failures -> `ALL_PROVIDERS_FAILED`;
- landing URLs are admitted after final aggregation;
- result file contains only paper schema, no `type` discriminator.

### 10.3 All Scope Runs Branches Concurrently

Use `asyncio.Event` controlled clients to prove the web and paper semantic branches are scheduled concurrently rather than serially.

The test should fail if paper branch is not started until web branch finishes, and vice versa.

### 10.4 All Scope Partial Success Matrix

Test all four combinations:

| Web | Paper | Expected |
|---|---|---|
| success | success | one mixed file with both types |
| success | failure | one mixed-mode file with web lines only |
| failure | success | one mixed-mode file with paper lines only |
| failure | failure | `ALL_PROVIDERS_FAILED`, no file |

A successful empty branch still counts as success.

### 10.5 Shared LLM Quota

When `scope=all` causes two semantic calls for the same configured LLM provider, assert the existing LLM `CapacityGate` limits both. No new independent quota namespace should let the combined search exceed configured concurrency.

---

## 11. ResultWriter Tests

Keep existing `SearchRecord` serializer tests unchanged where possible.

### 11.1 Existing Web Serialization Regression

Assert exact bytes for existing web-only search remain unchanged:

```json
{"url":"https://example.com/a","abstract":"A"}
```

No `type` field is added to default keyword or LLM web-only output.

### 11.2 Paper Serialization

Assert deterministic compact JSON representation of:

- title;
- authors array;
- abstract, including valid empty abstract;
- nested identifiers;
- ISO dates / null;
- landing URL / nullable PDF URL;
- venue/topics;
- citation-count map;
- OA fields;
- sources array.

Reject invalid final `PaperRecord` invariants before file creation.

### 11.3 Mixed Serialization

Given web and paper records:

```json
{"type":"web",...}
{"type":"paper",...}
```

Assert the discriminator is added only during serialization; domain objects are not mutated/replaced with generic dictionaries earlier in the pipeline.

### 11.4 Atomic Failure Behavior

Use a deliberately invalid later record to prove serialization occurs before target creation. Use an injected/write failure if existing test support permits to prove partial target cleanup remains intact.

---

## 12. Protocol and CLI Tests

### 12.1 CLI

Add cases:

```text
paper-search "query"
llm-search "prompt" --scope web
llm-search "prompt" --scope paper
llm-search "prompt" --scope all
```

Invalid scope should be rejected by argparse without contacting the daemon.

Default `llm-search "prompt"` continues producing `scope=web` semantics.

### 12.2 Protocol Codec

Test:

- encode/decode `PaperSearchRequest`;
- decode legacy LLM request without `scope` as web;
- encode/decode explicit web/paper/all scopes;
- reject unknown scope;
- reject unknown extra keys while permitting the one new optional `scope` key;
- response contract remains `SuccessResponse(text=single_path)`.

### 12.3 Request IDs / Result Kinds

Extend result-kind tests for `paper-<request_id>.jsonl`. Existing keyword/llm names remain unchanged.

---

## 13. Configuration Tests

Academic configuration must be tested independently from current web-provider config to prove the two credential models do not bleed into each other.

Required scenarios:

```text
arXiv enabled, no API key env
=> valid
```

```text
dblp enabled, no API key env
=> valid
```

```text
Semantic Scholar optional key absent
=> valid unauthenticated configuration if that policy is implemented
```

```text
Semantic Scholar key env configured and present
=> secret resolved
```

```text
CORE configured to require key; env missing
=> CONFIG_ERROR
```

```text
Unpaywall absent
=> resolver disabled, configuration valid
```

```text
Unpaywall explicitly enabled + contact env present
=> resolver configured
```

```text
Unpaywall explicitly enabled + contact env missing
=> CONFIG_ERROR
```

Also test unknown providers, unknown keys, invalid booleans/integers, non-positive concurrency, and reserved constructor options.

Existing `resolve_web_provider_config` tests must pass unchanged to demonstrate that academic configuration did not weaken web config invariants.

---

## 14. Runtime Assembly Tests

Construct runtime from resolved config using mock HTTP client factories.

Assert:

- only enabled academic providers are instantiated;
- correct provider order is preserved;
- academic quota gates use configured limits;
- resolver is `None` when unconfigured;
- exactly one Unpaywall resolver is built when enabled;
- academic HTTP executors are closed by `Runtime.aclose`;
- existing web/LLM provider assembly remains unchanged;
- `PaperSearchOrchestrator` receives shared URLStore and ResultWriter paths;
- LLM paper aggregation uses the same aggregation policy/component as direct paper search rather than a duplicate implementation.

---

## 15. HTTP Executor Regression Tests

The shared executor extension must prove no regression in current JSON clients.

Keep all existing `request_json` retry/status/decode tests.

Add:

- GET query `params` are passed without being logged as endpoint metadata when logging policy excludes query strings;
- `request_text` returns response text for successful XML/Atom calls;
- `request_text` shares timeout/retry/status behavior with `request_json`;
- text mode does not attempt JSON decoding;
- JSON mode still raises `ProtocolFailure` for invalid JSON;
- secrets/query contents remain absent from structured logs.

If implementation refactors a shared internal request method, test public behavior rather than private helper shape.

---

## 16. Observability Tests

Use the existing structured-test logger utilities.

Assert representative events:

```text
provider_started provider=openalex stage=paper_search
provider_completed provider=openalex stage=paper_search results=<n>
paper_candidate_rejected provider=dblp reason=missing_title
paper_enrichment_failed resolver=unpaywall stage=oa_resolve
llm_search_branch_failed scope=paper
results_written kind=paper|llm
```

Assert logs do not contain sentinel values placed in:

- user paper query;
- LLM prompt;
- LLM malformed output;
- paper title;
- paper abstract;
- API secret/contact value.

Tests should prefer fixed event/reason assertions over exact full log lines to avoid brittle timing-field coupling.

---

## 17. Acceptance Tests

Extend end-to-end daemon/CLI tests with fakes only.

### 17.1 `paper-search`

Start a controlled daemon runtime with fake academic providers, invoke the CLI request path, and assert:

- stdout contains only one absolute result path;
- file exists and contains expected merged paper JSONL;
- stderr/business stdout is not polluted by debug events;
- paper landing URL can subsequently be passed to existing `url-fetch` because it was admitted;
- PDF URL is not automatically admitted.

### 17.2 `llm-search --scope all`

Controlled LLM clients return one web result and one paper result. Assert:

- stdout contains one result path;
- one JSONL file contains typed web and paper lines;
- request ID correlation remains one request ID/result filename;
- default `llm-search` acceptance behavior remains unchanged.

### 17.3 Shutdown/Failure Regression

Academic requests should participate in existing active-request/shutdown coordination exactly like current business workflows. No separate daemon lifecycle behavior is introduced.

---

## 18. Optional Live Integration Tests

Live tests remain disabled by default and are not required for CI.

A future/feature implementation may introduce:

```text
ACADEMIC_SEARCH_RUN_INTEGRATION=1
```

Prefer a very small smoke set such as OpenAlex and/or arXiv because they can validate connectivity without making every developer configure all academic credentials.

Live checks should validate only:

- endpoint reachable;
- response can be parsed into at least the expected top-level shape;
- one real query can produce valid `PaperSearchHit` values when results exist.

Do not use live tests as the primary parser/merge test source; fixtures provide deterministic coverage.

---

## 19. Verification Gate

Before implementation is considered ready, run the repository's existing full no-network gate:

```text
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

Required regression expectations:

- all pre-feature tests still pass;
- existing default `llm-search` output bytes remain unchanged;
- existing web provider config behavior remains unchanged;
- keyword search and URL fetch acceptance tests remain unchanged except where shared fakes need additive constructor defaults;
- no default test performs real academic-provider network access.

The most important feature-specific release blockers are failures in identifier normalization, transitive clustering, deterministic merge permutation tests, partial-success semantics, and legacy LLM web-output regression tests.
