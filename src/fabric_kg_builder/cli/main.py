"""fabric-kg CLI entry point.

Click group with global options. All subcommands are registered here.
Entry point: fabric-kg = fabric_kg_builder.cli:main
"""

import click

from fabric_kg_builder import __version__
from fabric_kg_builder.cli.domain_cmd import domain_cmd
from fabric_kg_builder.cli.init_cmd import init_cmd
from fabric_kg_builder.cli.set_domain_cmd import set_domain_cmd
from fabric_kg_builder.cli.inspect_cmd import inspect_source_cmd
from fabric_kg_builder.cli.semantic_cmd import (
    compile_agent_cmd,
    compile_graph_cmd,
    compile_semantic_cmd,
    inspect_ontology_cmd,
    validate_artifacts_cmd,
)
from fabric_kg_builder.cli.enrich_cmd import enrich_cmd
from fabric_kg_builder.cli.densify_cmd import densify_cmd
from fabric_kg_builder.cli.compile_data_cmd import compile_data_cmd
from fabric_kg_builder.cli.compile_ontology_cmd import compile_ontology_cmd
from fabric_kg_builder.cli.compile_search_cmd import compile_search_cmd
from fabric_kg_builder.cli.package_cmd import package_cmd
from fabric_kg_builder.cli.deploy_cmd import (
    deploy_lakehouse_cmd,
    deploy_graph_cmd,
    deploy_data_agent_cmd,
    deploy_ontology_cmd,
    deploy_search_cmd,
    deploy_serving_cmd,
    validate_projection_cmd,
)
from fabric_kg_builder.cli.validate_cmd import validate_cmd
from fabric_kg_builder.cli.build_deploy_cmd import build_deploy_cmd
from fabric_kg_builder.cli.lineage_cmd import assets_cmd, lineage_cmd, trace_cmd
from fabric_kg_builder.cli.infra_cmd import infra_cmd
from fabric_kg_builder.cli.knowledge_cmd import knowledge_group
from fabric_kg_builder.cli.app_cmd import app_cmd
from fabric_kg_builder.cli.runtime_cmd import (
    collect_evidence_cmd,
    evaluate_cmd,
    report_cmd,
    validate_deployment_cmd,
)
from fabric_kg_builder.cli.diagnostics_cmd import inspect_diagnostics_cmd
from fabric_kg_builder.cli.init_domain_cmd import init_domain_cmd


