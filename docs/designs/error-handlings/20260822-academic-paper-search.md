# Error Handling: Academic Paper Search

## 1. Goals

This design extends the existing gateway failure model without creating an academic-only exception hierarchy. The key distinction is between failures that invalidate an entire provider pipeline, malformed individual paper candidates, identity/merge conflicts, and optional OA-enrichment failures.

Required invariants:

- One failed academic provider must not fail `paper-search` when at least one other provider pipeline completes.
- A provider pipeline that completes successfully with zero results counts as a completed pipeline.
- One malformed result inside an otherwise valid provider response must not discard the provider's other valid results.
- Identifier contradictions must never silently merge two papers.
- Unpaywall failure or OA miss must never make an already discovered paper disappear.
- `llm-search --scope all` must preserve a successful web branch when the paper branch fails, and vice versa.
- Failed commands must not leave partial result files.
- User prompts, queries, abstracts, model outputs, secrets, and raw third-party payloads must not be added to diagnostic logs.

---

## 2. Public Error Taxonomy

Reuse the existing `GatewayError` hierarchy:

```text
GatewayError
├── InputFailure
├── ExecutionFailure
│   ├── ProtocolFailure
│   └── ParserFailure
└── ConfigFailure
```

Add only one new public error code for the direct academic workflow:

```python
NO_ACADEMIC_SEARCH_PROVIDERS = "no_academic_search_providers"
```

Do not add provider-specific public error codes such as `OPENALEX_FAILED`, `ARXIV_FAILED`, or `CORE_RATE_LIMITED`. Provider identity and failure category belong in DEBUG events and internal exception messages, while the stable CLI/daemon API keeps coarse-grained gateway errors.

Existing codes remain applicable:

| Situation | Error type/code |
|---|---|
| Empty paper query | `InputFailure(EMPTY_QUERY)` |
| Invalid `llm-search --scope` at protocol boundary | `InputFailure(BAD_REQUEST)` or CLI parser rejection |
| No enabled academic discovery providers | `ExecutionFailure(NO_ACADEMIC_SEARCH_PROVIDERS)` |
| No configured LLM search invocations | `ExecutionFailure(NO_LLM_SEARCH_PROVIDERS)` |
| Every academic provider pipeline failed | `ExecutionFailure(ALL_PROVIDERS_FAILED)` |
| Every selected LLM-search branch failed | `ExecutionFailure(ALL_PROVIDERS_FAILED)` |
| Malformed provider response envelope / invalid JSON/XML | `ProtocolFailure(PROTOCOL_ERROR)` scoped to that provider pipeline |
| Restricted LLM paper format rejected | `ParserFailure(PROTOCOL_ERROR)` scoped to that invocation |
| Invalid enabled academic/resolver configuration | `ConfigFailure(CONFIG_ERROR)` |

---

## 3. Academic Provider Failure Semantics

### 3.1 Pipeline Completion Rule

`PaperSearchOrchestrator` uses the same high-level completion rule as current keyword search.

```text
provider A -> completed with records
provider B -> failed
provider C -> completed with []

=> command succeeds
```

A pipeline is considered completed when the provider request and top-level response parsing finish normally, regardless of whether zero or more valid `PaperSearchHit`s remain after per-item filtering.

Only when every enabled provider pipeline raises an execution/protocol failure does direct `paper-search` raise `ALL_PROVIDERS_FAILED`.

### 3.2 Transport and HTTP Failures

The shared HTTP executor remains responsible for transport failure classification and retries.

Retryable categories remain:

- timeout / transport interruption;
- HTTP 408;
- HTTP 429;
- HTTP 5xx.

Academic adapters do not implement their own `sleep`, retry loops, or broad `except Exception: return []` fallbacks.

A terminal retryable failure becomes an `ExecutionFailure` for that provider pipeline. Non-retryable HTTP 4xx also becomes an `ExecutionFailure`, except where an adapter explicitly defines a status as a normal lookup miss, such as Unpaywall DOI 404.

### 3.3 Response-Envelope Failures

An entire provider pipeline fails with `ProtocolFailure` when the transport succeeded but the top-level provider protocol cannot be interpreted safely, for example:

- arXiv response is not parseable Atom/XML;
- dblp response is not parseable XML;
- JSON provider response is not a JSON object when an object envelope is required;
- required top-level collections such as the expected `results`/`data` envelope have incompatible types;
- response content is structurally unrelated to the configured provider protocol.

Do not silently convert these failures into `[]`; doing so would make provider outages and incompatible API changes indistinguishable from a real empty search result.

---

## 4. Per-Candidate Rejection

When the provider envelope is valid, malformed individual items are isolated rather than failing the entire provider pipeline.

A candidate is rejected when a required paper invariant cannot be established, including:

- missing/blank title;
- missing/blank provider source ID when the adapter cannot derive a stable source-native identifier;
- invalid required landing URL with no safe canonical fallback;
- impossible publication date representation after allowed provider-specific parsing;
- invalid source field/type that prevents deterministic mapping;
- strong identifier contradiction described below.

