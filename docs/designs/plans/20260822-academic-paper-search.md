# Academic Paper Search Implementation Plan

**Goal:** Add first-class academic paper discovery to `agent-search-gateway` with arXiv, Semantic Scholar, OpenAlex, dblp, Crossref, and CORE discovery, optional Unpaywall OA enrichment, deterministic cross-provider paper identity/merge semantics, direct `paper-search`, and scoped LLM paper search without changing existing web-search behavior.
**Architecture:** docs/designs/architectures/20260822-academic-paper-search.md
**Error handling:** docs/designs/error-handlings/20260822-academic-paper-search.md
**Testing:** docs/designs/testings/20260822-academic-paper-search.md
---

## Reference implementation constraints

Use `openags/paper-search-mcp` for endpoint and field-mapping knowledge only. Do not copy behaviors that conflict with this gateway:

- Do not use its single-key `DOI else title+authors else paper_id` dedupe; implement transitive multi-index clustering.
- Do not copy provider-local retry/sleep loops; all adapters use the shared HTTP executor.
- Do not fall back from rejected Semantic Scholar authentication to unauthenticated requests.
- Do not copy CORE authentication fallback or local retries.
- Do not add dblp HTML scraping fallback in v1.
- Do not synthesize Crossref `1970-01-01` dates; missing dates remain `None`.
- Do not expose Unpaywall as keyword discovery; it is a DOI resolver after deduplication.
- Do not add runtime dependencies; parse arXiv/dblp XML with the standard library.

Use a separate `[academic_providers]` group with per-provider `enabled`, `max_concurrency`, `api_url`, optional `api_key_env`, and optional `contact_email_env` according to registration policy. Use a separate `[oa_resolvers.unpaywall]` group. Configuration examples that need environment-variable values must use placeholders such as `[REDACTED_SECRET]`; omitting an optional `*_env` field means unauthenticated/no-contact operation, while explicitly naming an environment variable that resolves empty is `CONFIG_ERROR`. Built-in CORE is optional-authentication; the generic resolver still supports required-authentication registrations for validation tests.

### Task 1: Academic domain contracts and pure identifier normalization

**Files:**
- Modify: `src/agent_search_gateway/models.py:16-97`
- Modify: `src/agent_search_gateway/providers/contracts.py:13-64`
- Create: `src/agent_search_gateway/academic/__init__.py`
- Create: `src/agent_search_gateway/academic/normalization.py`
- Create: `tests/academic/test_identifier_normalization.py`

- [ ] **Step 1: Write failing normalization/contract tests**
  - Import immutable `PaperIdentifiers`, `PaperRecord`, `OAResolution`, `PaperSearchHit`, `AcademicSearchProvider`, and `OAResolver`.
  - Add table-driven DOI tests for bare DOI, `doi:` prefix, DOI URL, case/whitespace normalization, and invalid identifiers.
  - Add arXiv tests for bare ID, `arXiv:`/abs-URL forms, and version stripping (`2401.12345v2 -> 2401.12345`); reject unsupported legacy forms rather than partially normalizing them.
  - Add canonicalization tests for Semantic Scholar IDs, OpenAlex `W...`/full URL, dblp record key/canonical record URL, and CORE work ID; assert provider-native keys are source-namespaced.
  - Add bibliographic-fingerprint tests: NFKC/casefold/whitespace-normalized exact title, intersecting normalized author evidence, and both publication years present/equal. Similar titles, incompatible authors, incompatible years, or missing year evidence do not weak-match.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/academic/test_identifier_normalization.py -v`
  - Expected: imports/functions are missing, then normalization assertions fail until the contract exists.

- [ ] **Step 3: Implement minimal domain values and pure normalizers**
  - Add `PaperIdentifiers`, `PaperRecord`, and `OAResolution` to `models.py` exactly as designed, using `date`, `NormalizedURL`, tuples, and `Mapping[str, int]`; no arbitrary provider `extra` map.
  - Add `PaperSearchHit`, `AcademicSearchProvider`, and `OAResolver` to `providers/contracts.py` without altering existing web protocols.
  - Implement pure DOI/arXiv/source-ID/title/author/topic/year/fingerprint helpers in `academic/normalization.py`.
  - Pseudocode: `normalize -> validate exact syntax -> return canonical scalar or None`; no network, fuzzy matching, logging, or merge policy.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run: `uv run pytest tests/academic/test_identifier_normalization.py -v`
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Consolidate only truly shared Unicode/whitespace helpers; keep provider-specific source-ID rules explicit. Rerun the targeted file and full suite.

### Task 2: Multi-index paper identity clustering and conflict rejection

**Files:**
- Create: `src/agent_search_gateway/academic/aggregator.py`
- Create: `tests/academic/test_paper_aggregator_identity.py`
- Reuse: `src/agent_search_gateway/academic/normalization.py`

- [ ] **Step 1: Write failing identity tests**
  - Direct same DOI, same arXiv ID, and same provider-native `(source, source_id)` merge cases.
  - Required transitive bridge: A has DOI, B has arXiv ID, C has both; multiple input permutations produce one logical record.
  - Same `(source, source_id)` with conflicting explicit DOI rejects the incoming candidate as `identifier_conflict` and keeps the established cluster.
  - Two different explicit DOIs never merge through bibliographic fallback.
  - Weak identity only when no strong IDs exist: exact normalized title + compatible authors + same known year merges; missing/incompatible year, incompatible authors, or merely similar title stays separate.
  - Stable cluster order follows configured provider priority and provider discovery order, not async completion or hash ordering.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/academic/test_paper_aggregator_identity.py -v`
  - Expected: `PaperAggregator` is missing or fails bridge/conflict/permutation cases.

