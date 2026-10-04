---
name: surface-repro
description: Prepare an optional Surface service-guide example with user-downloaded documents and the current fabric-kg corpus-first workflow. Keep model spending, design approval, extraction, and Fabric publication separately authorized.
---

## Purpose

Apply the `fabric-kg-pipeline` skill to a Surface service-guide corpus. This is
an optional worked example, not the default product taxonomy, a bundled dataset,
or a one-command deployment recipe.

The plugin requires a separately installed `fabric-kg` Python CLI. A clone is
needed only for repository setup examples, not for ordinary use with the
installed CLI and the user's own documents.

## Prepare the source

Ask the user which device, guide revisions, documents, and questions are in
scope. Obtain the PDFs from Microsoft's public service-guide download page:

https://www.microsoft.com/en-us/download/details.aspx?id=100440

Keep downloaded PDFs in a local, uncommitted corpus folder. A repository clone
includes setup guidance at `sample_data/Surface_Troubleshootings/README.md`,
but no PDFs, pre-extracted graph, or curated question file is bundled.
Do not infer that the user has downloaded every guide listed in that document.

Possible evaluation questions include:

- Which tools and components are explicitly supported for the selected task?
- What prerequisites and precautions are stated in the selected guide?
- Which procedure steps have recorded order, and which remain unresolved?
- Which requested part numbers or specifications are absent from the extraction?

Confirm questions and expected evidence with the user rather than presenting
these examples as a validated answer set.

## Use the current pipeline

Check the installed version and command contracts:

```bash
fabric-kg --version
fabric-kg domain discover --help
fabric-kg domain design --help
fabric-kg domain evaluate-design --help
fabric-kg domain compile-design --help
fabric-kg domain approve --help
fabric-kg enrich --help
fabric-kg validate-evidence --help
fabric-kg project-serving --help
```

Follow `fabric-kg-pipeline` for complete corpus accounting, design evaluation,
compilation, and explicit approval. Do not run deprecated `set-domain` or
unconditional `densify` as substitutes. Do not impose a fixed number of entity
types, invent connections, or force facts to fit a sample graph.

Discovery, OCR, and enrichment may make paid remote calls. A local artifact
destination does not mean the operation is offline. Obtain the required
credentials, scope, model-call limits, and spending approval before execution.
Preserve unsupported documents, unresolved evidence, and partial coverage.

After evidence validation and sealed L4 projection, native Hierarchical Leiden
is optional. It requires the `leiden` extra on Python 3.10–3.13, uses a fresh
output root, and has no fallback algorithm. Community grouping is structural,
not proof of better answers or correct procedure order.

## Publication and evaluation

Inspect `fabric-kg app publish-structured --help` and
`fabric-kg app publish-prototype-agent --help` only when publication is requested.
Review the default dry-run plan and obtain separate explicit live approval.
The create-only prototype is nontransactional and retains partial resources.

The current structured publisher creates Lakehouse + Ontology. The Data Agent
uses Ontology v1 as its sole structured source; AI Search and Lakehouse SQL are
not automatically attached. Creating an Agent does not establish answer quality.

Compare representative answers with the actual source guide. Distinguish
observed source facts, inferred ordering, and missing information. The screenshot
on the project site is an illustrative user-supplied example, not a benchmark.
Verify the source service guide before performing any repair.

Never commit source corpora, credentials, customer conversations, or
resource-specific configuration. Do not hand-edit sealed artifacts or receipts.