Optional malformed fields are omitted rather than rejecting the whole candidate when safe, for example:

- non-integer citation count -> `None` for that source;
- malformed optional PDF URL -> no `pdf_url`;
- malformed optional date when another valid publication date exists -> use the valid value;
- malformed topic entry -> discard that topic entry;
- absent abstract -> keep `abstract=""`.

Each rejection emits a fixed-code event without payload bodies:

```text
event=paper_candidate_rejected
provider=openalex
reason=missing_title
```

Suggested stable reason codes:

```text
missing_title
missing_source_id
invalid_landing_url
invalid_identifier
invalid_date
invalid_record_shape
identifier_conflict
```

Do not include the raw candidate, paper title, abstract, query, or model output in these events.

---

## 5. Identifier Conflict Handling

### 5.1 Strong Identifiers

Strong identity evidence includes:

- normalized DOI;
- normalized arXiv work ID;
- provider-native `(source, source_id)`.

If a candidate connects clusters through compatible strong identifiers, the clusters are merged transitively.

### 5.2 Contradiction Rule

A candidate is rejected rather than merged when it would assert an impossible strong-identifier relationship already contradicted by the same identity namespace.

Examples:

```text
Existing source identity:
  (openalex, W123) -> DOI 10.a/foo

New candidate:
  (openalex, W123) -> DOI 10.b/bar

=> reject new candidate as identifier_conflict
```

Similarly, a canonical DOI must not be overwritten by a different DOI merely because title/author metadata is similar.

Conflicts between weak metadata are not fatal. Title punctuation, author formatting, venue formatting, dates, citation counts, and abstract wording can differ across providers and are handled by deterministic field-selection rules.

### 5.3 Weak Matching Is Non-Authoritative

Bibliographic fingerprints are only a conservative fallback. A weak match must never override contradictory strong identifiers.

If two records have the same normalized title/author/year fingerprint but explicitly different DOIs, they remain separate papers and a DEBUG merge-suppression event may be emitted.

---

## 6. Deterministic Merge Failure Policy

Paper merging must not depend on asynchronous provider completion order.

For each field, define deterministic precedence or union semantics. The merge implementation must fail closed if an internal invariant is violated rather than choosing whichever record arrived first.

Recommended categories:

- `identifiers`: union only when compatible; contradiction rejects the incoming candidate.
- `sources`: stable union in configured source-priority order.
- `citation_counts`: source-keyed union; never collapse different provider counts into one unexplained number.
- `topics`: normalized stable union.
- `title`, `authors`, `venue`, dates: deterministic provider/quality precedence.
- `abstract`: non-empty academic API metadata preferred over LLM-derived metadata; deterministic source priority within the same class.
- `pdf_url`: trusted direct/OA API location preferred over LLM-provided location; invalid URL discarded.
- OA fields: explicit resolver/provider data overlays missing values according to fixed rules, without replacing a stronger verified direct PDF URL with a weaker landing URL.

An unexpected implementation invariant such as an impossible cluster data structure state should surface as an internal execution failure, not be swallowed as a candidate rejection.

---

## 7. Unpaywall / OA Resolver Semantics

Unpaywall is optional enrichment, never a discovery requirement.

### 7.1 Normal Misses

The following are normal non-error outcomes from the resolver and return `None` or a resolution with no PDF:

- DOI not found (HTTP 404);
- DOI exists but is not open access;
- record has no usable OA location;
- OA location exists only as a landing page and no PDF URL is available.

These outcomes do not generate command failures.

### 7.2 Resolver Execution Failures

Timeout, terminal 429/5xx, invalid JSON, or otherwise malformed Unpaywall protocol data are resolver failures. They are logged as enrichment failures and the pre-enrichment `PaperRecord` is retained.

```text
event=paper_enrichment_failed
resolver=unpaywall
stage=oa_resolve
error_type=ExecutionFailure|ProtocolFailure
```

Do not expose a successful paper-search command as failed solely because OA resolution failed.

### 7.3 Configuration Semantics

```text
Unpaywall not configured
=> resolver absent; startup succeeds

Unpaywall explicitly enabled and required contact-email environment variable is missing/empty
=> ConfigFailure(CONFIG_ERROR) during daemon startup
```

This preserves the existing gateway principle that explicitly enabled but invalid features fail early, while unconfigured optional features are simply absent.

---

## 8. LLM Paper Search Failure Semantics

### 8.1 Paper-Only Scope

`llm-search --scope paper` mirrors current multi-invocation LLM search behavior:

- each configured LLM invocation is independent;
- model/provider transport failure fails only that invocation;
- malformed restricted paper output raises `ParserFailure` for that invocation;
- a parsed empty paper result counts as a completed invocation;
- when at least one invocation completes, aggregate the completed invocation outputs;
- when every invocation fails, raise `ALL_PROVIDERS_FAILED`.

