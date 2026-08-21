## Architecture: Academic Paper Search

### 1. Scope & Assumptions

#### In Scope

- Add a new `paper-search <query>` CLI/daemon workflow for structured academic paper search.
- Add six academic discovery providers: arXiv, Semantic Scholar, OpenAlex, dblp, Crossref, and CORE.
- Add Unpaywall as an optional DOI-to-open-access resolution component, not as a search provider.
- Add a first-class `PaperRecord` domain model alongside the existing `SearchRecord`.
- Add internal `PaperSearchHit` provider output and centralized normalization, identity resolution, deduplication, and deterministic merge.
- Extend `llm-search` with `--scope web|paper|all`, defaulting to `web` for backward compatibility.
- Use separate LLM prompts/parsers for web and paper search. `--scope all` executes both pipelines independently and concurrently rather than using a mixed prompt.
- Preserve one result-file path per successful CLI command. `llm-search --scope all` writes one mixed JSONL file with a type discriminator.
- Admit final paper landing-page URLs into the existing `URLStore` so existing `url-fetch` can use paper pages discovered by search.
- Reuse existing retry, quota, observability, secret-redaction, daemon, socket, and result-path infrastructure.

#### Todo

- Direct PDF downloading or PDF parsing as a new gateway workflow.
- Automatic admission of `pdf_url` into `URLStore`.
- Fuzzy title/entity matching, embeddings, or probabilistic scholarly entity resolution.
- Persistent paper state across daemon restarts.
- Citation graph traversal, reference expansion, related-paper search, author search, or venue-specific query DSLs.
- Runtime plugin loading for third-party academic providers or OA resolvers.
- Provider-specific advanced CLI filters such as year ranges, venues, fields of study, sort order, or per-source result limits.
- A generalized `SearchResult` inheritance hierarchy for future datasets, patents, repositories, or other entity types.

#### Assumptions

- LLM providers used by `llm-search` may expose their own search capability to the model. The gateway does not provide its academic providers as LLM tool calls.
- Academic API results are metadata records. A paper may legitimately have no abstract, no DOI, or no direct PDF URL.
- DOI and arXiv identifiers are stronger paper identity evidence than URL equality.
- False merges are more harmful than leaving a duplicate paper unmerged; weak matching is therefore conservative.
- Unpaywall enrichment is optional and must never be required for a successful paper search result.
- Existing web-search and URL-fetch semantics remain migration-stable unless explicitly extended below.

---

### 2. Architecture Summary

Academic search is added as a parallel domain beside the existing URL-centric web-search domain. Academic provider adapters return normalized-enough `PaperSearchHit` candidates but do not deduplicate, mutate stores, or decide final field precedence. A `PaperAggregator` centralizes identifier normalization, identity clustering, conservative weak matching, deterministic field merge, and optional Unpaywall OA enrichment to produce `PaperRecord` values. `paper-search` calls enabled academic providers concurrently and writes paper JSONL. `llm-search --scope paper` uses a dedicated academic-search prompt/parser and passes parsed `PaperSearchHit` values through the same aggregator; `--scope all` concurrently executes the existing web LLM pipeline and the paper LLM pipeline, then combines their already-typed results only at serialization time. Existing `KeywordSearchHit`, `SearchRecord`, `URLStore`, keyword-search behavior, and URL-fetch admission rules are not generalized into paper concepts.

---

### 3. Design Decisions

#### Runtime Model

##### Parallel Academic Domain Instead of Extending Web Search Objects

- Description: Add `PaperSearchHit`, `PaperRecord`, `AcademicSearchProvider`, `PaperAggregator`, and `PaperSearchOrchestrator` beside the existing web-search types and orchestrator.
- Rationale: Existing web search is intentionally URL-centric and exposes only URL/title/snippet/body candidates. Academic records require identifiers, authors, dates, venue, citations, OA metadata, and multi-source provenance. Extending `KeywordSearchHit` or `URLRecord` with scholarly fields would couple unrelated workflows and weaken their invariants.
- Trade-offs: The runtime contains a second search domain and some parallel orchestration code.
- Rejected Alternatives:
  - Add academic adapters as `KeywordSearchProvider`s:
    - Description: Map every paper to URL/title/snippet and reuse the existing pipeline.
    - Why Rejected: It discards scholarly metadata at the adapter boundary and makes correct cross-source identity resolution impossible.
  - Replace `SearchRecord` with one universal record:
    - Description: Create a large optional-field result object used by web and paper search.
    - Why Rejected: It would make existing web consumers understand paper-only fields and create an under-constrained core type.

