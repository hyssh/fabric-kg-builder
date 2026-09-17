"""Explicit, versioned relationship capacity for reviewed design compilation."""

from typing import Literal

CompilerCapability = Literal["reviewed-design-64/v1"]
REVIEWED_DESIGN_64: CompilerCapability = "reviewed-design-64/v1"


def relationship_capacity(capability: CompilerCapability | None = None) -> int:
    if capability is None:
        return 24
    if capability == REVIEWED_DESIGN_64:
        return 64
    raise ValueError(f"Unknown compiler capability: {capability}")
