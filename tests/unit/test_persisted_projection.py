from types import SimpleNamespace

import pyarrow as pa

from fabric_kg_builder.semantic.persisted_projection import (
    _coerce_complex_scalar_columns,
)


def test_complex_values_are_serialized_for_scalar_ontology_properties() -> None:
    table = pa.table(
        {
            "entity_search_keys": [["AHU-1", "Air Handler"], None],
            "content": ["first", "second"],
        }
    )
    columns = [
        SimpleNamespace(column_name="entity_search_keys", data_type="string"),
        SimpleNamespace(column_name="content", data_type="string"),
    ]

    projected = _coerce_complex_scalar_columns(table, columns)

    assert projected["entity_search_keys"].to_pylist() == [
        '["AHU-1","Air Handler"]',
        None,
    ]
    assert projected["content"].to_pylist() == ["first", "second"]
    assert pa.types.is_string(projected.schema.field("entity_search_keys").type)