- [ ] **Step 3: Implement multi-index clustering**
  - `PaperAggregator` receives explicit provider priority.
  - Normalize every hit at the aggregation boundary; reject missing title/source/source_id, invalid required landing URL, invalid strong identifiers/dates, or strong-ID contradictions with fixed reason codes.
  - Maintain indexes for canonical DOI, canonical arXiv ID, provider-native `(source, source_id)`, and strict weak fingerprint.
  - Pseudocode: collect all clusters referenced by a candidate's strong keys; reject if combining them would contradict strong IDs; otherwise union all referenced clusters transitively, attach the candidate, and re-index every strong key. Consult weak fingerprint only when no strong identity exists.
  - Preserve an origin rank for result ordering; never make task completion order part of identity.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run: `uv run pytest tests/academic/test_paper_aggregator_identity.py -v`
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Extract small cluster/index/compatibility helpers; no test-only production APIs. Rerun targeted + full tests.

### Task 3: Deterministic paper merge and provenance policy

**Files:**
- Modify: `src/agent_search_gateway/academic/aggregator.py`
- Create: `tests/academic/test_paper_aggregator_merge.py`

- [ ] **Step 1: Write failing merge tests**
  - Build one logical paper from dblp/arXiv/OpenAlex/Crossref/Semantic Scholar/CORE with complementary fields; all input permutations produce equivalent final fields.
  - Assert compatible IDs union into `PaperIdentifiers` and no provider erases another ID.
  - Preserve citation provenance as a source-keyed map; never synthesize cross-source max/average.
  - Stable-union `sources` and `topics` independent of completion order.
  - Academic API abstract outranks `llm:*`; empty never replaces non-empty; academic/direct PDF outranks weaker LLM locator; invalid optional PDF is omitted.
  - Missing dates remain `None`; final URL/count/date invariants hold.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/academic/test_paper_aggregator_merge.py -v`

- [ ] **Step 3: Implement deterministic merge selection**
  - Centralize explicit precedence keys: configured academic source priority; non-LLM before `llm:*` for abstract/PDF; canonical `(source, source_id)` as deterministic same-priority tie-breaker.
  - Choose stable first acceptable title/authors/venue/landing URL/dates by precedence; never overwrite non-empty with empty.
  - Normalize and stable-union topics/sources; keep one citation value per source with deterministic within-source resolution (non-negative max is acceptable).
  - Construct `PaperIdentifiers` and deterministic citation mapping only after cluster resolution.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run: `uv run pytest tests/academic/test_paper_aggregator_merge.py -v`
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Keep precedence in the aggregator, never in adapters. Rerun both aggregator files + full suite.

### Task 4: Shared HTTP params/text support and status-aware failures

**Files:**
- Modify: `src/agent_search_gateway/providers/http.py:16-215`
- Modify: `tests/providers/test_http_executor.py`
- Modify if needed: `tests/support/http.py`

- [ ] **Step 1: Write failing transport regressions**
  - Existing `request_json` tests stay unchanged.
  - Add query `params` pass-through and prove parameter values do not enter endpoint logs.
  - Add `request_text` success/retry/timeout/status tests and prove text mode performs no JSON decoding.
  - Add internal `HttpStatusFailure(ExecutionFailure)` carrying `status_code` so Unpaywall can treat terminal 404 as a miss without parsing exception strings; no new public `ErrorCode`.
  - Put sentinel query/authentication text in requests and assert it is absent from logs.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/providers/test_http_executor.py -v`

- [ ] **Step 3: Implement one shared response path**
  - Extend `request_json(..., params=...)` compatibly and add `request_text(...)` with the same retry/status/logging semantics.
  - Factor retries/status handling into one private response-producing helper; JSON mode decodes and raises `ProtocolFailure` on invalid JSON; text mode returns response text.
  - Terminal status failures raise `HttpStatusFailure`; existing `ExecutionFailure` catches continue to work.
  - Log only the query-free base endpoint.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run: `uv run pytest tests/providers/test_http_executor.py -v`
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Remove duplicated retry/status branches without changing current JSON behavior.

### Task 5: Separate academic registry and configuration resolver

**Files:**
- Create: `src/agent_search_gateway/providers/academic/__init__.py`
- Create: `src/agent_search_gateway/providers/academic/registry.py`
- Modify: `src/agent_search_gateway/config.py:16-180,371-380`
- Create: `tests/unit/test_config_academic_providers.py`
- Extend: `tests/providers/test_registry.py`
- Regression: `tests/unit/test_config_web_providers.py`

