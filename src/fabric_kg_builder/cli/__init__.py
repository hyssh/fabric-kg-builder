"""CLI package for fabric-kg-builder.

Exposes the Click group and main() entry point so the console script
`fabric-kg = fabric_kg_builder.cli:main` resolves correctly.
"""

from fabric_kg_builder.cli.main import cli, main

__all__ = ["cli", "main"]
