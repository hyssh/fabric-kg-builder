"""build-deploy command — end-to-end pipeline from source to deployed state."""

import click


_BUILD_DEPLOY_EPILOG = """\b
Example:
  fabric-kg build-deploy --input sample_data\\Surface_Troubleshootings --env dev
  fabric-kg build-deploy --input sample_data\\Surface_Troubleshootings --env prod --skip-search

\b
Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("build-deploy", epilog=_BUILD_DEPLOY_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--input", "input_path", required=True, type=click.Path(),
              help="Source file or directory to process through the full pipeline.")
@click.option("--env", required=True, type=click.Choice(["dev", "test", "prod"]),
              help="Target deployment environment.")
@click.option("--skip-search", is_flag=True, default=False,
              help="Skip compile-search and deploy-search stages.")
@click.option("--resume", is_flag=True, default=False,
              help="Continue enrichment from last checkpoint (pass-through to enrich).")
@click.option("--force", is_flag=True, default=False,
              help="Ignore all checkpoints and restart every stage from scratch.")
def build_deploy_cmd(
    input_path: str,
    env: str,
    skip_search: bool,
    resume: bool,
    force: bool,
) -> None:
    """Run the full pipeline end-to-end from source files to deployed state.

    Executes all stages in order:
    set-domain → inspect-source → enrich → compile-data → compile-ontology
    → compile-search → package → deploy-lakehouse → deploy-ontology
    → deploy-search → validate

    Exit codes: 0 success · 1 error · 4 partial enrichment · stage-specific otherwise.
    """
    click.echo(
        f"[build-deploy] not implemented yet "
        f"(input={input_path!r}, env={env!r}, skip_search={skip_search})"
    )