_GROUP_EPILOG = """\b
Recommended production pipeline (run in dependency order):
  1. Author and approve
     init -> init-domain --input <source-path> -> domain review -> domain approve -> inspect-ontology
  2. Extract and compile
     enrich -> [densify] -> compile-data -> compile-semantic
     -> compile-ontology + compile-graph + compile-agent + compile-search
     -> validate-artifacts -> package -> validate
  3. Deploy structured semantic surfaces
     deploy-lakehouse -> deploy-ontology -> deploy-graph
     -> validate-projection
  4. Deploy retrieval efficiently
     Large runs (recommended): deploy-search --integrated-vectorization
       Stages JSON in Blob and lets an Azure AI Search indexer create vectors
       and index documents. The indexer may continue after the CLI wait limit;
       inspect its Azure Search status instead of restarting a healthy run.
     Small/test runs only: deploy-search without --integrated-vectorization
       Directly uploads local docs.json batches and can be slower or time out.
  5. Build Foundry IQ retrieval
     knowledge source upsert -> knowledge base upsert -> knowledge probe
  6. Publish agents in strict order
     deploy-data-agent -> app deploy-agent
     A Fabric Data Agent requires a valid persisted projection receipt.
     Deploy the Foundry prompt agent only after the new Data Agent exists and
     agent-metadata.yaml points to that Data Agent connection.
     Author Data Agent grounding before deployment:
       - semantic entity/relationship/property descriptions define sources
       - compile-agent creates shared instructions and semantic context
       - competency questions and physical GQL probes become validated examples
       - replace all {{ENTITY_NAME}}, {{RELATIONSHIP_NAME}}, and
         {{PROPERTY_NAME}} placeholders with exact case-sensitive contract names
  7. Accept and report
     collect-evidence -> evaluate -> validate-deployment -> report

\b
Guidance for Copilot and other AI agents:
  Prefer the ordered workflow above. For large document sets, choose Blob +
  indexer ingestion with --integrated-vectorization; do not default to repeated
  direct Search uploads. Never invent or bypass projection receipts, deploy a
  Foundry agent before its Fabric Data Agent dependency, or treat a CLI polling
  timeout as an indexer failure without checking indexer status.

\b
PowerShell example (large document set):
  fabric-kg init --target .\\my-kg-project
  fabric-kg domain review --file .\\my-kg-project\\domain.yaml
  fabric-kg domain approve --file .\\my-kg-project\\domain.yaml
  fabric-kg enrich --input .\\my-kg-project\\source-assets --recursive
  fabric-kg compile-data --input build\\enriched --validate
  fabric-kg compile-semantic --data-dir build\\parquet
  fabric-kg compile-ontology --semantic-dir build\\semantic --env dev
  fabric-kg compile-graph --semantic-dir build\\semantic
  fabric-kg compile-agent --semantic-dir build\\semantic --domain-contract domain.yaml
  fabric-kg compile-search --input build\\parquet
  fabric-kg validate-artifacts --build-dir build --require-search
  fabric-kg deploy-lakehouse --env dev --parquet-dir build\\parquet --no-mock
  fabric-kg deploy-ontology --env dev --semantic-dir build\\semantic --parquet-dir build\\parquet --no-mock
  fabric-kg deploy-graph --env dev --parquet-dir build\\parquet --graph-preview-acknowledged
  fabric-kg validate-projection --semantic-dir build\\semantic --ontology-receipt build\\release\\ontology-receipt.json --serving-receipt build\\release\\serving-receipt.json --out build\\release\\persisted-projection-receipt.json
  fabric-kg deploy-search --env dev --indexes kg-chunks --integrated-vectorization --no-mock
  fabric-kg knowledge source upsert --name my-kg-source --index-name my-kg-chunks --endpoint $env:AZURE_SEARCH_ENDPOINT
  fabric-kg knowledge base upsert --name my-kg-kb --sources my-kg-source --endpoint $env:AZURE_SEARCH_ENDPOINT
  fabric-kg knowledge probe --kb my-kg-kb --query "What equipment needs maintenance?" --endpoint $env:AZURE_SEARCH_ENDPOINT
  fabric-kg deploy-data-agent --env dev --mode create --semantic-dir build\\semantic --projection-receipt build\\release\\persisted-projection-receipt.json --agent-dir build\\agents
  fabric-kg app deploy-agent --env dev

\b
Run `fabric-kg <command> --help` before execution. The command help documents
required configuration, receipts, dependencies, and safe examples.

\b
Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.group(
    epilog=_GROUP_EPILOG,
    context_settings={"max_content_width": 120, "help_option_names": ["-h", "--help"]},
)
@click.version_option(version=__version__, prog_name="fabric-kg")
@click.option("--config", default="./fabric-kg.yaml", show_default=True,
              type=click.Path(), help="Path to fabric-kg.yaml config file.")
@click.option("--env", default="dev", show_default=True,
              type=str,
              help="Target environment name.")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Enable DEBUG logging.")
@click.option("--quiet", "-q", is_flag=True, default=False,
              help="Suppress output; show ERROR-level logs only.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show plan without executing any changes.")
@click.pass_context
def cli(
    ctx: click.Context,
    config: str,
    env: str,
    verbose: bool,
    quiet: bool,
    dry_run: bool,
) -> None:
    """fabric-kg-builder: build and deploy knowledge graphs to Microsoft Fabric.

    Transforms heterogeneous domain assets into traceable Search, Lakehouse,
    Graph, and Ontology artifacts plus a deployable agent experience.

    Graph quality depends on an approved domain contract. Capture the business
    context, problem, entity and relationship concepts, constraints, and
    competency questions before enrichment. Optional densification is driven by
    explicit domain configuration; no sample taxonomy is applied implicitly.

    Run any subcommand with --help for options, defaults, and a usage example.

    For large Search deployments, prefer Blob staging and Azure AI Search
    indexers via ``deploy-search --integrated-vectorization``. Publish a Fabric
    Data Agent only after persisted Ontology/Graph validation, then deploy the
    Foundry prompt agent that depends on it.
    """
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["env"] = env
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet
    ctx.obj["dry_run"] = dry_run


cli.add_command(init_cmd, name="init")
cli.add_command(domain_cmd, name="domain")
cli.add_command(set_domain_cmd, name="set-domain")
cli.add_command(inspect_source_cmd, name="inspect-source")
cli.add_command(inspect_ontology_cmd, name="inspect-ontology")
cli.add_command(compile_semantic_cmd, name="compile-semantic")
cli.add_command(enrich_cmd, name="enrich")
cli.add_command(densify_cmd, name="densify")
cli.add_command(compile_data_cmd, name="compile-data")
cli.add_command(compile_ontology_cmd, name="compile-ontology")
cli.add_command(compile_graph_cmd, name="compile-graph")
cli.add_command(compile_agent_cmd, name="compile-agent")
cli.add_command(validate_artifacts_cmd, name="validate-artifacts")
cli.add_command(compile_search_cmd, name="compile-search")
cli.add_command(package_cmd, name="package")
cli.add_command(deploy_lakehouse_cmd, name="deploy-lakehouse")
cli.add_command(deploy_graph_cmd, name="deploy-graph")
cli.add_command(deploy_data_agent_cmd, name="deploy-data-agent")
cli.add_command(deploy_ontology_cmd, name="deploy-ontology")
cli.add_command(deploy_search_cmd, name="deploy-search")
cli.add_command(deploy_serving_cmd, name="deploy-serving")  # M6 SRV-011
cli.add_command(validate_projection_cmd, name="validate-projection")
cli.add_command(validate_cmd, name="validate")
cli.add_command(build_deploy_cmd, name="build-deploy")
cli.add_command(assets_cmd, name="assets")
cli.add_command(lineage_cmd, name="lineage")
# Deprecated compatibility alias; use `fabric-kg lineage trace`.
cli.add_command(trace_cmd, name="trace")
cli.add_command(infra_cmd, name="infra")
cli.add_command(knowledge_group, name="knowledge")
cli.add_command(app_cmd, name="app")  # M8: Foundry agent + reference app
cli.add_command(validate_deployment_cmd, name="validate-deployment")
cli.add_command(collect_evidence_cmd, name="collect-evidence")
cli.add_command(evaluate_cmd, name="evaluate")
cli.add_command(report_cmd, name="report")
cli.add_command(inspect_diagnostics_cmd, name="inspect-diagnostics")
cli.add_command(init_domain_cmd, name="init-domain")


def _configure_utf8_console() -> None:
    """Reconfigure stdout/stderr to UTF-8 on Windows (cp1252 is default there).

    Characters like → (U+2192) that appear in log summaries and graph-path
    echo strings are not encodable in cp1252.  Without this call the CLI would
    crash with UnicodeEncodeError, caught by the per-file try/except in
    enrich_cmd, producing exit 4 with entities=0.

    We use ``errors='replace'`` as a safety net: any remaining unencodable
    characters become '?' rather than raising.  The guard for ``hasattr``
    keeps this compatible with environments where stdout has been replaced with
    a non-standard object (e.g. pytest's capture streams).
    """
    import sys

    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass  # best-effort — never crash the CLI over this


def main() -> None:
    """Console script entry point: fabric-kg."""
    _configure_utf8_console()
    cli(auto_envvar_prefix="FABRIC_KG")
