"""``derive-hierarchy`` command — offline Schema-2 sealed-L4 hierarchy build.

Reads an already-sealed, already-published L4 serving run (the same
authority ``project-serving`` writes), converts its asserted-only entities
and typed directed relationships into W2's own ``EntityRow``/``RelationshipRow``
hierarchy-core shape via
:func:`fabric_kg_builder.semantic.sealed_l4_hierarchy_source.build_hierarchy_core_rows`,
runs W2's community/hierarchy build
(:func:`fabric_kg_builder.graph.community_hierarchy_v2.build_derived_community_hierarchy_safe`)
against those rows, and persists the resulting outcome as a new, separately
versioned derived-hierarchy artifact + receipt via
:func:`fabric_kg_builder.semantic.derived_hierarchy_artifact.write_derived_hierarchy_artifact`.

This command makes no LLM, Foundry, Document Intelligence, embedding,
Search, or Fabric call, and requires no Azure authentication or resource.
It never mutates the sealed L4 run, any L3 input-manifest search root, any
local L1-L4 state directory, or the local release-plan directory it
protects by default; the writer itself also always force-protects the
sealed L4 run root regardless of what this command passes.

This command is additive: it does not replace, migrate, or reinterpret any
existing sealed L4/L5a bytes, hashes, or receipts, and it does not invoke
the legacy ``GraphOrchestrator`` path. Live publication (L5a) of the
resulting artifact is a separate, later stage owned elsewhere.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import click

from fabric_kg_builder.graph.community_hierarchy_v2 import (
    build_derived_community_hierarchy_safe,
)
from fabric_kg_builder.graph.community_provider import CommunityProvider
from fabric_kg_builder.semantic.derived_hierarchy_artifact import (
    DerivedHierarchyAdapterError,
    write_derived_hierarchy_artifact,
)
from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import (
    SealedL4HierarchyCoreInput,
    build_hierarchy_core_rows,
    load_sealed_l4_hierarchy_rows,
)

#: Exit code for sealed-input unavailability, a fail-closed core-mapping
#: rejection, a native-provider dependency that is not importable/supported,
#: or a writer-level interface problem (output-root collision, already-
#: published run, unrecognized status) -- i.e. the command never reached a
#: receipted hierarchy-core outcome at all.
_INPUT_OR_UNEXPECTED_ERROR_EXIT = 1
#: Receipted outcome status "insufficient": a real, valid, structurally
#: sound hierarchy-core result was built and published, but the projection
#: had zero non-self-loop edges, so every entity is its own terminal
#: community -- not a failure, but explicitly not "complete" either.
_INSUFFICIENT_EXIT = 2
#: Receipted outcome status "empty": the sealed input had zero entities.
#: Distinct from every other status because the receipt has no
#: execution/input-graph hash to bind (consistent with W2 L5a's own
#: ``L5A_HIERARCHY_EMPTY_UNBOUND`` refusal semantics) -- this command
#: never fabricates one.
_EMPTY_EXIT = 3
#: Receipted outcome status "native_unavailable": the configured provider's
#: native dependency is not importable (e.g. the optional ``leiden``
#: dependency-group extra is not installed, or the running interpreter is
#: outside the pinned package's supported range). No algorithmic fallback
#: is substituted.
_NATIVE_UNAVAILABLE_EXIT = 4
#: Receipted outcome status "failed": the provider ran but its output (or
#: the requested build configuration) could not be validated. Reused from
#: this repository's existing stage-failure convention
#: (``schema2_stages_cmd._STAGE_FAILURE_EXIT``) so a fail-closed outcome
#: reported *after* a successful write carries the same exit code as other
#: schema-2 stage failures reported elsewhere in this CLI.
_FAILED_EXIT = 5

_STATUS_EXIT_CODES: Mapping[str, int] = {
    "complete": 0,
    "insufficient": _INSUFFICIENT_EXIT,
    "empty": _EMPTY_EXIT,
    "native_unavailable": _NATIVE_UNAVAILABLE_EXIT,
    "failed": _FAILED_EXIT,
}

#: Private test-only provider-injection hook. Never set outside this
#: module's own test suite: production callers always get the real
#: ``NativeLeidenProvider`` constructed fresh (and lazily imported) inside
#: ``derive_hierarchy_cmd`` itself. A test may monkeypatch this module
#: attribute to a zero-arg factory returning any ``CommunityProvider`` --
#: most commonly a deterministic fake or the dependency-free
#: ``DeterministicConnectedComponentsProvider`` -- to exercise this
#: command's wiring/exit-code behaviour without the real native dependency.
#: Nothing reachable from this module ever claims such an injected result
#: is a "native" acceptance; callers doing so are responsible for making
#: that distinction in their own reporting.
_test_only_provider_factory: Callable[[], CommunityProvider] | None = None


def _resolve_provider() -> CommunityProvider:
    if _test_only_provider_factory is not None:
        return _test_only_provider_factory()
    # Lazy import: constructing ``NativeLeidenProvider`` never imports the
    # native dependency itself (deferred to first ``.partition()`` call,
    # which ``build_derived_community_hierarchy_safe`` already converts to
    # a non-raising ``NATIVE_UNAVAILABLE`` outcome) -- importing this
    # module at CLI-registration time therefore never requires the
    # optional ``leiden`` dependency-group extra to be installed.
    from fabric_kg_builder.graph.community_provider import NativeLeidenProvider

    return NativeLeidenProvider()


def _evidence_sidecars(
    core_input: SealedL4HierarchyCoreInput,
) -> tuple[Mapping[str, Sequence[str]], Mapping[str, Sequence[str]]]:
    """Build the entity/relationship evidence sidecars from the sealed rows.

    Every ``EntityRow``/``RelationshipRow`` ``build_hierarchy_core_rows``
    produced already carries its own sealed ``evidence_span_ids`` (mapped
    onto the row's ``evidence_ids`` field); this builds the id-keyed
    mapping both ``build_derived_community_hierarchy_safe`` and
    ``write_derived_hierarchy_artifact`` accept from that same sealed
    evidence, so the community build and the receipted
    ``evidence_binding_hash`` are computed against one real, shared
    evidence sidecar derived from the sealed input -- never an empty
    default standing in for it.
    """

    entity_evidence = {
        row.entity_id: tuple(row.evidence_ids or ())
        for row in core_input.entities
    }
    relationship_evidence = {
        row.relationship_id: tuple(row.evidence_ids or ())
        for row in core_input.relationships
    }
    return entity_evidence, relationship_evidence


def _default_protected_roots(
    *,
    l1_state: str,
    l2_state: str,
    l3_state: str,
    l4_state: str,
    l5a_release_dir: str,
    input_manifest_search_roots: Sequence[Path],
) -> dict[str, Path]:
    """The concrete protected-root defaults this command always wires in.

    Covers every root this offline integration actually touches or reads
    from, beyond the sealed L4 run root itself (which
    ``write_derived_hierarchy_artifact`` force-protects unconditionally):
    every L3 input-manifest search root passed to
    ``load_sealed_l4_hierarchy_rows``, the conventional local L1-L4 state-
    directory defaults, and the local L5a release-plan directory
    convention. A caller-visible label names each root so a rejection
    names exactly which one collided.
    """

    roots: dict[str, Path] = {
        ".fkg/l1 state": Path(l1_state),
        ".fkg/l2 state": Path(l2_state),
        ".fkg/l3 state": Path(l3_state),
        ".fkg/l4 state": Path(l4_state),
        "L5a release-plan": Path(l5a_release_dir),
    }
    for index, search_root in enumerate(input_manifest_search_roots):
        roots[f"L3 input-manifest search root #{index}"] = search_root
    return roots


def _fail(message: str, *, code: str | None = None) -> None:
    """Report a fail-closed, typed, no-traceback error and stop."""
    label = f"{code}: " if code else ""
    click.echo(f"[derive-hierarchy] FAILED: {label}{message}", err=True)
    sys.exit(_INPUT_OR_UNEXPECTED_ERROR_EXIT)


_DERIVE_HIERARCHY_EPILOG = """\b
Example:
  fabric-kg derive-hierarchy --source .fkg/l4/<run-id> --out .fkg/derived-hierarchy/<run-id> \\
      --run-id hier-run-1 --config-id default --policy-id default \\
      --input-manifest-search-root .fkg/l3

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("derive-hierarchy", epilog=_DERIVE_HIERARCHY_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--source", "source_root", required=True, type=click.Path(),
              help="Sealed L4 run root to read asserted-only facts from.")
@click.option("--out", "output_root", required=True, type=click.Path(),
              help="Output root for the new derived-hierarchy artifact + receipt. "
                   "Must not exist yet and must not collide with any protected root.")
@click.option("--input-manifest-search-root", "input_manifest_search_roots",
              multiple=True, required=True, type=click.Path(),
              help="L3 input-manifest search root (repeatable); forwarded to the "
                   "sealed-L4 reader's authority check and protected as an output-root "
                   "collision guard.")
@click.option("--run-id", "run_id", required=True, help="Caller-assigned hierarchy run id.")
@click.option("--config-id", "config_id", default="default", show_default=True,
              help="Caller-assigned build-configuration id.")
@click.option("--policy-id", "policy_id", required=True,
              help="Caller-assigned community-detection policy id.")
@click.option("--seed", "seed", default=0xDEADBEEF, show_default=True, type=int,
              help="Deterministic partition seed.")
@click.option("--max-cluster-size", "max_cluster_size", default=10, show_default=True,
              type=int, help="Maximum community size before further splitting.")
@click.option("--l1-state", "l1_state", default=".fkg/l1", show_default=True,
              type=click.Path(), help="Approved L1 state directory (protected root).")
@click.option("--l2-state", "l2_state", default=".fkg/l2", show_default=True,
              type=click.Path(), help="Completed L2 state directory (protected root).")
@click.option("--l3-state", "l3_state", default=".fkg/l3", show_default=True,
              type=click.Path(), help="L3 state directory (protected root).")
@click.option("--l4-state", "l4_state", default=".fkg/l4", show_default=True,
              type=click.Path(), help="L4 state directory (protected root).")
@click.option("--l5a-release-dir", "l5a_release_dir", default="build/release",
              show_default=True, type=click.Path(),
              help="Local L5a release-plan directory convention (protected root).")
def derive_hierarchy_cmd(
    source_root: str,
    output_root: str,
    input_manifest_search_roots: tuple[str, ...],
    run_id: str,
    config_id: str,
    policy_id: str,
    seed: int,
    max_cluster_size: int,
    l1_state: str,
    l2_state: str,
    l3_state: str,
    l4_state: str,
    l5a_release_dir: str,
) -> None:
    """Build and publish a new derived community hierarchy from sealed L4 (offline).

    Reads a sealed L4 serving run, maps its asserted-only entities and typed
    directed relationships onto W2's hierarchy-core row shape, runs the
    community/hierarchy build, and persists the outcome as a new, separately
    versioned derived-hierarchy artifact + receipt. Unsealed, stale, tampered,
    or unapproved input is rejected before any build is attempted; a sealed
    partial extraction scope is preserved and reported, never silently
    widened or narrowed. Never mutates the sealed input, any L3 search root,
    any local L1-L4 state directory, or the local release-plan directory.

    Makes no remote call of any kind and requires no Azure authentication.

    Exit codes: 0 complete · 1 input/unexpected error ·
    2 insufficient · 3 empty · 4 native_unavailable · 5 failed.
    """

    search_roots = tuple(Path(root) for root in input_manifest_search_roots)
    click.echo(f"[derive-hierarchy] Reading sealed L4 run {source_root} ...")
    try:
        sealed = load_sealed_l4_hierarchy_rows(
            Path(source_root), input_manifest_search_roots=search_roots
        )
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        _fail(f"sealed L4 input is unavailable or failed an authority check: {exc}")
        return

    click.echo(
        f"  entities: {len(sealed.entities)}, relationships: {len(sealed.relationships)}"
    )
    if sealed.partial_scope is not None:
        click.echo(f"  sealed partial scope: {sealed.partial_scope}")

    try:
        core_input = build_hierarchy_core_rows(sealed)
    except ValueError as exc:
        _fail(f"sealed input could not be mapped onto the hierarchy core: {exc}")
        return

    entity_evidence, relationship_evidence = _evidence_sidecars(core_input)

    provider = _resolve_provider()
    click.echo(
        f"[derive-hierarchy] Building hierarchy with provider "
        f"{provider.provider_id}=={provider.provider_version} ..."
    )
    outcome = build_derived_community_hierarchy_safe(
        list(core_input.entities),
        list(core_input.relationships),
        provider=provider,
        policy_id=policy_id,
        run_id=run_id,
        config_id=config_id,
        max_cluster_size=max_cluster_size,
        seed=seed,
        entity_evidence=entity_evidence,
        relationship_evidence=relationship_evidence,
    )

    protected_roots = _default_protected_roots(
        l1_state=l1_state,
        l2_state=l2_state,
        l3_state=l3_state,
        l4_state=l4_state,
        l5a_release_dir=l5a_release_dir,
        input_manifest_search_roots=search_roots,
    )

    click.echo(f"[derive-hierarchy] Publishing outcome status={outcome.status} ...")
    try:
        result = write_derived_hierarchy_artifact(
            sealed=sealed,
            outcome=outcome,
            output_root=Path(output_root),
            run_id=run_id,
            config_id=config_id,
            policy_id=policy_id,
            seed=seed,
            max_cluster_size=max_cluster_size,
            requested_provider_name=provider.provider_id,
            requested_provider_version=provider.provider_version,
            entity_evidence=entity_evidence,
            relationship_evidence=relationship_evidence,
            protected_roots=protected_roots,
        )
    except DerivedHierarchyAdapterError as exc:
        _fail(str(exc))
        return

    receipt = result.receipt
    click.echo(f"  status: {receipt.status}")
    click.echo(f"  receipt: {receipt.derived_hierarchy_receipt_id}")
    click.echo(f"  receipt hash: {receipt.receipt_hash}")
    click.echo(f"  input graph hash: {receipt.input_graph_hash}")
    click.echo(f"  execution fingerprint: {receipt.execution_fingerprint}")
    click.echo(f"  evidence binding hash: {receipt.evidence_binding_hash}")
    if receipt.reason:
        click.echo(f"  reason: {receipt.reason}")
    click.echo(f"  output root: {result.output_root}")
    click.echo(f"  manifest: {result.manifest_path}")
    click.echo(f"  receipt file: {result.receipt_path}")

    exit_code = _STATUS_EXIT_CODES.get(receipt.status, _FAILED_EXIT)
    if exit_code != 0:
        sys.exit(exit_code)
