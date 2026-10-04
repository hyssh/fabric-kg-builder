"""Privacy surface gate for CI and packaging checks."""
from .tracked_surface_gate import (
    FORBIDDEN_MARKERS,
    SELF_EXEMPT_FILES,
    PrivacyGateError,
    Violation,
    find_forbidden_in_text,
    run_full_gate,
    scan_file,
    tracked_surface_files,
)

__all__ = [
    "FORBIDDEN_MARKERS",
    "SELF_EXEMPT_FILES",
    "PrivacyGateError",
    "Violation",
    "find_forbidden_in_text",
    "run_full_gate",
    "scan_file",
    "tracked_surface_files",
]