The paper parser must never partially reinterpret malformed mixed web/paper output. Its grammar accepts only the paper format defined by the prompt contract.

### 8.2 `scope=all` Partial Success

The web and paper branches are independently successful or failed.

| Web branch | Paper branch | Command outcome |
|---|---|---|
| success | success | write both result classes |
| success | failure | write web results only; log paper branch failure |
| failure | success | write paper results only; log web branch failure |
| failure | failure | raise `ALL_PROVIDERS_FAILED`; write no result file |

A successful branch may contain zero records. Therefore `success + failure` can legally produce an empty result file if the successful branch completed with no results.

This rule prevents `scope=all` from being less reliable than either individual scope.

---

## 9. Result Persistence Failures

Existing result-writing safety rules remain mandatory:

1. Serialize every output record before creating the destination file.
2. If any record fails validation/serialization, no result file is created.
3. Create the target using exclusive creation as today.
4. If a filesystem/write failure occurs after creation, best-effort delete the partial target.
5. Never return a result path until the file is fully written.

Paper serialization validation includes at least:

- non-empty title;
- valid normalized landing URL;
- valid normalized optional PDF URL;
- JSON-serializable deterministic identifiers/maps/lists;
- valid ISO date conversion;
- non-negative integer citation counts when present;
- no duplicate `sources` entries.

Mixed `scope=all` serialization adds the `type` discriminator at the sink only; domain objects themselves remain unchanged.

---

## 10. URL Admission Failures

A final `PaperRecord.url` is admitted into the existing `URLStore` using:

```python
admission_abstract = record.abstract.strip() or record.title.strip()
```

Because final paper titles are required non-empty, this preserves the `URLStore.admit` invariant without modifying the store.

If final landing URL normalization somehow fails after aggregation, treat that as an internal paper-record invariant failure rather than silently writing a record that cannot participate in existing URL-fetch semantics.

`pdf_url` is not admitted in this feature, so PDF-specific fetch failures are out of scope.

---

## 11. Configuration Failures

Academic configuration is validated at runtime assembly/startup rather than lazily on first request.

Reject:

- unknown enabled academic provider;
- unknown provider option;
- non-positive concurrency;
- required API-key environment variable missing for a provider configured to require it;
- optional API-key variable name present but invalid type/empty name;
- required contact-email variable missing for explicitly enabled Unpaywall;
- reserved adapter constructor option names supplied through config;
- malformed API URLs or unsupported registration settings where validation can be performed locally.

Do not perform live network credential checks in `doctor` or startup; preserve the existing no-network diagnostic/startup philosophy unless a separate future feature changes it.

---

## 12. Observability and Redaction

Academic failures should follow the existing structured event model.

Recommended events/stages:

```text
provider_started           stage=paper_search
provider_completed         stage=paper_search
provider_failed            stage=paper_search
paper_candidate_rejected   reason=<fixed code>
paper_clusters_merged      reason=shared_doi|shared_arxiv|bridged_identity|bibliographic_match
paper_merge_suppressed     reason=strong_identifier_conflict
provider_started           stage=oa_resolve
provider_completed         stage=oa_resolve
paper_enrichment_failed    stage=oa_resolve
llm_search_branch_failed   scope=web|paper
results_written            kind=paper|llm
```

Safe operational metadata may include provider/resolver name, stage, elapsed time, attempt number, status code, counts, normalized identifier *type* presence, and fixed reason codes.

Do not log:

- raw search query/prompt;
- paper title/abstract/authors;
- raw DOI if project logging policy treats target identifiers as content (prefer hashed/redacted identifier or only `has_doi=true` unless a future explicit policy permits DOI logging);
- raw model output;
- raw provider response;
- API keys/contact values.

Existing central secret redaction remains defense in depth.

---

## 13. Failure Matrix

| Boundary | Failure | Scope affected | User-visible command failure? |
|---|---|---|---:|
| CLI/protocol | invalid scope | request | yes |
| Config | no academic providers enabled when `paper-search` invoked | request | yes |
| Config | explicitly enabled resolver missing required env | daemon startup | yes |
| Provider HTTP | timeout / 429 / 5xx after retries | one provider | only if all discovery providers fail |
| Provider protocol | malformed JSON/XML envelope | one provider | only if all discovery providers fail |
| Provider item | one malformed paper | one candidate | no |
| Aggregation | weak metadata disagreement | field/merge decision | no |
| Aggregation | strong identifier contradiction | incoming candidate | no, candidate rejected |
| Aggregation | impossible internal cluster invariant | workflow | yes |
| Unpaywall | DOI not found / no OA | one paper enrichment | no |
| Unpaywall | timeout / malformed response | one paper enrichment | no |
| LLM parser | one invocation malformed | one invocation | only if all selected invocation pipelines/branches fail |
| LLM all | web branch failed, paper succeeded | web branch | no |
| LLM all | paper branch failed, web succeeded | paper branch | no |
| Persistence | serialization/write failure | workflow | yes; partial file removed |
