from rag_assistant.cachekey import build_answer_cache_key, normalize_question
from rag_assistant.domain import DataClass, QueryScope

ANNA_IT = QueryScope(tenant="nordfels", user_id="anna", department="it")
BEN_HR = QueryScope(tenant="nordfels", user_id="ben", department="hr")
CARL_IT = QueryScope(tenant="nordfels", user_id="carl", department="it")


def key(scope, dc=DataClass.INTERNAL, q="Wie beantrage ich VPN-Zugang?", corpus=1, emb=1):
    return build_answer_cache_key(scope, dc, q, corpus, emb)


def test_different_department_never_shares_cache():
    # THE bug this design prevents: the cache sits in front of RLS.
    assert key(ANNA_IT) != key(BEN_HR)


def test_same_department_shares_cache():
    # authorized sharing is fine (both users could get the answer from the DB anyway)
    assert key(ANNA_IT) == key(CARL_IT)


def test_corpus_version_invalidates():
    assert key(ANNA_IT, corpus=1) != key(ANNA_IT, corpus=2)


def test_embedding_version_invalidates():
    assert key(ANNA_IT, emb=1) != key(ANNA_IT, emb=2)


def test_data_class_separates():
    assert key(ANNA_IT, dc=DataClass.INTERNAL) != key(ANNA_IT, dc=DataClass.CONFIDENTIAL)


def test_normalization_collapses_whitespace_and_case():
    a = key(ANNA_IT, q="Wie   beantrage ich VPN-Zugang? ")
    b = key(ANNA_IT, q="wie beantrage ich vpn-zugang?")
    assert a == b
    assert normalize_question("  A   B ") == "a b"


def test_key_carries_no_plaintext_question():
    k = key(ANNA_IT)
    assert "vpn" not in k.lower()
    assert k.startswith("answer:nordfels:")
