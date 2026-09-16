"""Offline assessment of sealed historical or current L3/L4 publications."""

from __future__ import annotations

import json
from pathlib import Path

import click

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
from fabric_kg_builder.serving.business_quality import (
    assess_business_quality,
    parse_quality_policy,
)


@click.command("assess-business-quality")
@click.option("--l4-run", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--l3-root", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--quality-policy", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--expected-policy-hash", help="Optional canonical SHA-256 of the normalized approved policy JSON (including defaults).")
@click.option("--graph-definition", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Optional local native Graph definition (parts JSON); report presentation gaps separately.")
@click.option("--output", required=True, type=click.Path(dir_okay=False, path_type=Path))
def assess_business_quality_cmd(
    l4_run: Path,
    l3_root: Path,
    quality_policy: Path | None,
    expected_policy_hash: str | None,
    graph_definition: Path | None,
    output: Path,
) -> None:
    """Write counts, row-level blockers/warnings, and exact input hashes.

    No cloud/model calls. Exit 5 means the report was written with blockers;
    exit 1 means invalid inputs. Without a policy, assess historical artifacts
    against existing declared fields without retroactively approving labels.
    """
    try:
        policy = None
        if quality_policy is not None:
            policy = parse_quality_policy(json.loads(quality_policy.read_text("utf-8"))).model_dump(mode="json")
        if expected_policy_hash is not None and (
            policy is None or canonical_sha256(policy) != expected_policy_hash
        ):
            raise ValueError("BUSINESS_QUALITY_POLICY_HASH_MISMATCH")
        source = SealedL4ServingSource.from_run(
            l4_run, input_manifest_search_roots=(l3_root,),
        )
        from fabric_kg_builder.serving.l5a_crosswalk import compile_publication_crosswalks

        crosswalks = compile_publication_crosswalks(source)
        graph = json.loads(graph_definition.read_text("utf-8")) if graph_definition else None
        report = assess_business_quality(source, policy=policy, crosswalks=crosswalks, graph_definition=graph)
        if output.resolve().is_relative_to(l4_run.resolve()) or output.resolve().is_relative_to(l3_root.resolve()):
            raise ValueError("Write the assessment outside sealed input roots.")
        output.parent.mkdir(parents=True, exist_ok=True)
        # Do not overwrite either approved policies or previous assessments.
        with output.open("x", encoding="utf-8") as stream:
            stream.write(canonical_json(report) + "\n")
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(canonical_json({"output": str(output), "report_hash": report["report_hash"], **report["summary"]}))
    if report["summary"]["blocker_count"]:
        raise click.exceptions.Exit(5)
