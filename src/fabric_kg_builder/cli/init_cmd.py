"""init command — scaffold a new fabric-kg-builder project."""

import click


_INIT_EPILOG = """\b
Example:
  fabric-kg init
  fabric-kg init --template csv-only

Questions? https://github.com/hyssh/fabric-kg-builder/issues
"""


@click.command("init", epilog=_INIT_EPILOG,
               context_settings={"max_content_width": 120})
@click.option("--template", default="default", show_default=True,
              type=click.Choice(["default", "csv-only"]),
              help="Project template to scaffold: 'default' includes all source types; "
                   "'csv-only' skips PDF/DOCX config.")
def init_cmd(template: str) -> None:
    """Scaffold a new project with config, ontology model, and directory structure.

    Creates fabric-kg.yaml, ontology/model.yaml, ontology/ids.lock.json,
    ontology/environments/{dev,test,prod}.json, and an empty build/ directory.

    Exit codes: 0 success · 1 error · 2 already initialized (no-op).
    """
    click.echo(f"[init] not implemented yet (template={template})")