##### Unpaywall as Optional OA Resolver

- Description: Model Unpaywall as a small `OAResolver` component that receives a normalized DOI and returns OA location metadata. It is not registered as an `AcademicSearchProvider`.
- Rationale: Unpaywall is DOI-centric resolution/enrichment, not generic discovery. Treating it as a search provider would create a false capability model and unnecessary one-result search semantics.
- Trade-offs: Academic runtime assembly has a separate optional resolver slot in addition to the provider list.
- Rejected Alternatives:
  - Expose Unpaywall as another search provider:
    - Description: Accept arbitrary search query text and attempt DOI extraction.
    - Why Rejected: Generic keyword searches do not map to Unpaywall's API contract.
  - Build a general plugin framework now:
    - Description: Dynamically load resolver implementations through Python entry points.
    - Why Rejected: The current requirement needs one internal optional component; runtime plugin infrastructure is unnecessary scope.

#### Interface / Protocol

##### New `paper-search` Command

- Description: Add `agent-search-gateway paper-search "query"`. It returns one absolute `paper-<request_id>.jsonl` path on success.
- Rationale: Structured paper metadata has a different public schema from web search and should have an explicit direct-search entry point.
- Trade-offs: Adds one CLI subcommand and one daemon request type.

##### Scoped LLM Search

- Description: Extend `llm-search` with `--scope web|paper|all`; omitted scope is equivalent to `web`.
- Rationale: Users can explicitly choose whether model-provider search should target general web results, scholarly results, or both. A value parameter is more extensible and clearer than accumulating boolean switches such as `--academic`.
- Trade-offs: The protocol request gains one optional field and `llm-search` gains branch-specific behavior.
- Rejected Alternatives:
  - `--academic` boolean:
    - Description: Add only a paper-mode switch.
    - Why Rejected: It does not express `all` cleanly and scales poorly if additional result domains are introduced later.
  - Always run both searches:
    - Description: Make every LLM search execute web and academic prompts.
    - Why Rejected: Doubles search-model calls for users who only want existing web behavior and changes current semantics.

##### `all` Is Two Pipelines, Not One Mixed Prompt

- Description: `llm-search --scope all` concurrently runs the existing web LLM search pipeline and a paper LLM search pipeline, each with its own strict prompt and parser.
- Rationale: Each parser keeps one simple grammar. Failure isolation is clearer, model adherence is easier to test, and web/paper components remain reusable independently.
- Trade-offs: `all` can issue two LLM search calls per configured LLM invocation.
- Rejected Alternatives:
  - One prompt returning both `## Result` and `## Paper` blocks:
    - Description: Ask one model response to contain mixed block types and parse both.
    - Why Rejected: It creates a third mixed grammar, increases parser ambiguity, and couples otherwise independent pipelines.

##### Backward-Compatible LLM Wire Shape

- Description: `LLMSearchRequest` gains `scope` with semantic default `web`. The decoder accepts legacy requests without `scope`; the encoder may omit `scope` when it is `web`.
- Rationale: Existing local protocol behavior is documented as a stable boundary. Default CLI invocation and legacy frames should retain current semantics.
- Trade-offs: Protocol decoding must support an optional request key rather than one exact shape.

#### State Management

##### Paper Identity Is Identifier-Centric

- Description: Paper aggregation uses canonical DOI, canonical arXiv ID, source-native IDs, and finally a conservative bibliographic fingerprint. URL is a locator, not the primary paper identity.
- Rationale: The same paper commonly appears under arXiv, DOI resolver, Semantic Scholar, OpenAlex, repository, and publisher URLs.
- Trade-offs: Identity resolution is more involved than URL-set deduplication.

