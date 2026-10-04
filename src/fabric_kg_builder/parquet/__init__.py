"""Parquet I/O for the 8 canonical tables.

Uses PyArrow for direct schema control and zero-copy reads.
The PyArrow schema is the authoritative data contract — all writers
must conform to the schema defined in parquet.schemas.
"""
