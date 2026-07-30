"""Data-class routing policy — deterministic CODE, never an LLM decision.

Asymmetry that shapes everything here: wrongly-local costs quality,
wrongly-cloud is a data-protection incident. Therefore fail-closed:
- unknown collection            → CONFIDENTIAL
- unknown data class            → CONFIDENTIAL
- CONFIDENTIAL                  → local providers only, for EVERY LLM call of the
                                  request path (condense/route, grading, generation),
                                  and NO cloud fallback on local failure.

The policy binds the whole request path because the USER QUESTION itself may
contain confidential content — not only the retrieved documents.
"""

from __future__ import annotations

from .domain import CollectionCfg, DataClass

LOCAL = "local"
CLOUD = "cloud"
# An EU/EEA processor bound by a data-processing agreement, e.g. a rented
# sovereign inference endpoint. Distinct from CLOUD: the hard floor below is
# that CONFIDENTIAL never reaches CLOUD — sovereign is the only off-host kind
# a confidential request may ever use, and only outside the offline profile.
SOVEREIGN = "sovereign"

PROFILE_DEFAULT = "default"
PROFILE_OFFLINE = "offline"


def effective_data_class(collection: CollectionCfg | None) -> DataClass:
    """Resolve the data class for a request. One request = exactly one collection,
    so the class is known BEFORE the first LLM call."""
    if collection is None:
        return DataClass.CONFIDENTIAL
    return collection.data_class


def allowed_provider_kinds(
    data_class: DataClass, profile: str = PROFILE_DEFAULT
) -> tuple[str, ...]:
    """Which provider kinds may serve this request. Order is NOT preference —
    the registry picks within this set."""
    if profile == PROFILE_OFFLINE:
        return (LOCAL,)
    if data_class == DataClass.CONFIDENTIAL:
        return (LOCAL, SOVEREIGN)
    return (CLOUD, SOVEREIGN, LOCAL)


def judge_provider_kinds(data_class: DataClass, profile: str = PROFILE_DEFAULT) -> tuple[str, ...]:
    """Which provider kinds may JUDGE answers of this data class.

    Deliberately identical to allowed_provider_kinds: the judge reads the same
    answer + retrieved contexts the generator saw, so it is bound by exactly the
    same data-class gate — evaluation is not a side channel. A separate function
    (not an alias at call sites) so the judge path names its rule and cannot
    silently drift from it."""
    return allowed_provider_kinds(data_class, profile)


def assert_provider_allowed(provider_kind: str, data_class: DataClass, profile: str) -> None:
    """Hard gate used by the registry before EVERY call. Raises instead of degrading."""
    if provider_kind not in allowed_provider_kinds(data_class, profile):
        raise PermissionError(
            f"provider kind '{provider_kind}' is not allowed for data class "
            f"'{data_class.value}' (profile={profile}) — fail-closed, no cloud fallback"
        )


def ingest_department_allowed(scope_department: str, department: str) -> bool:
    """Write-side counterpart of the RLS read rule: a user may ingest documents
    only for their OWN department or 'all'. Mirrors the WITH CHECK clauses in
    db/init/02_roles_rls.sql so the API rejects with a clear 403 instead of a
    database policy error deep inside the worker."""
    return department in (scope_department, "all")
