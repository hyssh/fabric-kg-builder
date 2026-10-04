"""Identity and execution-fingerprint hashing for the v2 derived hierarchy.

Community identity is derived exclusively from stable, semantic inputs:
domain, provider/policy identity, the canonical sorted member entity-id
set, and structural lineage (already-computed child identities). It is
NEVER derived from display labels (the legacy ``_cluster_id`` bug — label
collisions produce colliding cluster ids across unrelated clusters) nor
from native integer cluster ids (which are not stable across runs or input
permutations).

Execution fingerprints are kept separate from cluster identity: they bind
one build attempt to its exact input snapshot, config, provider version,
and seed, for audit/debugging — not for identifying "the same community"
across runs (that is cluster identity's job).

Two distinct kinds of value are produced here and must not be confused:

* ``compute_community_identity`` mints an *id* (``"dcommunity:" + 32 hex
  chars`` — the same truncated, prefixed shape as every other id minted by
  ``model.ids.make_id``/``contracts.base.deterministic_contract_id``
  elsewhere in this repo). It is built over a canonically-encoded
  (sorted-key JSON, NFC-normalized) structured payload via
  ``contracts.base.deterministic_contract_id`` rather than a
  delimiter-joined string, so an entity/lineage id that happens to contain
  a colon, comma, or semicolon can never be crafted to collide with a
  differently-shaped input.
* ``compute_evidence_binding_hash`` and ``compute_execution_fingerprint``
  mint *content hashes* — full, unprefixed 64-hex-character SHA-256
  digests via ``contracts.base.canonical_sha256``, matching every other
  content-hash field in this repo (e.g. ``RelationshipRow.content_hash``)
  and the shape downstream consumers (W3's sealed-L4 adapter) expect for a
  "fingerprint"/"hash" field. They must never be truncated or prefixed —
  that is an id's shape, not a content hash's.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from fabric_kg_builder.contracts.base import canonical_sha256, deterministic_contract_id


def compute_community_identity(
    *,
    domain_hash: Optional[str],
    provider_id: str,
    provider_version: str,
    policy_id: str,
    member_entity_ids: Iterable[str],
    structural_lineage: Iterable[str] = (),
) -> str:
    """Compute a stable community identity string.

    ``member_entity_ids`` must be the complete, already-sorted set of
    canonical entity ids that terminate under this community (for an
    internal cluster, the union of its children's member sets). This
    function sorts defensively regardless, since callers may pass an
    unsorted iterable.

    ``structural_lineage`` is the sorted tuple of this cluster's direct
    children's *already-computed* identity strings (empty for a terminal
    cluster). Because child identity is computed first (post-order) and
    folded into the parent's hash input, identical subtrees always produce
    identical ancestor identities regardless of native-id numbering.
    """
    payload = {
        "domain_hash": domain_hash,
        "provider_id": provider_id,
        "provider_version": provider_version,
        "policy_id": policy_id,
        "member_entity_ids": sorted(member_entity_ids),
        "structural_lineage": sorted(structural_lineage),
    }
    return deterministic_contract_id("dcommunity", payload)


def compute_evidence_binding_hash(
    entity_evidence: Mapping[str, Sequence[str]],
    relationship_evidence: Mapping[str, Sequence[str]],
) -> str:
    """Hash the resolved evidence-sidecar *content* (not cluster identity).

    Evidence content is part of the output artifact (it shows up in
    ``evidence_ids`` on clusters/memberships) even though it is explicitly
    excluded from community *identity* (see module docstring). This hash
    lets ``compute_execution_fingerprint`` bind it into the per-attempt
    fingerprint: two builds over an identical graph/config/seed but with
    different evidence-sidecar content must still produce different
    fingerprints, since the artifacts they produce differ.

    Both mappings are serialized key-sorted, with each value's id sequence
    also sorted, so the hash is insensitive to iteration/list order.

    Returns a full, unprefixed 64-hex-character SHA-256 digest (a content
    hash, not an id — see module docstring). This is a breaking shape
    change from the previous ``"devidence:" + 32 hex`` id-shaped output.
    """
    payload = {
        "entity_evidence": {
            entity_id: sorted(ids) for entity_id, ids in entity_evidence.items()
        },
        "relationship_evidence": {
            rel_id: sorted(ids) for rel_id, ids in relationship_evidence.items()
        },
    }
    return canonical_sha256(payload)


def compute_execution_fingerprint(
    *,
    input_graph_hash: str,
    config_id: str,
    provider_id: str,
    provider_version: str,
    seed: int,
    policy_id: str,
    max_cluster_size: int,
    domain_hash: Optional[str] = None,
    evidence_binding_hash: Optional[str] = None,
) -> str:
    """Bind one build attempt to every outcome-affecting setting.

    Two builds are fingerprint-identical only if they share the identical
    input graph snapshot, config id, provider identity/version, seed,
    policy id, ``max_cluster_size`` (a split-trigger that directly changes
    the resulting tree shape), domain/source authority (when applicable),
    and resolved evidence-sidecar content (when supplied — see
    ``compute_evidence_binding_hash``). Any difference in any of these
    inputs produces a different fingerprint, even if the resulting
    community structure happens to look identical — this is an
    execution-attempt binding, not a structural-equality check (that is
    cluster identity's job, which never depends on evidence content).

    Returns a full, unprefixed 64-hex-character SHA-256 digest (a content
    hash, not an id — see module docstring). This is a breaking shape
    change from the previous ``"dfingerprint:" + 32 hex`` id-shaped
    output.
    """
    payload = {
        "input_graph_hash": input_graph_hash,
        "config_id": config_id,
        "provider_id": provider_id,
        "provider_version": provider_version,
        "seed": seed,
        "policy_id": policy_id,
        "max_cluster_size": max_cluster_size,
        "domain_hash": domain_hash,
        "evidence_binding_hash": evidence_binding_hash,
    }
    return canonical_sha256(payload)