- [ ] **Step 1: Write failing registry/config tests**
  - Synthetic registrations cover authentication/contact modes `none|optional|required` without real adapters.
  - No-auth configuration succeeds; optional authentication omitted succeeds; optional auth env configured + present resolves; explicitly named but missing env fails; required auth missing fails.
  - Optional contact omitted/present, required contact missing, unknown provider/option, malformed HTTP(S) API URL, invalid/non-positive concurrency, wrong scalar type, and reserved constructor-key cases.
  - `[oa_resolvers.unpaywall]` absent/disabled succeeds; enabled required-contact config succeeds only when its environment variable resolves.
  - Existing web provider config tests remain unchanged.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/unit/test_config_academic_providers.py tests/providers/test_registry.py -v`

- [ ] **Step 3: Implement additive academic config types**
  - Add `AcademicProviderRegistration` and `OAResolverRegistration` with factory, allowed options, authentication/contact requirements, and registration-order-preserving registries separate from web `ProviderRegistry`.
  - Add `ResolvedAcademicProviderConfig`, `ResolvedAcademicProviderGroup`, and optional `ResolvedOAResolverConfig`; wrap both resolved auth/contact values in `SecretValue` for redaction.
  - Shared academic keys: `enabled`, `max_concurrency`, `api_key_env`, `contact_email_env`; validate remaining keys against registration options.
  - Add empty/default academic + resolver fields to `ResolvedConfig` so existing direct construction stays compatible.
  - Extend `resolve_config` with keyword-only academic/resolver registries; omitted registries mean no academic providers, preserving existing web-only call sites until runtime wiring.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run: `uv run pytest tests/unit/test_config_academic_providers.py tests/unit/test_config_web_providers.py tests/providers/test_registry.py -v`
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Share generic scalar/URL/env-name validation only where it does not weaken web invariants.

### Task 6: arXiv discovery adapter

**Files:**
- Create: `src/agent_search_gateway/providers/academic/arxiv.py`
- Create: `tests/providers/academic/test_arxiv.py`
- Create: `tests/fixtures/providers/academic/arxiv/search.xml`
- Create: `tests/fixtures/providers/academic/arxiv/malformed.xml`

- [ ] **Step 1: Write failing arXiv fixture tests**
  - Assert GET export API params (`search_query=all:<query>`, max-result/sort options) and no authentication requirement.
  - Map title/authors/summary/published/updated/DOI/categories/versioned arXiv ID/landing/PDF.
  - Malformed one entry is isolated; malformed Atom/XML envelope raises `ProtocolFailure`; HTTP failure propagates shared-executor semantics.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/providers/academic/test_arxiv.py -v`

- [ ] **Step 3: Implement adapter**
  - Use `request_text` + `xml.etree.ElementTree`; emit `PaperSearchHit(source="arxiv", source_id=<canonical work id>, arxiv_id=<canonical work id>, ...)`.
  - No local retries/sleeps, download functions, or feedparser dependency.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run: `uv run pytest tests/providers/academic/test_arxiv.py -v`
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor XML helpers and rerun**

### Task 7: Semantic Scholar discovery adapter

**Files:**
- Create: `src/agent_search_gateway/providers/academic/semantic_scholar.py`
- Create: `tests/providers/academic/test_semantic_scholar.py`
- Create: `tests/fixtures/providers/academic/semantic_scholar/search.json`
- Create: `tests/fixtures/providers/academic/semantic_scholar/malformed.json`

- [ ] **Step 1: Write failing Semantic Scholar tests**
  - Assert Graph search params `query`, `limit`, and requested fields: title, abstract, citationCount, authors, url, publicationDate, externalIds, fieldsOfStudy, openAccessPdf.
  - No-auth request when optional authentication is absent; auth header only when configured.
  - Map `paperId`, DOI, authors/date/citations/fields-of-study/landing/PDF; missing abstract/OA is valid.
  - Invalid top-level `data` envelope -> `ProtocolFailure`; malformed item isolated; rejected authentication propagates failure with no unauthenticated fallback.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/providers/academic/test_semantic_scholar.py -v`

- [ ] **Step 3: Implement adapter**
  - One shared-executor call per global retry attempt; reveal optional `SecretValue` only at request-header construction.
  - Keep identity/merge/precedence out of the adapter.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run targeted test, then `uv run pytest -q`.

- [ ] **Step 5: Refactor mapping helpers and rerun**

### Task 8: OpenAlex discovery adapter

**Files:**
- Create: `src/agent_search_gateway/providers/academic/openalex.py`
- Create: `tests/providers/academic/test_openalex.py`
- Create: `tests/fixtures/providers/academic/openalex/search.json`
- Create: `tests/fixtures/providers/academic/openalex/malformed.json`

- [ ] **Step 1: Write failing OpenAlex tests**
  - Assert GET `/works` with `search`/`per_page`; optional contact identity uses the documented polite request identity and is not logged.
  - Map work ID, DOI URL, authorships, primary landing/PDF, OA metadata, cited-by count, concepts/topics, publication date.
  - Pure abstract inverted-index reconstruction covers out-of-order positions and empty index.
  - Malformed item isolated; malformed `results` envelope -> `ProtocolFailure`.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/providers/academic/test_openalex.py -v`

- [ ] **Step 3: Implement adapter**
  - Deterministically reconstruct abstract by sorted positions.
  - Prefer primary location; use canonical OpenAlex work URL as required landing fallback.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run targeted test, then `uv run pytest -q`.

- [ ] **Step 5: Refactor and rerun**

### Task 9: dblp discovery adapter

**Files:**
- Create: `src/agent_search_gateway/providers/academic/dblp.py`
- Create: `tests/providers/academic/test_dblp.py`
- Create: `tests/fixtures/providers/academic/dblp/search.xml`
- Create: `tests/fixtures/providers/academic/dblp/malformed.xml`

