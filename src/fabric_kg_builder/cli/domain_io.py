"""Domain input errors for public CLI commands, without dumping model payloads."""

import click
from pydantic import ValidationError

from fabric_kg_builder.domain.service import DomainContractError, load_domain_contract


def load_cli_domain_contract(path):
    try:
        return load_domain_contract(path)
    except DomainContractError as exc:
        if isinstance(exc.__cause__, ValidationError):
            detail = "; ".join(
                f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                for item in exc.__cause__.errors(include_input=False, include_url=False)[:20]
            )
        else:
            detail = str(exc)
        raise click.ClickException(f"Invalid domain contract: {detail}") from exc