##### Multi-Index Identity Clustering

- Description: The aggregator maintains temporary indexes for normalized DOI, arXiv ID, provider-native `(source, id)`, and strict bibliographic fingerprint. A candidate can connect previously separate clusters through multiple strong identifiers.
- Rationale: A single unique-key function cannot resolve transitive cases such as candidate A containing DOI only, B containing arXiv ID only, and C containing both.
- Trade-offs: Aggregation requires cluster merging rather than a simple `seen` set.
- Rejected Alternatives:
  - `DOI else title+authors else paper_id` key:
    - Description: Keep only the first available identity key per result.
    - Why Rejected: It fails cross-identifier transitive merges and is the principal weakness of the reference aggregator.

##### Conservative Weak Matching

- Description: Bibliographic fallback requires an exact normalized title plus compatible normalized author evidence and publication year. No fuzzy title threshold is used in the first version.
- Rationale: Incorrectly merging two distinct papers silently corrupts metadata. Duplicate retention is safer than false identity.
- Trade-offs: Some genuine duplicates with title punctuation/subtitle drift or incomplete authors remain separate.

##### Paper Landing URLs Reuse Existing URL Admission

- Description: After final paper aggregation, a valid `PaperRecord.url` is admitted to the existing `URLStore`. The admission abstract is `paper.abstract` when non-empty, otherwise `paper.title`. `pdf_url` is metadata only and is not automatically admitted.
- Rationale: Search-discovered paper landing pages should remain eligible for existing `url-fetch` without changing the five-field URL state machine. Some scholarly sources such as dblp may have no abstract, so title fallback preserves the store's non-empty-abstract invariant.
- Trade-offs: URLStore's internal abstract can be a title for metadata-only paper records, and direct PDF fetching remains out of scope.

#### Storage / Persistence

##### One Result Path Per Command

- Description: Successful `keyword-search`, `paper-search`, and `llm-search` continue to return exactly one result-file path.
- Rationale: This preserves CLI stdout and daemon `SuccessResponse(text=path)` behavior and avoids introducing manifest files or multi-path output conventions.
- Trade-offs: `llm-search --scope all` needs heterogeneous serialization in one JSONL file.

##### Homogeneous Single-Scope Files, Typed Mixed Files

- Description: Web-only LLM output keeps the exact existing `{"url","abstract"}` schema. Paper-only output contains only paper objects without a discriminator. `scope=all` adds `"type":"web"` or `"type":"paper"` to each line.
- Rationale: Default compatibility is preserved while mixed results remain unambiguously parseable.
- Trade-offs: Consumers of `scope=all` must branch on `type`.

#### Provider Integration

##### Six Discovery Adapters with One Internal Contract

- Description: arXiv, Semantic Scholar, OpenAlex, dblp, Crossref, and CORE implement `AcademicSearchProvider.search(query) -> list[PaperSearchHit]`.
- Rationale: Provider-specific request/response mapping stays at adapters while paper identity and final merge policy remain centralized.
- Trade-offs: The gateway maintains six API/XML mappings and provider fixtures.

##### Separate Academic Provider Registry and Config Group

- Description: Academic discovery providers use their own registration/configuration group rather than extending `WebProviderRegistration`. All discovery providers expose search only; resolver configuration is separate.
- Rationale: Academic credential/contact requirements differ substantially from current web providers, whose enabled stages assume API-key environment variables and search/fetch capabilities.
- Trade-offs: Adds an academic registry/config resolver alongside the existing web registry.

##### Provider-Specific Credential Requirements Without Shared Fake Requirements

- Description: Academic config supports no-key providers, optional API keys, API-key providers, and contact-email configuration as required by each adapter. Provider registrations declare allowed options and whether credentials/contact identity are optional or required.
- Rationale: arXiv/dblp do not require API keys; Semantic Scholar may use one; CORE may use one; Crossref/OpenAlex benefit from contact identity; Unpaywall resolution requires contact email. Forcing all through `api_key_env` would distort real API contracts.
- Trade-offs: Academic config validation is slightly richer than the current web config resolver.