- [ ] **Step 1: Write failing dblp tests**
  - Assert GET publication API with `q`, `format=xml`, `h`.
  - Map `info@key` as stable source ID, title/authors/venue/year/record URL/DOI from electronic-edition data; missing abstract is valid.
  - Malformed hit isolated; malformed XML -> `ProtocolFailure`; assert no HTML fallback request occurs after failure.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/providers/academic/test_dblp.py -v`

- [ ] **Step 3: Implement adapter**
  - Use `request_text` + standard XML parser; use `info@key` first and canonical record URL only as validated fallback source ID.
  - No process-random title hash, local retry, or HTML scraping.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run targeted test, then `uv run pytest -q`.

- [ ] **Step 5: Refactor and rerun**

### Task 10: Crossref discovery adapter

**Files:**
- Create: `src/agent_search_gateway/providers/academic/crossref.py`
- Create: `tests/providers/academic/test_crossref.py`
- Create: `tests/fixtures/providers/academic/crossref/search.json`
- Create: `tests/fixtures/providers/academic/crossref/malformed.json`

- [ ] **Step 1: Write failing Crossref tests**
  - Assert GET `/works` query/row/relevance params and optional contact `mailto` only when configured.
  - Map DOI source ID, title/authors/abstract/container venue/date fallback/citation count/item URL or DOI landing fallback/PDF link when explicit.
  - Missing abstract/date is valid; missing date is `None`, never epoch.
  - Malformed `message.items` -> `ProtocolFailure`; malformed item isolated.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/providers/academic/test_crossref.py -v`

- [ ] **Step 3: Implement adapter**
  - Reveal optional contact only into request params and never logs.
  - No Crossref-specific retry/sleep loop.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run targeted test, then `uv run pytest -q`.

- [ ] **Step 5: Refactor date/PDF helpers and rerun**

### Task 11: CORE discovery adapter

**Files:**
- Create: `src/agent_search_gateway/providers/academic/core.py`
- Create: `tests/providers/academic/test_core.py`
- Create: `tests/fixtures/providers/academic/core/search.json`
- Create: `tests/fixtures/providers/academic/core/malformed.json`

- [ ] **Step 1: Write failing CORE tests**
  - Assert GET `/v3/search/works` with `q`, `limit`, `offset=0`.
  - Optional bearer authentication only when configured; no-auth built-in path remains valid.
  - Map CORE ID/title/authors/abstract/DOI/date/landing/PDF from download/full-text URLs/repository-subject-tag metadata/citation count.
  - Malformed record isolated; malformed `results` -> `ProtocolFailure`; call-count proves no local retry or auth fallback.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/providers/academic/test_core.py -v`

- [ ] **Step 3: Implement adapter**
  - Reveal optional authentication value only at request-header construction.
  - Keep PDF as metadata only; no download/read methods; no local retries or authentication fallback.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run targeted test, then `uv run pytest -q`.

- [ ] **Step 5: Refactor and rerun**

### Task 12: Unpaywall resolver and resilient OA enrichment

**Files:**
- Create: `src/agent_search_gateway/providers/academic/unpaywall.py`
- Create: `src/agent_search_gateway/academic/enrichment.py`
- Create: `tests/providers/academic/test_unpaywall.py`
- Create: `tests/academic/test_oa_enrichment.py`
- Create: `tests/fixtures/providers/academic/unpaywall/oa.json`
- Create: `tests/fixtures/providers/academic/unpaywall/non_oa.json`
- Create: `tests/fixtures/providers/academic/unpaywall/malformed.json`

- [ ] **Step 1: Write failing resolver/enrichment tests**
  - Best PDF URL, landing-only best location, deterministic alternate location fallback, non-OA record, 404 normal miss, and timeout/terminal 429/5xx/malformed JSON surfaced as typed failures.
  - Required contact value is sent as request param but never logged.
  - Resolver failure logs `paper_enrichment_failed` and retains original record; success fills missing/weaker OA fields and never replaces stronger direct PDF.
  - Six hits -> one DOI paper -> exactly one resolver call; DOI-less paper -> zero calls.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/providers/academic/test_unpaywall.py tests/academic/test_oa_enrichment.py -v`

- [ ] **Step 3: Implement resolver + enrichment**
  - Resolve `/v2/<canonical-doi>` with contact param using shared executor.
  - Catch only `HttpStatusFailure(status_code=404)` as miss; other transport/status/protocol failures propagate to enrichment.
  - Validate response into `OAResolution`; deterministic best/alternate location selection.
  - `enrich_paper_records` runs after aggregation, caches per DOI, overlays only missing/weaker fields, catches/logs resolver failures, and never drops the paper.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run targeted tests, then `uv run pytest -q`.

- [ ] **Step 5: Refactor and rerun**

### Task 13: Register six discovery providers and Unpaywall with explicit policies

**Files:**
- Create: `src/agent_search_gateway/providers/academic/defaults.py`
- Create: `tests/providers/academic/test_registry.py`
- Reuse: `src/agent_search_gateway/providers/academic/registry.py`

