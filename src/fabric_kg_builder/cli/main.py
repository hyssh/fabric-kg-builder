"""fabric-kg CLI entry point.

Click group with global options. All subcommands are registered here.
Entry point: fabric-kg = fabric_kg_builder.cli:main
"""

import click

from fabric_kg_builder import __version__
from fabric_kg_builder.cli.init_cmd import init_cmd
from fabric_kg_builder.cli.set_domain_cmd import set_domain_cmd
from fabric_kg_builder.cli.inspect_cmd import inspect_source_cmd
from fabric_kg_builder.cli.enrich_cmd import enrich_cmd
from fabric_kg_builder.cli.densify_cmd import densify_cmd
from fabric_kg_builder.cli.compile_data_cmd import compile_data_cmd
from fabric_kg_builder.cli.compile_ontology_cmd import compile_ontology_cmd
from fabric_kg_builder.cli.compile_search_cmd import compile_search_cmd
from fabric_kg_builder.cli.package_cmd import package_cmd
from fabric_kg_builder.cli.deploy_cmd import (
    deploy_lakehouse_cmd,
    deploy_ontology_cmd,
    deploy_search_cmd,
)
from fabric_kg_builder.cli.validate_cmd import validate_cmd
from fabric_kg_builder.cli.build_deploy_cmd import build_deploy_cmd


_GROUP_EPILOG = """\b
Pipeline stages (run in order):
  1. set-domain       Persist a domain brief so the LLM understands your data
  2. inspect-source   Profile source files before enrichment
  3. enrich           LLM extraction → build/enriched/ (canonical JSON)
  4. densify          [RECOMMENDED] Add hub + S/C/R edges → build/enriched_dense/
  5. compile-data     Enriched JSON → 8 canonical Parquet tables (build/parquet/)
  6. compile-ontology ontology/model.yaml → Fabric Ontology definition (build/ontology/)
  7. compile-search   Parquet → AI Search schemas + doc batches (build/search/)
  8. package          Bundle all build artifacts → dist/
  9. deploy-lakehouse Upload Parquet Delta tables to Fabric OneLake
 10. deploy-ontology  Push Ontology definition (--multitype for rich typed graph)
 11. deploy-search    Push AI Search index schemas and documents
 12. validate         Run VAL + BRG gate catalog against build artifacts
 13. build-deploy     End-to-end convenience wrapper (all stages)

\b
Example (Surface sample data, Windows paths):
  fabric-kg enrich --input sample_data\\Surface_Troubleshootings
  fabric-kg densify
  fabric-kg compile-data --input build\\enriched_dense
  fabric-kg compile-ontology
  fabric-kg compile-search
  fabric-kg package
  fabric-kg deploy-lakehouse --env dev --mock
  fabric-kg deploy-ontology --env dev --multitype --no-mock

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
              type=click.Choice(["dev", "test", "prod"]),
              help="Target environment.")
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

    Transforms raw documents and CSV files into a fully deployed knowledge graph:
    Parquet tables in Fabric Lakehouse, an Ontology definition, and AI Search
    indexes for hybrid vector + keyword retrieval.

    Graph quality depends on a DOMAIN-FIT model. Start from a domain template —
    define the entity types (graph nodes) and relationships (typed edges) for your
    industry, supply 3-5 sample questions, then iterate. Run `densify` between
    `enrich` and `compile-data` for a well-connected graph; use
    `deploy-ontology --multitype` for a rich typed ontology in the Explorer.
    See README section 'Domain Template Playbook' for a full worked example.

    Run any subcommand with --help for options, defaults, and a usage example.
    """
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["env"] = env
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet
    ctx.obj["dry_run"] = dry_run


cli.add_command(init_cmd, name="init")
cli.add_command(set_domain_cmd, name="set-domain")
cli.add_command(inspect_source_cmd, name="inspect-source")
cli.add_command(enrich_cmd, name="enrich")
cli.add_command(densify_cmd, name="densify")
cli.add_command(compile_data_cmd, name="compile-data")
cli.add_command(compile_ontology_cmd, name="compile-ontology")
cli.add_command(compile_search_cmd, name="compile-search")
cli.add_command(package_cmd, name="package")
cli.add_command(deploy_lakehouse_cmd, name="deploy-lakehouse")
cli.add_command(deploy_ontology_cmd, name="deploy-ontology")
cli.add_command(deploy_search_cmd, name="deploy-search")
cli.add_command(validate_cmd, name="validate")
cli.add_command(build_deploy_cmd, name="build-deploy")


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
