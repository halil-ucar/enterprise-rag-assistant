"""LLM-as-judge faithfulness — a literal port of the RAGAS metric.

Definition source: ragas 0.4.3, ragas/metrics/_faithfulness.py (verified against
the package on PyPI, not paraphrased). Two steps, one score:

  1. Statement generation: decompose the answer into standalone claims.
  2. NLI verdict: judge each claim against the context (1 = inferable, 0 = not).
  Score = supported claims / total claims; no claims → no score (RAGAS: NaN).

Both prompt INSTRUCTIONS below are verbatim from ragas 0.4.3. The few-shot
examples keep one original RAGAS example per step and add one German example,
mirroring RAGAS's own prompt adaptation mechanism (adapt_prompts translates
examples, never the instruction) — corpus and answers here are German.

Harness-level policy on top of the metric (documented, not part of RAGAS):
  - Refusals carry no claims → skipped (N/A), never scored 0 or 1.
  - judge ≠ generator (self-preference bias): a case whose answer was generated
    by the judge's own model is skipped. Which model judges which case is
    decided by the policy gate in judge_select.py (per data class), never here.
The judge sees the exact context bundle the generator saw — not a re-retrieval.
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import openai
from pydantic import BaseModel

from .citations import is_refusal
from .pipeline import parse_json_loose
from .ports import LLMMessage, LLMProvider, LLMResult

# Transient judge failures worth retrying: rate limits + 5xx + network blips
# (both the cloud judge endpoint and a busy local Ollama surface these).
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_JUDGE_ATTEMPTS = 3
_KEY_RE = re.compile(r"key=[\w.\-]+")


class JudgeError(RuntimeError):
    """The judge provider failed even after retries. The case is reported as
    `judge_error` — NEVER silently folded into the legitimate N/A skips, so a
    transient outage can't quietly deflate (or inflate) the faithfulness mean."""


def _redact(text: str) -> str:
    """Belt-and-suspenders: strip any leaked API key from an error string
    before it is surfaced (the provider already keeps it out of the URL)."""
    return _KEY_RE.sub("key=REDACTED", text)


def _is_transient(exc: Exception) -> bool:
    # The local judge (Ollama) surfaces raw httpx errors; the cloud judge goes
    # through the OpenAI SDK, which wraps them in its own exception family.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code in _TRANSIENT_STATUS
    if isinstance(exc, openai.APIConnectionError | openai.APITimeoutError):
        return True
    return isinstance(exc, httpx.TransportError | httpx.TimeoutException)


# ── verbatim instructions from ragas 0.4.3 ────────────────────────────────────
STATEMENT_INSTRUCTION = (
    "Given a question and an answer, analyze the complexity of each sentence in the answer. "
    "Break down each sentence into one or more fully understandable statements. "
    "Ensure that no pronouns are used in any statement. Format the outputs in JSON."
)

NLI_INSTRUCTION = (
    "Your task is to judge the faithfulness of a series of statements based on a given context. "
    "For each statement you must return verdict as 1 if the statement can be directly inferred "
    "based on the context or 0 if the statement can not be directly inferred based on the context."
)

# One original RAGAS example + one German adaptation per step (see docstring).
_STATEMENT_EXAMPLES: list[tuple[dict, dict]] = [
    (
        {
            "question": "Who was Albert Einstein and what is he best known for?",
            "answer": (
                "He was a German-born theoretical physicist, widely acknowledged to be one "
                "of the greatest and most influential physicists of all time. He was best "
                "known for developing the theory of relativity, he also made important "
                "contributions to the development of the theory of quantum mechanics."
            ),
        },
        {
            "statements": [
                "Albert Einstein was a German-born theoretical physicist.",
                "Albert Einstein is recognized as one of the greatest and most influential "
                "physicists of all time.",
                "Albert Einstein was best known for developing the theory of relativity.",
                "Albert Einstein also made important contributions to the development of "
                "the theory of quantum mechanics.",
            ]
        },
    ),
    (
        {
            "question": "Wofür steht der Fehlercode NF-4102 und was ist zu tun?",
            "answer": (
                "Er steht für ein abgelaufenes VPN-Zertifikat. Betroffene müssen das "
                "Zertifikat im Self-Service-Portal erneuern."
            ),
        },
        {
            "statements": [
                "Der Fehlercode NF-4102 steht für ein abgelaufenes VPN-Zertifikat.",
                "Betroffene Benutzer müssen das VPN-Zertifikat im Self-Service-Portal erneuern.",
            ]
        },
    ),
]

