"""Tests for the IC memo generator.

The Claude prompt chain is exercised with a fake client that records each request,
so the tests check the exact request shape (model, fallbacks, structured output,
cache placement) without spending API credits.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from src.analyzer import AnalysisResult
from src.memo_generator import (
    DEFAULT_MODEL,
    FALLBACK_BETA,
    PLACEHOLDERS,
    SECTIONS,
    MemoGenerationError,
    Risk,
    RiskAssessment,
    generate_memo,
    render_tables,
    template_body,
    validate_memo_body,
)

USAGE = SimpleNamespace(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0,
                        cache_read_input_tokens=900)
ASSESSMENT = RiskAssessment(
    deal_thesis=["Exceptional cash generation."],
    key_risks=[Risk(title="Entry price", category="financial", severity="high",
                    description="d", quantification="q", mitigant="m")],
    downside_commentary="c", covenant_assessment="c", recommendation_rationale="r")


class FakeMessages:
    """Records requests; returns canned step-1 and step-2 responses."""

    def __init__(self, body: str, parse_error: Exception | None = None,
                 stop_reason: str = "end_turn") -> None:
        self.body, self.parse_error, self.stop_reason = body, parse_error, stop_reason
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(("parse", kwargs))
        if self.parse_error:
            raise self.parse_error
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=ASSESSMENT,
                               usage=USAGE, stop_details=None)

    @contextmanager
    def stream(self, **kwargs: Any) -> Any:
        self.calls.append(("stream", kwargs))
        final = SimpleNamespace(stop_reason="end_turn", usage=USAGE,
                                content=[SimpleNamespace(type="text", text=self.body)])
        yield SimpleNamespace(get_final_message=lambda: final)


def fake_client(messages: FakeMessages) -> Any:
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


# --------------------------------------------------------------------------------------
# Template memo
# --------------------------------------------------------------------------------------
def test_template_memo_is_complete(analysis: AnalysisResult) -> None:
    memo = generate_memo(analysis, offline=True)
    assert memo.source == "template"
    assert "{{" not in memo.markdown
    for section in SECTIONS:
        assert section in memo.markdown
    assert "**Recommendation: PASS**" in memo.markdown
    assert "## Appendix: Key Assumptions" in memo.markdown


def test_template_body_passes_validation(analysis: AnalysisResult) -> None:
    assert validate_memo_body(template_body(analysis), "PASS") == []


def test_tables_contain_engine_figures(analysis: AnalysisResult) -> None:
    tables = render_tables(analysis)
    assert set(tables) == set(PLACEHOLDERS)
    lbo = analysis.base.model.lbo
    assert f"{lbo.moic:.2f}x" in tables["{{TABLE_SCENARIOS}}"]
    assert "**$540.0B**" not in tables["{{TABLE_SOURCES_USES}}"]  # only totals are bold
    assert "$540.0B" in tables["{{TABLE_SOURCES_USES}}"]


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda b: b.replace(SECTIONS[3], "## 4. Risks"), "missing section heading"),
        (lambda b: b.replace("{{TABLE_LBO_IRR}}", "{{TABLE_LBO_IRR}}\n{{TABLE_LBO_IRR}}"),
         "appears 2 times"),
        (lambda b: b.replace("**Recommendation: PASS**", "**Recommendation: PROCEED**"),
         "contradicting"),
        (lambda b: b.replace("{{TABLE_COVENANTS}}", "").replace(
            SECTIONS[1], SECTIONS[1] + "\n{{TABLE_COVENANTS}}"), "outside"),
    ],
)
def test_validation_catches_bad_memos(analysis: AnalysisResult, mutate: Any,
                                      problem: str) -> None:
    problems = validate_memo_body(mutate(template_body(analysis)), "PASS")
    assert any(problem in p for p in problems), problems


# --------------------------------------------------------------------------------------
# Claude prompt chain (fake client)
# --------------------------------------------------------------------------------------
def test_prompt_chain_request_shape(analysis: AnalysisResult) -> None:
    messages = FakeMessages(template_body(analysis))
    memo = generate_memo(analysis, client=fake_client(messages))

    assert memo.source == DEFAULT_MODEL
    assert memo.risk_assessment == ASSESSMENT
    assert "{{" not in memo.markdown
    assert memo.usage["memo"]["cache_read_input_tokens"] == 900

    (kind1, step1), (kind2, step2) = messages.calls
    assert (kind1, kind2) == ("parse", "stream")
    for req in (step1, step2):
        assert req["model"] == "claude-opus-5-5"
        assert req["fallbacks"] == "default" and req["betas"] == [FALLBACK_BETA]
        assert req["output_config"] == {"effort": "high"}
        assert "thinking" not in req and "temperature" not in req
    assert step1["output_format"] is RiskAssessment
    # Both steps share an identical system prefix, cached after the deal data block.
    assert step1["system"] == step2["system"]
    assert step1["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "<deal_data>" in step1["system"][-1]["text"]
    # Step 2 receives the step-1 assessment and the required verdict.
    prompt = step2["messages"][0]["content"]
    assert "<risk_assessment>" in prompt and "**Recommendation: PASS**" in prompt


def test_contradicting_verdict_is_rejected(analysis: AnalysisResult) -> None:
    body = template_body(analysis).replace("**Recommendation: PASS**",
                                           "**Recommendation: PROCEED**")
    with pytest.raises(MemoGenerationError, match="contradicting"):
        generate_memo(analysis, client=fake_client(FakeMessages(body)))


def test_refusal_raises(analysis: AnalysisResult) -> None:
    messages = FakeMessages(template_body(analysis), stop_reason="refusal")
    with pytest.raises(MemoGenerationError, match="declined"):
        generate_memo(analysis, client=fake_client(messages))


def test_missing_credentials_fall_back_to_template(analysis: AnalysisResult) -> None:
    error = TypeError("Could not resolve authentication method. Expected one of api_key...")
    memo = generate_memo(analysis, client=fake_client(FakeMessages("", parse_error=error)))
    assert memo.source == "template"


def test_unrelated_type_errors_propagate(analysis: AnalysisResult) -> None:
    error = TypeError("unexpected keyword argument")
    with pytest.raises(TypeError, match="unexpected keyword"):
        generate_memo(analysis, client=fake_client(FakeMessages("", parse_error=error)))