##### Reuse Existing HTTP Execution Semantics

- Description: Retain the existing shared HTTP executor and extend it minimally with GET query parameters and text-response support for arXiv/dblp. Existing JSON callers continue using `request_json` unchanged.
- Rationale: Retry, timeout, logging, secret redaction, and failure classification already exist and should not be reimplemented in each academic adapter.
- Trade-offs: The current `HttpJsonExecutor` name becomes less exact if it gains `request_text`; a broader rename can be deferred to avoid a large unrelated diff.
- Rejected Alternatives:
  - Copy `requests`/`time.sleep` loops from the reference repository:
    - Description: Let each adapter own sessions, retries, and delays.
    - Why Rejected: It duplicates gateway infrastructure and weakens observability/error consistency.
  - Rename/refactor all current HTTP code immediately:
    - Description: Replace every existing web import with a new generalized transport type.
    - Why Rejected: Broad churn is unnecessary for the feature; behavior can be generalized compatibly first.

#### Concurrency / Scheduling

##### Concurrent Discovery with Shared Academic Quotas

- Description: `PaperSearchOrchestrator` calls all enabled academic providers concurrently. `ProviderQuotaManager` is extended with optional `academic_limits` and `get_academic`; existing constructor call sites remain valid through an empty default.
- Rationale: This reuses proven `CapacityGate` instrumentation without duplicating semaphore logic or changing web/LLM quotas.
- Trade-offs: `ProviderQuotaManager` gains a third quota namespace.

##### Enrich After Deduplication

- Description: Unpaywall is called only after candidates have been normalized and clustered. At most one OA resolution is attempted per final DOI-bearing paper requiring enrichment.
- Rationale: Six providers may return the same DOI; resolving before deduplication wastes network calls and rate limit.
- Trade-offs: OA metadata is unavailable during earlier field-selection decisions, so enrichment is a final overlay step.

##### LLM `all` Reuses Existing LLM Quotas

- Description: Web and paper LLM branches execute concurrently but acquire the same per-LLM-provider quota already used by the underlying client.
- Rationale: `scope=all` should not bypass provider concurrency limits just because it creates two semantic branches.
- Trade-offs: With low LLM concurrency, one branch may wait behind the other even though the orchestrator schedules both concurrently.

#### Security

##### Strict Boundary Validation

- Description: Normalize/validate DOI, arXiv IDs, source IDs, dates, citation counts, and URLs before they enter final paper records. Invalid candidate fields are rejected or omitted according to the field's requiredness.
- Rationale: Academic APIs and LLM search output are external/untrusted protocol data.
- Trade-offs: Some malformed-but-recoverable third-party records may be discarded.

##### No New Secret Logging

- Description: Academic API keys/contact config follow existing secret wrappers and redaction. Queries, paper abstracts, and LLM prompt/response bodies are not added to DEBUG logs.
- Rationale: Existing logging policy deliberately records operational metadata rather than user/search content.
- Trade-offs: Debugging provider-specific malformed records relies on reason codes and fixtures rather than raw response logging.

#### Observability

##### Reuse Existing Provider Event Vocabulary with Academic Stages

- Description: Academic provider calls emit existing-style `provider_started`, `provider_completed`, and `provider_failed` events with stages such as `paper_search` and `oa_resolve`. Candidate/merge decisions use fixed events/reasons such as `paper_candidate_rejected`, `paper_clusters_merged`, and `paper_enrichment_failed`.
- Rationale: Operators already understand the gateway's structured event style; fixed reason codes make silent record drops diagnosable without logging payloads.
- Trade-offs: Adds a modest number of new event/reason names that must be documented/tested.

#### Future Migration

##### Keep Public Web Contracts Unchanged

- Description: `SearchRecord`, `KeywordSearchHit`, `URLRecord`, keyword-search output, and default LLM web output remain unchanged. New paper contracts are additive.
- Rationale: A parallel additive domain minimizes migration risk and leaves a clean boundary for future implementation changes.
- Trade-offs: Some concepts, such as search orchestration and result writing, have web/paper-specific methods rather than one generalized abstraction.

