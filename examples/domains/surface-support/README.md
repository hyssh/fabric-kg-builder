# Surface field-service — example domain

An **example**, not a default. Nothing in `fabric-kg` loads anything from this
directory unless you point a command at it explicitly.

## Why it moved here

These files used to sit at the repository root as `ontology/model.yaml` and
`ontology/ids.lock.json`. That location is where a *user's own project* keeps
its ontology — `fabric-kg init` scaffolds a domain-neutral one there. Keeping a
fully populated Surface hardware ontology at that path in the tool's own
repository meant the tool shipped with one corpus's domain model installed as
if it were the product's ontology, and runtime code path-searches for exactly
that filename (`validate/suite.py`, `agent/tools/fabric_data.py`).

The practical consequence was that a second domain had no place to live.

## Contents

| File | What it is |
|---|---|
| `model.yaml` | Ontology model for a Surface device service/troubleshooting domain — entity types, relationships, and their bindings. |
| `ids.lock.json` | Stable-ID lock for that model. Meaningless outside it. |
| `type-profiles.yaml` | The `surface-support` entity-type allowlist, previously hardcoded as `SURFACE_SUPPORT_TYPES` in `ontology/multitype_plan.py`. |

## Using it

```bash
fabric-kg compile-ontology \
  --model examples/domains/surface-support/model.yaml \
  --ids   examples/domains/surface-support/ids.lock.json \
  --out   build/ontology
```

## Starting your own domain instead

```bash
fabric-kg init                # scaffolds a domain-neutral ontology/model.yaml
fabric-kg init-domain ...     # infers a domain contract from your data
```

Read this example for shape and conventions. Do not copy its entity types
unless you are actually modelling repairable hardware.

> **Known gap:** the inferred domain contract (`domain.yaml`) is not yet wired
> into `compile-ontology`, so the ontology model still has to be authored or
> edited by hand today. That gap is what makes an example like this necessary,
> and closing it is tracked separately from this cleanup.