_NLI_EXAMPLES: list[tuple[dict, dict]] = [
    (
        {
            "context": (
                "John is a student at XYZ University. He is pursuing a degree in Computer "
                "Science. He is enrolled in several courses this semester, including Data "
                "Structures, Algorithms, and Database Management. John is a diligent student "
                "and spends a significant amount of time studying and completing assignments. "
                "He often stays late in the library to work on his projects."
            ),
            "statements": [
                "John is majoring in Biology.",
                "John is a dedicated student.",
            ],
        },
        {
            "statements": [
                {
                    "statement": "John is majoring in Biology.",
                    "reason": (
                        "John's major is explicitly mentioned as Computer Science. There is "
                        "no information suggesting he is majoring in Biology."
                    ),
                    "verdict": 0,
                },
                {
                    "statement": "John is a dedicated student.",
                    "reason": (
                        "The context states that he spends a significant amount of time "
                        "studying and completing assignments. Additionally, it mentions that "
                        "he often stays late in the library to work on his projects, which "
                        "implies dedication."
                    ),
                    "verdict": 1,
                },
            ]
        },
    ),
    (
        {
            "context": (
                "Das VPN-Profil erlaubt maximal drei gleichzeitige Sitzungen pro "
                "Benutzerkonto. Beim Überschreiten des Limits wird die älteste Sitzung "
                "automatisch beendet."
            ),
            "statements": [
                "Ein Benutzerkonto darf höchstens drei gleichzeitige VPN-Sitzungen haben.",
                "Beim Überschreiten des Limits wird das Benutzerkonto gesperrt.",
            ],
        },
        {
            "statements": [
                {
                    "statement": (
                        "Ein Benutzerkonto darf höchstens drei gleichzeitige VPN-Sitzungen haben."
                    ),
                    "reason": (
                        "Der Kontext nennt explizit ein Maximum von drei gleichzeitigen "
                        "Sitzungen pro Benutzerkonto."
                    ),
                    "verdict": 1,
                },
                {
                    "statement": "Beim Überschreiten des Limits wird das Benutzerkonto gesperrt.",
                    "reason": (
                        "Laut Kontext wird die älteste Sitzung beendet; von einer "
                        "Kontosperrung steht dort nichts."
                    ),
                    "verdict": 0,
                },
            ]
        },
    ),
]


def _render(instruction: str, examples: list[tuple[dict, dict]], payload: dict) -> list[LLMMessage]:
    """Instruction + few-shot input/output JSON pairs + the actual input —
    the same shape ragas' PydanticPrompt renders."""
    blocks = ["Examples:"]
    for inp, out in examples:
        blocks.append(f"Input: {json.dumps(inp, ensure_ascii=False)}")
        blocks.append(f"Output: {json.dumps(out, ensure_ascii=False)}")
    blocks.append("Your actual task:")
    blocks.append(f"Input: {json.dumps(payload, ensure_ascii=False)}")
    blocks.append("Output:")
    return [
        LLMMessage(role="system", content=instruction),
        LLMMessage(role="user", content="\n\n".join(blocks)),
    ]


class StatementVerdict(BaseModel):
    statement: str
    reason: str = ""
    verdict: int  # 1 = inferable from context, 0 = not (RAGAS NLI verdict)


class FaithfulnessResult(BaseModel):
    """score is None when the case was not scored; `skipped` says why
    (refusal / judge==generator / no_statements / no_verdicts — see the module
    docstring). A judge OUTAGE never lands here: it raises JudgeError, which the
    caller records as `judge_error` so it stays distinct from legitimate N/A."""

    score: float | None = None
    statements: list[str] = []
    verdicts: list[StatementVerdict] = []
    skipped: str = ""


class FaithfulnessJudge:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def score_answer(
        self, question: str, answer: str, context: str, *, generator_model: str = ""
    ) -> FaithfulnessResult:
        if is_refusal(answer):
            return FaithfulnessResult(skipped="refusal")
        if generator_model and generator_model == self.provider.model:
            return FaithfulnessResult(skipped="judge==generator")

        statements = await self._statements(question, answer)
        if not statements:
            return FaithfulnessResult(skipped="no_statements")

        verdicts = await self._verdicts(context, statements)
        if not verdicts:
            return FaithfulnessResult(statements=statements, skipped="no_verdicts")

        # RAGAS _compute_score: supported / total, over the returned verdicts.
        supported = sum(1 for v in verdicts if v.verdict == 1)
        return FaithfulnessResult(
            score=supported / len(verdicts), statements=statements, verdicts=verdicts
        )

    async def _complete(self, messages: list[LLMMessage], max_tokens: int) -> LLMResult:
        """One judge call with bounded transient retry. The production path gets
        resilience from the registry's circuit breaker + fallback chain; the judge
        talks to a raw provider, so it retries here and gives up as JudgeError."""
        delay = 2.0
        last: Exception | None = None
        for attempt in range(_JUDGE_ATTEMPTS):
            try:
                return await self.provider.complete(messages, max_tokens=max_tokens, json_mode=True)
            except Exception as exc:  # noqa: BLE001 — bounded retry, then re-raised as JudgeError
                last = exc
                if not _is_transient(exc) or attempt == _JUDGE_ATTEMPTS - 1:
                    break
                await asyncio.sleep(delay)
                delay *= 2
        raise JudgeError(_redact(str(last))) from last

    async def _statements(self, question: str, answer: str) -> list[str]:
        result = await self._complete(
            _render(
                STATEMENT_INSTRUCTION, _STATEMENT_EXAMPLES, {"question": question, "answer": answer}
            ),
            max_tokens=1024,
        )
        data = parse_json_loose(result.text)
        raw = data.get("statements", [])
        return (
            [s.strip() for s in raw if isinstance(s, str) and s.strip()]
            if isinstance(raw, list)
            else []
        )

    async def _verdicts(self, context: str, statements: list[str]) -> list[StatementVerdict]:
        result = await self._complete(
            _render(NLI_INSTRUCTION, _NLI_EXAMPLES, {"context": context, "statements": statements}),
            max_tokens=2048,
        )
        data = parse_json_loose(result.text)
        raw = data.get("statements", [])
        if not isinstance(raw, list):
            return []
        out: list[StatementVerdict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                verdict = 1 if int(item.get("verdict", 0)) == 1 else 0
            except (TypeError, ValueError):
                verdict = 0
            out.append(
                StatementVerdict(
                    statement=str(item.get("statement", "")),
                    reason=str(item.get("reason", "")),
                    verdict=verdict,
                )
            )
        return out
