# Testing the schema-2 document-review prototype

This local prototype retains DomainContractV2 and adds a document challenge and
revision loop. It does not claim that the complete cloud release roadmap is done.

## Capability discovery

```bash
fabric-kg --version
fabric-kg domain assessment-schema
fabric-kg domain assess --help
fabric-kg domain review-assessment --help
fabric-kg domain revise --help
fabric-kg domain analyze-layout --help
fabric-kg enrich --help
```

`assessment-schema` prints the actual versioned JSON schemas. Package version
alone is insufficient to identify a prototype build; record the source commit.

## Foundry project inference

The CLI can explicitly use a Foundry project Responses endpoint instead of the
account-level Chat Completions endpoint:

```yaml
foundry:
  endpoint: ${FOUNDRY_PROJECT_ENDPOINT}
  inference_api: project_responses
  chat_deployment: gpt-4.1
  request_timeout_seconds: 300
```

Use an existing deployment available to that project. This transport requires
the existing `agent` optional dependency (`azure-ai-projects>=2.3`), authenticates
with AzureCliCredential, uses `store: false`, and never falls back to API keys or
another endpoint. It supports JSON generation, not embeddings. The default
`chat_completions` transport retains its existing configuration.

Model connectivity is separate from a valid ontology proposal. `init-domain`
still rejects generated candidates that violate evidence, ordering or endpoint
contracts. HTTP provider failures now retain a bounded, redacted status/detail
in the CLI failure audit.

## 1. Propose and inspect

Use `init-domain --input ... --intake ... --non-interactive` to prepare a blocked
draft. Live L1 generation uses the configured model and requires a budget.
`--candidates` supplies an explicitly offline fixture instead. The draft is not
approved merely because the command exits successfully.

Assessment defaults to planning, with no model calls or output-file writes:

```bash
fabric-kg domain assess --file domain.yaml --input ./documents
```

Inspect `planned_windows` and `file_dispositions`. Only then run a bounded
assessment:

```bash
fabric-kg --config fabric-kg.yaml domain assess \
  --file domain.yaml --input ./documents \
  --live --max-calls 4 --max-output-tokens 1600 \
  --out .fkg/assessments/first.json
```

`--responses` is an offline JSON object mapping exact planned window IDs to
`{"findings": [...]}`. Do not label fixture execution as model validation.

Reports are create-only. Use a new `--out` and `--checkpoint` pointing to an
earlier report to continue matching inputs. Responses are also cached beside the
report, by exact request/model fingerprint. Cached responses are revalidated.

`complete` means all represented text windows were assessed with no excluded or
failed file dispositions. It does not prove semantic recall. Deferred windows,
unsupported modalities and unlabelled source facts remain explicit limitations.

## 2. Review and produce a separate revision

Create a decisions array with each report finding's **actual** ID, a disposition
(`accepted`, `rejected`, or `deferred`) and nonempty rationale. Decide every
finding exactly once; do not invent IDs or auto-accept merely to unblock a run.

```bash
fabric-kg domain review-assessment \
  --file domain.yaml --assessment .fkg/assessments/first.json \
  --decisions decisions.json --actor reviewer \
  --out .fkg/reviews/first.json --revision-out .fkg/reviews/request.json
```

Reviewing findings does not approve the ontology. `domain revise` defaults to
planning; `--live --max-calls 2` permits bounded model generation, while
`--candidates` supplies an offline L1 proposal fixture.

```bash
fabric-kg domain revise \
  --parent-state .fkg/l1 --input ./documents \
  --assessment .fkg/assessments/first.json \
  --review .fkg/reviews/first.json --request .fkg/reviews/request.json \
  --out-state .fkg/revisions/second
```

The command re-reads the corpus, verifies report windows against actual sources,
supplies the parent definitions, and mints fresh design evidence for accepted
locations. It cannot silently remove IDs, change identity/hierarchy/property
types or add required properties to existing types. Such breaking changes need
a separately authorized workflow.

The child stays a blocked draft. The parent is unchanged. Inspect the child and
its recorded change set, then use `domain approve` with the exact project ID,
run ID and proposal hash. Never substitute placeholders or hand-edit receipts.

## 3. Consume explicit approved state

Pass both the approved domain and its corresponding L1 state:

```bash
fabric-kg enrich --input ./documents \
  --domain-file .fkg/revisions/second/domain.yaml \
  --l1-state .fkg/revisions/second --l2-state .fkg/runs/second/l2 \
  --dry-run
```

Without `--dry-run`, enrichment may call the model. It checks the current corpus
against the approved manifest. L2 output must not overlap sources or L1 state;
`--force` cannot delete an explicitly supplied state root.

Continue with `validate-evidence` and `project-serving`, supplying the matching
`--l1-state`, `--l2-state`, `--l3-state` and `--domain` arguments from their help.
`project-serving` prints its actual immutable run root; that, not merely the L4
state parent directory, is the input to `app publish-structured --l4-run`.

Property values now retain owner identity, canonical scalar JSON and evidence
through L2/L3/L4 into typed L5a columns. Only literal source-supported values are
asserted; unsupported normalization transformations remain non-asserting.

## 4. Optional raw OCR replay

```bash
fabric-kg domain analyze-layout \
  --input document.pdf --endpoint "$AZURE_DOCINTEL_ENDPOINT" \
  --cache-dir .fkg/ocr --max-pages 1
```

Default mode is a plan. Explicit `--live` permits one analysis POST using the
current Azure CLI identity, never API-key fallback. Over-page/byte-limit inputs
are rejected before submission. A matching cache makes no new POST. Ambiguous
failures retain a reservation; do not remove it to retry blindly.

The cache preserves lossless raw response JSON, Unicode codepoints, tables and
geometry. Its 1.1 format rejects legacy 1.0 with explicit guidance rather than
silently normalizing or reanalyzing it.

Save the returned nonsecret `extractor_identity` as JSON, then supply
`domain assess --ocr-cache .fkg/ocr --ocr-identity identity.json`.
For OCR-backed revisions, also pass `domain revise --ocr-cache .fkg/ocr`.
Do not claim cached OCR text is perfect interpretation of the original image.

## 5. Offline acceptance and live boundaries

The existing pytest runner contains a complete fake-provider CLI exercise:

```bash
python -m pytest tests/unit/test_schema2_prototype_cli.py \
  tests/unit/test_domain_assessment.py tests/unit/test_domain_revision.py \
  tests/unit/test_layout_cache_cmd.py -q --no-cov -p no:cacheprovider
```

The CLI exercise reaches approved L1, L2, L3, L4 and typed L5a materialization
with a non-null source-grounded scalar. This is not a live Fabric deployment.

Do not use `densify` as a schema-2 step or route around missing authority through
legacy output files. Concrete schema-2 live Fabric publication remains
capability-gated. Heterogeneous endpoint key signatures, full cross-release
migration, and production-wide recovery remain separate work.
In particular, current generated physical crosswalk IDs are not yet a proven
stable-ID upgrade mechanism. Do not infer safe deployed schema evolution from
a successful local draft revision.

Model/DI access denial is a blocker requiring the resource owner to confirm
data-plane permissions/access policy. Stop rather than retry with keys, other
identities or another resource. Successful management-plane listing is not
proof that inference is authorized.