---

### 4. Component Catalog

| Component | Purpose | Key Responsibilities | Public Interfaces | Dependencies | Owns State? | Data-Flow Role |
|---|---|---|---|---|---|---|
| `PaperSearchHit` | Carry one provider/LLM paper candidate | Hold source metadata before aggregation | Internal immutable value object | normalized scalar types | No | Source value |
| `PaperIdentifiers` | Represent canonical strong identifiers | Store DOI, arXiv, Semantic Scholar, OpenAlex, dblp, CORE IDs | `PaperIdentifiers(...)` | identifier normalizers | No | Domain value |
| `PaperRecord` | Represent final user-visible paper | Hold merged bibliographic, citation, OA, locator, and provenance data | Serialized in paper JSONL | `PaperIdentifiers`, normalized URLs | No | Output domain value |
| `AcademicSearchProvider` | Isolate academic discovery APIs | Execute one source query and map results to hits | `async search(query) -> list[PaperSearchHit]` | shared HTTP executor | No | Adapter contract |
| Academic Provider Registry | Assemble supported discovery adapters | Register factories, config options, credential/contact requirements | registration lookup/build | provider adapters | Registration data only | Registry |
| Academic Provider Adapters | Map six source APIs | Build requests, parse JSON/XML, reject malformed individual records | `search` | HTTP executor, parsers | No | Source adapter |
| Paper Normalizers | Canonicalize identity/metadata values | DOI/arXiv/source ID/title/author/date/URL normalization | pure functions | standard library | No | Validator/transformer |
| `PaperAggregator` | Produce stable unique papers | Cluster identities, merge fields deterministically, preserve provenance | `aggregate(hits) -> list[PaperRecord]` | normalizers, merge policy | Temporary per-call indexes/clusters | Transformer |
| `OAResolver` | Enrich DOI-bearing papers with OA location | Resolve DOI to OA landing/PDF/license/status | `resolve(doi) -> OAResolution | None` | shared HTTP executor | No | Optional enrichment adapter |
| Unpaywall Resolver | Implement OA resolution | Call Unpaywall and normalize OA response | `resolve` | HTTP executor, contact config | No | Optional enrichment adapter |
| `PaperSearchOrchestrator` | Coordinate direct paper search | Validate query, run providers concurrently, aggregate, enrich, admit landing URLs, write result | `paper_search(query, request_id)` | providers, quotas, aggregator, resolver, URLStore, ResultWriter | No | Coordinator |
| LLM Paper Prompt/Parser | Convert prompt-provider search into paper hits | Generate strict paper-search messages; parse restricted response blocks | prompt builder + parser | LLM client/stages | No | Transformer/boundary |
| Existing `SearchOrchestrator` | Coordinate web and scoped LLM search | Preserve keyword/web behavior; branch LLM scope and combine completed web/paper results | `llm_search(prompt, scope, request_id)` | existing web pipeline, paper aggregation path | No | Coordinator |
| Existing `ProviderQuotaManager` | Enforce provider concurrency | Add academic quota namespace while preserving web/LLM namespaces | `get_web`, `get_llm`, `get_academic` | `CapacityGate` | Yes, semaphores | Gate |
| Existing HTTP Executor | Standardize provider transport | Add params/text capability while retaining retry/logging behavior | `request_json`, `request_text` | httpx, retry policy | Client connection state | Transport boundary |
| Existing `URLStore` | Gate later URL fetches | Admit paper landing URLs using abstract-or-title | unchanged `admit` | URL normalization | Yes, URL state | Store |
| Existing `ResultWriter` | Persist one result path | Add paper and mixed serializers while preserving existing web serializer | web/paper/mixed write methods | filesystem | No | Sink |

Provider adapters and OA resolvers must not mutate `PaperAggregator`, `URLStore`, or result files directly. Aggregation and admission decisions belong to orchestrators/core components.

---

### 5. Data Flow

