"""Faithfulness judge (RAGAS port) against FakeLLM: scoring, skips, prompt fidelity."""

import json
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest

from rag_assistant import judge as judge_mod
from rag_assistant.judge import (
    NLI_INSTRUCTION,
    STATEMENT_INSTRUCTION,
    FaithfulnessJudge,
    JudgeError,
)
from rag_assistant.ports import LLMMessage, LLMResult
from rag_assistant.prompts import REFUSAL_TEXT
from rag_assistant.testing.fakes import FakeLLM


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/x")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(status, request=req))


class FlakyJudge:
    """Raises `fail_times` transient (or given) errors, then serves `then` texts.
    kind='cloud', model set so the judge==generator guard does not fire."""

    kind = "cloud"
    model = "judge-test"

    def __init__(self, fail_times: int, then: list[str], exc: Exception | None = None):
        self.fail_times = fail_times
        self.then = list(then)
        self.exc = exc or _http_error(503)
        self.calls = 0

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return LLMResult(text=self.then.pop(0), input_tokens=1, output_tokens=1)

    def stream(self, *a, **k) -> AsyncIterator[str]:  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Neutralize the retry backoff so tests do not wait real seconds."""

    async def _instant(_delay):
        return None

    monkeypatch.setattr(judge_mod.asyncio, "sleep", _instant)


QUESTION = "Was bedeutet NF-4102?"
ANSWER = "Der Fehlercode NF-4102 steht für ein abgelaufenes Zertifikat [S1]."
CONTEXT = (
    "<<<DOKUMENT S1 | VPN-Handbuch | Fehlerbehebung>>>\n"
    "NF-4102: Zertifikat abgelaufen. Lösung: Zertifikat erneuern.\n"
    "<<<ENDE S1>>>"
)


def _judge(*responses: str) -> tuple[FaithfulnessJudge, FakeLLM]:
    llm = FakeLLM(name="fake-judge", responses=list(responses))
    return FaithfulnessJudge(llm), llm


def _verdict(statement: str, verdict: int) -> dict:
    return {"statement": statement, "reason": "test", "verdict": verdict}


async def test_score_is_supported_over_total_claims():
    judge, _ = _judge(
        json.dumps({"statements": ["A.", "B.", "C."]}),
        json.dumps({"statements": [_verdict("A.", 1), _verdict("B.", 1), _verdict("C.", 0)]}),
    )
    result = await judge.score_answer(QUESTION, ANSWER, CONTEXT)
    assert result.score == pytest.approx(2 / 3)
    assert len(result.statements) == 3
    assert len(result.verdicts) == 3
    assert not result.skipped


async def test_prompts_use_verbatim_ragas_instructions_and_exact_context():
    """The RAGAS fidelity guarantee: both instructions go out verbatim and the
    NLI step judges against the exact context bundle the generator saw."""
    judge, llm = _judge(
        json.dumps({"statements": ["A."]}),
        json.dumps({"statements": [_verdict("A.", 1)]}),
    )
    await judge.score_answer(QUESTION, ANSWER, CONTEXT)
    assert llm.calls[0][0].content == STATEMENT_INSTRUCTION
    assert llm.calls[1][0].content == NLI_INSTRUCTION
    # Payloads travel JSON-encoded inside the prompt (newlines become \n).
    assert json.dumps(CONTEXT, ensure_ascii=False) in llm.calls[1][1].content
    assert json.dumps(QUESTION, ensure_ascii=False) in llm.calls[0][1].content


async def test_refusal_is_skipped_without_llm_calls():
    judge, llm = _judge()
    result = await judge.score_answer(QUESTION, REFUSAL_TEXT, CONTEXT)
    assert result.score is None
    assert result.skipped == "refusal"
    assert llm.calls == []


async def test_judge_never_grades_its_own_generator_model():
    judge, llm = _judge()
    # FakeLLM.model == "fake": simulate the generation chain having fallen back
    # to the judge's own model — the invariant judge != generator must skip.
    result = await judge.score_answer(QUESTION, ANSWER, CONTEXT, generator_model="fake")
    assert result.score is None
    assert result.skipped == "judge==generator"
    assert llm.calls == []


async def test_no_statements_yields_no_score():
    # RAGAS returns NaN when decomposition produces no statements; we map to None.
    judge, llm = _judge(json.dumps({"statements": []}))
    result = await judge.score_answer(QUESTION, ANSWER, CONTEXT)
    assert result.score is None
    assert result.skipped == "no_statements"
    assert len(llm.calls) == 1  # the NLI step never ran


async def test_unparsable_judge_output_is_tolerated():
    judge, _ = _judge("kaputt kein json")
    result = await judge.score_answer(QUESTION, ANSWER, CONTEXT)
    assert result.score is None
    assert result.skipped == "no_statements"


async def test_score_follows_returned_verdicts_like_ragas():
    # RAGAS computes over the verdict list the judge returns, not the input
    # statement count — mirror that exactly.
    judge, _ = _judge(
        json.dumps({"statements": ["A.", "B.", "C."]}),
        json.dumps({"statements": [_verdict("A.", 1), _verdict("B.", 0)]}),
    )
    result = await judge.score_answer(QUESTION, ANSWER, CONTEXT)
    assert result.score == pytest.approx(1 / 2)


async def test_malformed_verdict_values_count_as_unsupported():
    judge, _ = _judge(
        json.dumps({"statements": ["A.", "B."]}),
        json.dumps({"statements": [_verdict("A.", 1), {"statement": "B.", "verdict": "unsinn"}]}),
    )
    result = await judge.score_answer(QUESTION, ANSWER, CONTEXT)
    assert result.score == pytest.approx(1 / 2)


async def test_transient_error_is_retried_then_succeeds():
    provider = FlakyJudge(
        fail_times=1,  # one 503, then the statement + verdict calls succeed
        then=[
            json.dumps({"statements": ["A.", "B."]}),
            json.dumps({"statements": [_verdict("A.", 1), _verdict("B.", 1)]}),
        ],
    )
    result = await FaithfulnessJudge(provider).score_answer(QUESTION, ANSWER, CONTEXT)
    assert result.score == pytest.approx(1.0)
    assert provider.calls == 3  # 1 failed + 1 retry (statements) + 1 (verdicts)


async def test_persistent_transient_failure_raises_judge_error():
    provider = FlakyJudge(fail_times=99, then=[])
    with pytest.raises(JudgeError):
        await FaithfulnessJudge(provider).score_answer(QUESTION, ANSWER, CONTEXT)
    assert provider.calls == 3  # bounded: exactly _JUDGE_ATTEMPTS, no runaway


async def test_non_transient_error_is_not_retried():
    provider = FlakyJudge(fail_times=99, then=[], exc=_http_error(400))
    with pytest.raises(JudgeError):
        await FaithfulnessJudge(provider).score_answer(QUESTION, ANSWER, CONTEXT)
    assert provider.calls == 1  # a 400 is a bug, not a blip — fail fast


async def test_judge_error_message_redacts_leaked_key():
    req = httpx.Request("POST", "https://host/v1beta/x?key=AQ.SECRET123")
    exc = httpx.HTTPStatusError(
        "503 for url 'https://host/v1beta/x?key=AQ.SECRET123'",
        request=req,
        response=httpx.Response(503, request=req),
    )
    provider = FlakyJudge(fail_times=99, then=[], exc=exc)
    with pytest.raises(JudgeError) as ei:
        await FaithfulnessJudge(provider).score_answer(QUESTION, ANSWER, CONTEXT)
    assert "AQ.SECRET123" not in str(ei.value)
    assert "key=REDACTED" in str(ei.value)