- [ ] **Step 1: Write failing built-in registration tests**
  - Discovery order exactly `arxiv`, `semantic_scholar`, `openalex`, `dblp`, `crossref`, `core`.
  - Authentication modes: arXiv none, Semantic Scholar optional, OpenAlex none, dblp none, Crossref none, CORE optional.
  - Optional contact identity for OpenAlex/Crossref; required contact for separately registered Unpaywall resolver.
  - Only intended options (v1: `api_url`) are accepted; resolver never appears in discovery iteration.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/providers/academic/test_registry.py -v`

- [ ] **Step 3: Implement default builders**
  - Add separate default discovery and OA-resolver builders with concrete factories and requirement metadata.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run: `uv run pytest tests/providers/academic/test_registry.py tests/unit/test_config_academic_providers.py -v`
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**

### Task 14: Academic quota namespace

**Files:**
- Modify: `src/agent_search_gateway/concurrency.py:125-180`
- Modify: `tests/runtime/test_quota_manager.py`

- [ ] **Step 1: Write failing quota tests**
  - Existing web/LLM-only constructor use still works unchanged.
  - `academic_limits` plus `get_academic` enforce per-provider limits with controlled tasks/events.
  - The same provider-name string in web, LLM, and academic namespaces still resolves to independent gates.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/runtime/test_quota_manager.py -v`

- [ ] **Step 3: Implement the additive namespace**
  - Add optional empty academic-limit mapping and create `CapacityGate(..., quota_kind="academic")` entries.
  - Keep `get_web`, `get_llm`, and `wait_until_any_web_available` behavior unchanged.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run: `uv run pytest tests/runtime/test_quota_manager.py -v`
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Share quota-namespace construction only if it simplifies the class without changing existing gate semantics.

### Task 15: Paper/mixed result serialization and paper result filenames

**Files:**
- Modify: `src/agent_search_gateway/result_writer.py:12-50`
- Modify: `src/agent_search_gateway/request_ids.py:11-45,92-96`
- Modify: `tests/unit/test_result_writer.py`
- Modify: `tests/unit/test_request_ids.py`

- [ ] **Step 1: Write failing writer/result-kind tests**
  - Preserve exact current compact web-only bytes and assert no `type` discriminator is added.
  - Add paper-only compact JSON with title, authors array, valid empty abstract, complete nested identifiers, ISO dates/null, landing/PDF URLs, venue/topics, citation-count map, OA fields, and sources in deterministic order.
  - Reject final paper invariants before destination creation: empty title, invalid/non-normalized landing or PDF URL, negative/non-integer citation count, duplicate sources, invalid date/mapping shape.
  - Add mixed serialization where only the sink adds `type:"web"` / `type:"paper"`; do not mutate/replace domain objects upstream.
  - Extend result-kind/collision tests for `paper-<request_id>.jsonl`; keep keyword/llm filenames unchanged.
  - Preserve serialize-before-create and partial-target cleanup behavior.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/unit/test_result_writer.py tests/unit/test_request_ids.py -v`

- [ ] **Step 3: Implement additive serializers/writers**
  - Keep the current web serializer and web `write_results` semantics unchanged.
  - Add focused paper serializer and paper/mixed write methods; serialize all records before target creation.
  - Extend `ResultKind` with `paper`; LLM paper-only and mixed results still use the existing `llm` filename kind.
  - Share only atomic file-write mechanics; keep schemas explicit.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run: `uv run pytest tests/unit/test_result_writer.py tests/unit/test_request_ids.py -v`
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Deduplicate filesystem mechanics without introducing an untyped universal record serializer.

### Task 16: Direct `PaperSearchOrchestrator`

**Files:**
- Create: `src/agent_search_gateway/orchestrators/paper.py`
- Create: `tests/orchestrators/test_paper_search_pipeline.py`
- Modify: `tests/support/fakes.py`
- Reuse: `academic/aggregator.py`, `academic/enrichment.py`, `ProviderQuotaManager`, `URLStore`, `ResultWriter`

- [ ] **Step 1: Write failing direct paper-search tests**
  - Whitespace query -> `EMPTY_QUERY`; no enabled academic discovery providers -> new `NO_ACADEMIC_SEARCH_PROVIDERS`.
  - Provider A failure + provider B valid hits -> success; failure + completed empty provider -> successful empty file; all providers fail -> `ALL_PROVIDERS_FAILED` and no file.
  - Controlled providers prove concurrent scheduling, `get_academic(provider.name)` acquisition, and output flattening in configured provider order rather than task-completion order.
  - Complementary duplicate hits produce one merged `PaperRecord`; deduplicated DOI triggers one OA resolve.
  - Landing URL admission uses non-empty abstract or title fallback; `pdf_url` is not admitted.
  - Provider/candidate/cluster/enrichment log assertions use safe metadata/reason codes and contain no raw query/title/abstract/DOI payload.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/orchestrators/test_paper_search_pipeline.py -v`