#### 5.1 Entry Point 1: `agent-search-gateway paper-search <query>`

```text
CLI Entrypoint:
  parse query
  if query is empty:
    fail locally/through request validation with EMPTY_QUERY
  send PaperSearchRequest(query) to daemon

Foreground Daemon:
  dispatch PaperSearchRequest to PaperSearchOrchestrator.paper_search

PaperSearchOrchestrator:
  if no academic discovery providers are enabled:
    raise NO_ACADEMIC_SEARCH_PROVIDERS

  concurrently for each enabled AcademicSearchProvider:
    acquire ProviderQuotaManager.get_academic(provider.name)
    try:
      hits = provider.search(normalized_query)
      mark provider pipeline completed, even when hits == []
    catch provider execution/protocol failure:
      log provider_failed
      preserve failure for aggregate completion rule

  if every provider pipeline failed:
    raise ALL_PROVIDERS_FAILED

  collect hits from completed pipelines in configured provider order

  PaperAggregator.aggregate(hits):
    normalize identifiers and metadata
    reject malformed/conflicting candidates with fixed reason codes
    cluster by strong identifiers; bridge clusters transitively
    use conservative bibliographic fallback only when strong IDs are absent
    merge fields using deterministic source/field policy
    produce PaperRecord[] in stable discovery order

  if Unpaywall resolver is configured:
    for each PaperRecord with normalized DOI needing OA/PDF enrichment:
      try:
        resolution = resolver.resolve(doi)
        if resolution exists:
          overlay OA fields according to merge policy
      catch resolver failure:
        log paper_enrichment_failed
        keep original PaperRecord

  for each final PaperRecord:
    normalize record.url
    URLStore.admit(record.url, record.abstract or record.title)
    do not admit record.pdf_url

  ResultWriter.write_paper_results("paper", records, request_id)
  return one absolute result path

CLI Entrypoint:
  print only the returned path
```

#### 5.2 Entry Point 2: `agent-search-gateway llm-search <prompt> --scope web`

```text
CLI Entrypoint:
  parse scope; omitted scope => web
  send LLMSearchRequest(prompt, scope=web)

SearchOrchestrator:
  execute the existing LLM web-search pipeline unchanged
  parse only `## Result` blocks
  admit URLs to URLStore using existing logic
  write existing llm JSONL schema: {url, abstract}
  return one path
```

#### 5.3 Entry Point 3: `agent-search-gateway llm-search <prompt> --scope paper`

```text
SearchOrchestrator / paper LLM branch:
  if no configured LLM search invocations:
    raise NO_LLM_SEARCH_PROVIDERS

  concurrently for each configured LLM invocation:
    call LLM provider with llm_paper_search_messages(prompt)
    parse only restricted `## Paper` blocks into PaperSearchHit[]
    if provider call or parser fails:
      mark that invocation failed

  if every invocation failed:
    raise ALL_PROVIDERS_FAILED

  aggregate hits from completed invocations through the same PaperAggregator
  optionally enrich DOI-bearing records through configured OAResolver
  admit each paper landing URL to URLStore using abstract-or-title
  write paper-only `llm-<request_id>.jsonl`
  return one path
```

#### 5.4 Entry Point 4: `agent-search-gateway llm-search <prompt> --scope all`

```text
SearchOrchestrator:
  schedule web_branch(prompt) and paper_branch(prompt) concurrently

  web_branch:
    run existing web LLM invocation pipelines
    return SearchRecord[] OR branch failure

  paper_branch:
    run paper LLM invocation pipelines
    aggregate to PaperRecord[]
    optional OA enrichment
    return PaperRecord[] OR branch failure

  await both branches

  if web branch failed AND paper branch failed:
    raise ALL_PROVIDERS_FAILED

  if web branch succeeded:
    retain its SearchRecord[] (including empty list)
  else:
    log llm_search_branch_failed scope=web

  if paper branch succeeded:
    retain its PaperRecord[] (including empty list)
    admit paper landing URLs
  else:
    log llm_search_branch_failed scope=paper

  ResultWriter.write_mixed_results:
    serialize web records as {type:"web", ...}
    serialize paper records as {type:"paper", ...}
    preserve deterministic branch/result ordering
    write one `llm-<request_id>.jsonl`

  return one path
