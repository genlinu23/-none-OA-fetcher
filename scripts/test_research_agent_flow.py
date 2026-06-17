from __future__ import annotations

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ligen_downloader.web_app as web_app


def assert_unavailable(agent: dict[str, object], *, error_has: str) -> None:
    if agent.get("model") != "llm-unavailable":
        raise AssertionError(f"expected llm-unavailable, got {agent.get('model')!r}")
    if bool(agent.get("include_in_brief")):
        raise AssertionError(f"unavailable agent must not update brief: {agent!r}")
    if agent.get("normalized_requirement"):
        raise AssertionError(f"unavailable agent leaked normalized requirement: {agent!r}")
    if error_has not in str(agent.get("model_error") or ""):
        raise AssertionError(f"missing error marker {error_has!r}: {agent!r}")


def test_no_api_key_does_not_fallback() -> None:
    old_key = os.environ.pop("OPENAI_API_KEY", None)
    try:
        agent = web_app._build_research_agent_reply(
            message="papers about transoer architecture in computer vision",
            history=[],
            current_brief="",
        )
    finally:
        if old_key is not None:
            os.environ["OPENAI_API_KEY"] = old_key
    assert_unavailable(agent, error_has="OPENAI_API_KEY")


def test_llm_error_does_not_fallback() -> None:
    old_key = os.environ.get("OPENAI_API_KEY")
    old_call = web_app._call_research_agent_llm
    os.environ["OPENAI_API_KEY"] = "test-key"

    def raise_model_error(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("simulated model outage")

    web_app._call_research_agent_llm = raise_model_error  # type: ignore[assignment]
    try:
        agent = web_app._build_research_agent_reply(
            message="你好，你是谁",
            history=[],
            current_brief="Transformer architecture",
        )
    finally:
        web_app._call_research_agent_llm = old_call
        if old_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = old_key

    assert_unavailable(agent, error_has="simulated model outage")


def main() -> None:
    test_no_api_key_does_not_fallback()
    test_llm_error_does_not_fallback()
    print("research agent no-fallback failure contract test passed")


if __name__ == "__main__":
    main()
