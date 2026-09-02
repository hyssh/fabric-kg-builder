"""Guard: production code must not carry one corpus's domain vocabulary.

Why this exists
---------------
``fabric-kg`` is presented as a domain-general pipeline: point it at documents,
get an ontology. Its first real corpus was Microsoft Surface field-service
documentation, and that corpus leaked into the tool itself — an entity-type
allowlist named ``SURFACE_SUPPORT_TYPES`` compiled into
``ontology/multitype_plan.py``, a ``--type-profile`` flag whose only accepted
value was ``surface-support``, and a fully populated Surface ontology committed
at ``ontology/model.yaml``, the very path a *user's own project* is supposed to
own.

None of that is a bug in the usual sense — every one of those things worked.
The defect is that the tool privileged one domain while claiming to privilege
none, and nothing would have told us if more of it accumulated.

What this guard does and does not prove
---------------------------------------
This is a **tripwire, not a proof**. It fails when known corpus-specific
markers reappear in shipped code. It cannot detect domain vocabulary we have
not thought to name, and it says nothing about whether the pipeline actually
generalizes — that question is answered by running the pipeline end to end on
an unrelated corpus, not by grepping.

Adding a marker here is cheap. If a future corpus starts leaking the same way,
add its markers rather than assuming this list is complete.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "fabric_kg_builder"

#: Structural markers of the Surface field-service corpus. Each is specific
#: enough that it cannot match the ordinary English word "surface" (which is
#: used legitimately throughout the codebase in "privacy surface",
#: "surface an error", "source_surfaces", and so on).
CORPUS_MARKERS: tuple[tuple[str, str], ...] = (
    (r"Microsoft\s+Surface", "Surface product family"),
    (r"Surface\s+(Pro|Laptop|Go|Studio|Hub|Book|Duo)\b", "Surface product name"),
    (r"SURFACE_SUPPORT", "Surface entity-type allowlist constant"),
    (r"surface[-_]support", "Surface type-profile identifier"),
    (r"surface[-_]kg\b", "Surface corpus data path"),
    (r"seattle[-_]?hub", "Seattle Hub deployment identifier"),
)

#: Files allowed to contain the markers, with the reason. Full-file exemptions
#: only, kept deliberately short — a growing list here is itself a finding.
EXEMPT: dict[str, str] = {}


def _production_sources() -> list[Path]:
    return sorted(
        p
        for p in SRC_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _violations() -> list[tuple[Path, int, str, str]]:
    found: list[tuple[Path, int, str, str]] = []
    for path in _production_sources():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in EXEMPT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            continue
        for pattern, label in CORPUS_MARKERS:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                line = text.count("\n", 0, match.start()) + 1
                found.append((path.relative_to(REPO_ROOT), line, label, match.group(0)))
    return found


@pytest.mark.unit
def test_production_code_carries_no_corpus_vocabulary() -> None:
    """No shipped module may hardcode the Surface corpus's vocabulary."""
    violations = _violations()
    assert not violations, "corpus-specific vocabulary found in production code:\n" + "\n".join(
        f"  {path}:{line}  [{label}]  {snippet!r}"
        for path, line, label, snippet in violations
    )


@pytest.mark.unit
def test_guard_can_actually_fail(tmp_path: Path) -> None:
    """The guard's own detection must work, or it is decoration.

    A guard that has never been observed failing proves nothing. This drives
    the same matching logic over a file that does contain a marker.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        'CORE_TYPES = ("Device",)  # tuned for Surface Pro 9 service docs\n',
        encoding="utf-8",
    )
    text = planted.read_text(encoding="utf-8")
    hits = [
        label
        for pattern, label in CORPUS_MARKERS
        if re.search(pattern, text, flags=re.IGNORECASE)
    ]
    assert hits, "guard failed to flag a file that plainly contains a corpus marker"


@pytest.mark.unit
def test_repo_root_ships_no_domain_ontology() -> None:
    """``ontology/model.yaml`` at the repo root belongs to a user's project.

    The tool's own repository must not ship a populated one there: runtime code
    path-searches that filename, so a committed model silently becomes the
    default ontology for anyone running from a clone.
    """
    stray = REPO_ROOT / "ontology" / "model.yaml"
    assert not stray.exists(), (
        f"{stray.relative_to(REPO_ROOT)} is a per-project artifact and must not be "
        "committed to the tool's repository. Domain examples belong under "
        "examples/domains/<name>/."
    )


@pytest.mark.unit
def test_surface_example_domain_is_intact() -> None:
    """The demoted Surface ontology must remain available as an example.

    Demoting it must not mean deleting it: it is the only worked example of a
    fully populated ontology model in the repository.
    """
    example = REPO_ROOT / "examples" / "domains" / "surface-support"
    missing = [
        name
        for name in ("model.yaml", "ids.lock.json", "type-profiles.yaml", "README.md")
        if not (example / name).is_file()
    ]
    assert not missing, f"example domain is incomplete, missing: {missing}"