```

---

### 6. Interfaces & Contracts

#### Request / Response Contract

New direct paper request:

```json
{
  "type": "paper_search",
  "query": "speculative decoding"
}
```

Backward-compatible LLM requests:

```json
{
  "type": "llm_search",
  "prompt": "find recent work"
}
```

```json
{
  "type": "llm_search",
  "prompt": "find recent work",
  "scope": "paper"
}
```

`scope` is one of `web`, `paper`, `all`; absence means `web`.

#### Internal Provider Contract

```python
@dataclass(frozen=True, slots=True)
class PaperSearchHit:
    source: str
    source_id: str
    title: str
    authors: tuple[str, ...] = ()
    abstract: str = ""
    doi: str = ""
    arxiv_id: str = ""
    published_date: date | None = None
    updated_date: date | None = None
    url: str = ""
    pdf_url: str = ""
    venue: str = ""
    topics: tuple[str, ...] = ()
    citation_count: int | None = None
    is_open_access: bool | None = None
    oa_status: str = ""
    license: str = ""

class AcademicSearchProvider(Protocol):
    name: str
    async def search(self, query: str) -> list[PaperSearchHit]: ...
```

`PaperSearchHit` is internal and may contain empty optional fields. `title`, `source`, and `source_id` are required after adapter validation.

#### Final Domain Object Contract

```python
@dataclass(frozen=True, slots=True)
class PaperIdentifiers:
    doi: str = ""
    arxiv_id: str = ""
    semantic_scholar_id: str = ""
    openalex_id: str = ""
    dblp_key: str = ""
    core_id: str = ""

@dataclass(frozen=True, slots=True)
class PaperRecord:
    title: str
    authors: tuple[str, ...]
    abstract: str
    identifiers: PaperIdentifiers
    published_date: date | None
    updated_date: date | None
    url: NormalizedURL
    pdf_url: NormalizedURL | None
    venue: str
    topics: tuple[str, ...]
    citation_counts: Mapping[str, int]
    is_open_access: bool | None
    oa_status: str
    license: str
    sources: tuple[str, ...]
```

`PaperRecord` is the stable paper-search output domain object. Provider-specific arbitrary `extra` mappings are intentionally excluded.

#### OA Resolver Contract

```python
@dataclass(frozen=True, slots=True)
class OAResolution:
    landing_url: NormalizedURL | None
    pdf_url: NormalizedURL | None
    is_open_access: bool
    oa_status: str = ""
    license: str = ""

class OAResolver(Protocol):
    name: str
    async def resolve(self, doi: str) -> OAResolution | None: ...
```

#### JSONL Output Contracts

Existing/default web LLM output remains unchanged:

```json
{"url":"https://example.com","abstract":"summary"}
```

Paper-only output:

```json
{"title":"Example Paper","authors":["A. Author"],"abstract":"...","identifiers":{"doi":"10.1000/example","arxiv_id":""},"published_date":"2026-01-02","updated_date":null,"url":"https://doi.org/10.1000/example","pdf_url":null,"venue":"ExampleConf","topics":["Machine Learning"],"citation_counts":{"openalex":12},"is_open_access":true,"oa_status":"gold","license":"cc-by","sources":["openalex","crossref"]}
```

Mixed `scope=all` output:

```json
{"type":"web","url":"https://example.com","abstract":"summary"}
{"type":"paper","title":"Example Paper","authors":["A. Author"],"abstract":"...","identifiers":{"doi":"10.1000/example"},"published_date":"2026-01-02","updated_date":null,"url":"https://doi.org/10.1000/example","pdf_url":null,"venue":"ExampleConf","topics":[],"citation_counts":{},"is_open_access":null,"oa_status":"","license":"","sources":["llm:provider"]}
```

Dates serialize as ISO-8601 strings or JSON null. Maps/lists are deterministic in output order. Web-only output does not gain a `type` field.