- [ ] **Step 3: Implement direct orchestration and shared finalization**
  - Add only `ErrorCode.NO_ACADEMIC_SEARCH_PROVIDERS`; reuse current `ExecutionFailure`/`ProtocolFailure` hierarchy.
  - `paper_search(query, request_id)` validates input/provider presence, gathers independent provider pipelines with `return_exceptions=True`, treats `[]` as completed, and raises `ALL_PROVIDERS_FAILED` only if every provider pipeline failed.
  - Each provider pipeline logs started/completed/failed, acquires the academic quota, checks that the adapter returned a list, and does not log raw search/paper content.
  - Flatten completed provider lists in configured provider order, then call `PaperAggregator`, `enrich_paper_records`, paper landing-URL admission, and the paper writer.
  - Expose one internal finalization function in `orchestrators/paper.py` for aggregate -> enrich -> admit so the LLM paper branch reuses the exact same component/policy path.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run: `uv run pytest tests/orchestrators/test_paper_search_pipeline.py -v`
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Keep provider execution separate from finalization and rerun targeted + full tests.

### Task 17: Direct paper request protocol, CLI, and daemon dispatch

**Files:**
- Modify: `src/agent_search_gateway/models.py:52-89`
- Modify: `src/agent_search_gateway/protocol.py:39-166`
- Modify: `src/agent_search_gateway/cli.py:46-83`
- Modify: `src/agent_search_gateway/daemon.py:15-54,73-80,265-363`
- Modify: `tests/unit/test_protocol_codec.py`
- Modify: `tests/cli/test_cli.py`
- Modify: `tests/daemon/test_daemon_dispatch.py`
- Modify: `tests/daemon/test_daemon_request_ids.py`

- [ ] **Step 1: Write failing request/dispatch tests**
  - Encode/decode exact `{"type":"paper_search","query":"..."}` to a new `PaperSearchRequest` and reject extra/missing fields.
  - CLI `paper-search <query>` builds that request; whitespace-only query fails locally as `EMPTY_QUERY` without contacting the daemon.
  - Daemon reports command `paper-search`, reserves a result-capable request ID, calls `runtime.paper_search_orchestrator.paper_search`, and returns one `SuccessResponse(text=<absolute path>)`.
  - Paper requests participate in existing active-workflow/shutdown tracking and request-ID collision handling.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/unit/test_protocol_codec.py tests/cli/test_cli.py tests/daemon/test_daemon_dispatch.py tests/daemon/test_daemon_request_ids.py -v`

- [ ] **Step 3: Implement additive request plumbing**
  - Add `PaperSearchRequest` to the `Request` union.
  - Add strict protocol parser/encoder entry for `paper_search`.
  - Add CLI subcommand and daemon `RuntimeLike` paper-orchestrator property/dispatch branch.
  - Include `PaperSearchRequest` in `may_write_search_result=True`; keep response envelope and all current request schemas unchanged.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run the targeted command above.
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Simplify request unions/type aliases without broad protocol refactoring.

### Task 18: Strict LLM paper prompt, parser, and stage

**Files:**
- Modify: `src/agent_search_gateway/llm/prompts.py:60-72`
- Modify: `src/agent_search_gateway/llm/stages.py:96-105`
- Create: `src/agent_search_gateway/paper_search_parser.py`
- Create: `tests/unit/test_paper_search_parser.py`
- Modify: `tests/unit/test_llm_stages.py`

- [ ] **Step 1: Write failing paper-grammar tests**
  - Define one strict repeated grammar; every field line appears exactly once, optional values may be empty: `## Paper`, `Title`, `Authors` (semicolon-separated), `Abstract`, `DOI`, `arXiv`, `Published`, `Updated`, `URL`, `PDF`, `Venue`, `Topics` (semicolon-separated), `Citations`, `Open Access`, `OA Status`, `License`.
  - `Title` and valid HTTP(S) `URL` are required; dates are empty or `YYYY-MM-DD`; citation is empty or non-negative integer; OA value is `true|false|unknown`.
  - Parser receives LLM provider name and emits `PaperSearchHit(source="llm:<provider>", source_id=<normalized landing URL>, ...)`; DOI/arXiv canonicalization remains at aggregation boundary.
  - Cover one/repeated blocks, optional empties, multiple authors, invalid date/count/URL, duplicate/missing fields, arbitrary Markdown, web `## Result`, and mixed paper/web response; malformed grammar raises `ParserFailure` with no partial reinterpretation.
  - Place sentinels in prompt/model/malformed output and assert errors/logs do not expose them.
  - New paper prompt/stage uses the grammar; current web prompt/stage remains behaviorally unchanged.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/unit/test_paper_search_parser.py tests/unit/test_llm_stages.py -v`

- [ ] **Step 3: Implement prompt/parser/stage**
  - Add paper-specific system messages without editing the current web grammar.
  - Parse structurally/strictly; validate date/count/bool/URLs but do not fuzzy-correct identifier text.
  - Add `LLMStages.llm_paper_search_markdown` beside `llm_search_markdown`, reusing the same configured LLM client/quota path.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run targeted tests.
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Do not merge web and paper grammars into one permissive parser.

### Task 19: Scoped LLM search (`web|paper|all`)

**Files:**
- Modify: `src/agent_search_gateway/models.py` (`LLMSearchRequest`)
- Modify: `src/agent_search_gateway/orchestrators/search.py:31-200`
- Modify: `src/agent_search_gateway/protocol.py:47-52,141-148`
- Modify: `src/agent_search_gateway/cli.py:57-59,74-78`
- Modify: `tests/orchestrators/test_llm_search.py`
- Modify: `tests/unit/test_protocol_codec.py`
- Modify: `tests/cli/test_cli.py`

- [ ] **Step 1: Write failing scope/backward-compatibility tests**
  - `LLMSearchRequest(prompt)` and the legacy two-field protocol payload decode as `scope="web"`; explicit `web|paper|all` scopes round-trip; unknown scope/extra fields are rejected before daemon work.
  - Web scope preserves the exact current web system prompt, parser, provider invocation count, URLStore admission, `llm-<request_id>.jsonl` filename, and exact `{"url","abstract"}` line schema with no `type` field.
  - Paper scope isolates each LLM invocation: provider or strict-parser failure affects only that invocation, completed empty output counts as success, all invocation failures -> `ALL_PROVIDERS_FAILED`, and completed hits are aggregated/enriched/admitted through the same paper finalization path as direct paper search.
  - For `all`, use controlled events to prove the web and paper semantic branches are scheduled concurrently. Cover success/success, success/failure, failure/success, failure/failure, and successful-empty branch behavior.
  - Mixed output order is deterministic: web records first in existing order, then paper records in aggregate order; only the mixed sink adds discriminators.
  - Configure low concurrency for one LLM provider and prove both semantic branches share the existing LLM quota rather than creating a new branch-specific quota.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/orchestrators/test_llm_search.py tests/unit/test_protocol_codec.py tests/cli/test_cli.py -v`

