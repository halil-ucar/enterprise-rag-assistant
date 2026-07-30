import pytest

from rag_assistant.domain import CollectionCfg, DataClass
from rag_assistant.policy import (
    CLOUD,
    LOCAL,
    PROFILE_DEFAULT,
    PROFILE_OFFLINE,
    SOVEREIGN,
    allowed_provider_kinds,
    assert_provider_allowed,
    effective_data_class,
    ingest_department_allowed,
    judge_provider_kinds,
)


def _col(dc: DataClass) -> CollectionCfg:
    return CollectionCfg(name="x", tenant="t", data_class=dc)


def test_unknown_collection_is_confidential_fail_closed():
    assert effective_data_class(None) == DataClass.CONFIDENTIAL


def test_confidential_never_includes_cloud():
    """The hard floor: confidential may use local or a sovereign (EU-hosted,
    contractual no-training terms) endpoint — but never the CLOUD kind, in
    any profile."""
    kinds = allowed_provider_kinds(DataClass.CONFIDENTIAL, PROFILE_DEFAULT)
    assert CLOUD not in kinds
    assert LOCAL in kinds and SOVEREIGN in kinds


def test_internal_allows_cloud_sovereign_and_local():
    kinds = allowed_provider_kinds(DataClass.INTERNAL, PROFILE_DEFAULT)
    assert CLOUD in kinds and SOVEREIGN in kinds and LOCAL in kinds


def test_offline_profile_forces_local_for_everything():
    # offline is stricter than sovereign: nothing leaves the host at all
    for dc in DataClass:
        assert allowed_provider_kinds(dc, PROFILE_OFFLINE) == (LOCAL,)


def test_judge_matrix_is_identical_to_generation_matrix():
    """E1: evaluation is not a side channel — the judge is bound by exactly
    the same matrix as generation, for every class and profile."""
    for profile in (PROFILE_DEFAULT, PROFILE_OFFLINE):
        for dc in DataClass:
            assert judge_provider_kinds(dc, profile) == allowed_provider_kinds(dc, profile)


def test_no_cloud_fallback_for_confidential_raises():
    # fail-closed also under failure: the gate raises instead of degrading to cloud
    with pytest.raises(PermissionError):
        assert_provider_allowed(CLOUD, DataClass.CONFIDENTIAL, PROFILE_DEFAULT)


def test_gate_passes_for_allowed_combination():
    assert_provider_allowed(CLOUD, DataClass.INTERNAL, PROFILE_DEFAULT)
    assert_provider_allowed(LOCAL, DataClass.CONFIDENTIAL, PROFILE_DEFAULT)


def test_collection_data_class_is_used():
    assert effective_data_class(_col(DataClass.PUBLIC)) == DataClass.PUBLIC


def test_ingest_department_write_scope():
    # own department or 'all' are allowed; a foreign department is not
    assert ingest_department_allowed("it", "it")
    assert ingest_department_allowed("it", "all")
    assert not ingest_department_allowed("it", "hr")
    assert not ingest_department_allowed("hr", "it")