- [ ] **Step 3: Implement scoped branching without rewriting web semantics**
  - Add `scope: Literal["web", "paper", "all"] = "web"` to `LLMSearchRequest`.
  - Protocol decoding accepts exactly the legacy `{type,prompt}` shape or the new `{type,prompt,scope}` shape; CLI adds `--scope` with argparse choices and default `web`.
  - Extract the existing LLM web collection into a helper without changing its prompt/parser/body; add paper collection using `llm_paper_search_markdown` + strict paper parser.
  - Add new paper-search dependencies to `SearchOrchestrator` as additive optional constructor inputs during this task so existing runtime assembly remains valid and web-only scope stays unchanged until Task 20 wires the real academic components. `scope=paper|all` must fail fast in tests if its required paper finalization dependency is absent; `scope=web` must never touch it.
  - Flatten successful paper-invocation lists in configured invocation order, then reuse the paper finalization function from `orchestrators/paper.py` for aggregate -> optional OA enrichment -> landing-URL admission.
  - For `all`, create/schedule both branch coroutines before awaiting; retain successful lists including empty lists, log fixed `llm_search_branch_failed scope=...` metadata for failed branches, fail only when both branches fail, and write one mixed `llm-...jsonl` file.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run: `uv run pytest tests/orchestrators/test_llm_search.py tests/unit/test_protocol_codec.py tests/cli/test_cli.py -v`
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Keep the existing web path as the default and avoid replacing typed web/paper records with one permissive universal record type. Rerun targeted + full tests.

### Task 20: Runtime assembly, daemon/doctor configuration, secrets, and lifecycle

**Files:**
- Modify: `src/agent_search_gateway/runtime.py:30-224`
- Modify: `src/agent_search_gateway/daemon.py:209-231`
- Modify: `src/agent_search_gateway/doctor.py` around `resolve_config`
- Modify: `config.example.toml`
- Modify: `tests/runtime/test_runtime_assembly.py`
- Create: `tests/runtime/test_academic_runtime_assembly.py`
- Modify: `tests/docs/test_documented_config.py`

- [ ] **Step 1: Write failing runtime/config assembly tests**
  - Build resolved config with mock HTTP clients and assert only enabled academic discovery providers instantiate, in configured order, with configured academic quota limits.
  - Assert optional provider authentication/contact values are passed only to providers whose registration allows them, without changing existing web-provider constructor contracts.
  - Resolver absent -> `None`; explicitly enabled resolver -> exactly one Unpaywall resolver.
  - Assert direct paper search and LLM paper finalization share one `PaperAggregator` policy/component, the same optional resolver, the same `URLStore`, and the same result directory/writer context rather than duplicate paper pipelines.
  - Assert every academic HTTP executor closes in `Runtime.aclose` while existing web executor and LLM-client close behavior remains unchanged.
  - Reserved academic constructor options or adapter factory `TypeError` become startup `ConfigFailure(CONFIG_ERROR)`.
  - Doctor/documented-config tests remain no-network and parse the new academic/resolver groups.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/runtime/test_academic_runtime_assembly.py tests/runtime/test_runtime_assembly.py tests/docs/test_documented_config.py -v`

- [ ] **Step 3: Wire the runtime and config consumers**
  - Extend `Runtime` with academic discovery providers, optional OA resolver, `PaperSearchOrchestrator`, and owned academic HTTP executors while retaining all current public runtime fields.
  - Build default/supplied academic and OA registries, academic `HttpJsonExecutor` instances, provider factory kwargs from resolved auth/contact/options only, and academic quota limits.
  - Instantiate one `PaperAggregator`; pass it, the optional resolver, shared `URLStore`, and `ResultWriter` context to the direct and LLM paper paths.
  - Update daemon and doctor to build/pass the default academic + resolver registries to `resolve_config`.
  - Add resolved academic authentication/contact `SecretValue`s to the existing debug redaction session before requests can log; never expose values in repr/log fields.
  - Extend `config.example.toml` with the plan's separate academic/resolver shape using redacted placeholders; keep Unpaywall disabled by default so the example remains valid without optional feature configuration.
  - Close each owned academic executor exactly once.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run the targeted runtime/docs tests above.
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Extract academic assembly helpers only as needed; do not fold academic registration into `WebProviderRegistration` or change existing web/LLM assembly semantics.

### Task 21: Academic observability and redaction regression coverage

**Files:**
- Modify: `tests/unit/test_observability_logging.py`
- Modify: `tests/support/logging.py` only if additive helpers are needed
- Modify as needed: `src/agent_search_gateway/orchestrators/paper.py`
- Modify as needed: `src/agent_search_gateway/academic/aggregator.py`
- Modify as needed: `src/agent_search_gateway/academic/enrichment.py`

- [ ] **Step 1: Write failing structured-log tests**
  - Assert representative fixed events/fields: `provider_started provider=openalex stage=paper_search`, `provider_completed ... results=<n>`, `paper_candidate_rejected reason=missing_title`, `paper_clusters_merged`, `paper_enrichment_failed resolver=unpaywall stage=oa_resolve`, `llm_search_branch_failed scope=paper`, and `results_written kind=paper|llm`.
  - Place distinct sentinels in user paper query, LLM prompt, malformed LLM output, paper title, paper abstract, DOI, provider authentication value, and contact value; assert no sentinel appears in rendered logs.
  - Assert candidate rejection reason codes include at least `missing_title`, `missing_source_id`, `invalid_landing_url`, `invalid_identifier`, `invalid_date`, `invalid_record_shape`, and `identifier_conflict` where the corresponding boundary fails.
  - Prefer event/reason field assertions over exact timing/full-line matching.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/unit/test_observability_logging.py tests/orchestrators/test_paper_search_pipeline.py tests/orchestrators/test_llm_search.py -v`

- [ ] **Step 3: Add only missing safe instrumentation**
  - Log operational metadata only: provider/resolver, stage, counts, elapsed time, fixed reason, error type, and branch scope.
  - Never log raw query/prompt/output, paper title/abstract, raw identifiers, authentication/contact values, or request query parameters.
  - Keep HTTP endpoint logging query-free and route all resolved sensitive config through the existing redactor.

- [ ] **Step 4: Verify GREEN + full suite**
  - Run targeted observability tests.
  - Run: `uv run pytest -q`

- [ ] **Step 5: Refactor and rerun**
  - Remove duplicate event emission/helpers while preserving fixed event/reason vocabulary and redaction guarantees.

### Task 22: Acceptance workflows, documentation, and release gate

**Files:**
- Modify: `tests/acceptance/test_gateway_workflows.py`
- Modify: `tests/support/acceptance.py`
- Modify: `tests/support/fakes.py`
- Modify: `README.md`
- Modify: `config.example.toml` only for final documentation corrections
- Optional future-only: `tests/integration/test_live_academic.py` guarded by `ACADEMIC_SEARCH_RUN_INTEGRATION=1` (do not add unless explicitly useful; never enable by default)

- [ ] **Step 1: Write failing acceptance/regression tests**
  - Direct `paper-search`: start a controlled daemon runtime with fake academic providers, invoke the real CLI/socket path, assert stdout contains exactly one absolute result path, and assert the file contains the expected merged paper JSONL.
  - Assert paper-search stderr/business stdout is not polluted by debug/provider output; the final landing URL can immediately be used by existing `url-fetch` because it was admitted; the PDF URL is not automatically admitted.
  - `llm-search --scope all`: controlled LLM client returns one web result and one paper result; assert one `llm-<request_id>.jsonl` contains typed web then paper lines and one correlated request ID.
  - Default `llm-search <prompt>` acceptance output remains the exact existing web-only behavior.
  - Paper workflows participate in the existing active-request/graceful-shutdown behavior; default tests make no real academic-provider network calls.

- [ ] **Step 2: Run RED**
  - Run: `uv run pytest tests/acceptance/test_gateway_workflows.py -v`

- [ ] **Step 3: Complete acceptance support and README**
  - Extend acceptance fakes additively for `paper_search_orchestrator` and paper-capable LLM results.
  - Document `paper-search`, `llm-search --scope web|paper|all`, paper-only and mixed JSONL schemas, all six discovery providers, Unpaywall's resolver-only role, and academic authentication/contact configuration semantics.
  - Document v1 non-goals explicitly: no PDF-download/read workflow, no dblp HTML fallback, no fuzzy identity matching, and no Unpaywall discovery search.
  - Document optional live integration only if a guarded live test is actually added.

- [ ] **Step 4: Run the full release gate**
  - Run: `uv sync --locked`
  - Run: `uv run ruff check .`
  - Run: `uv run mypy src tests`
  - Run: `uv run pytest -v`
  - Expected: all legacy and feature tests pass; default test suite has no external academic network dependency; default LLM web output bytes and current web-provider config behavior remain unchanged.

- [ ] **Step 5: Final self-review against all three design specs**
  - Re-read the architecture, error-handling, and testing specs and verify every public contract, normalization/identity rule, partial-success rule, OA failure rule, output schema, logging rule, and lifecycle rule has both implementation coverage and a regression test.
  - Remove dead helpers, provider-local retry/fallback code, arbitrary provider `extra` state, hidden transport fallback, and test-only production hooks.
  - Re-run `uv run ruff check .`, `uv run mypy src tests`, and `uv run pytest -v` after cleanup.
