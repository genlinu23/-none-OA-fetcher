from __future__ import annotations

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ligen_downloader.web_app import _build_research_agent_reply


CASES = [
    {
        "name": "casual identity question",
        "message": "你是谁，可以干嘛",
        "brief": "",
        "expect_include": False,
        "expect_empty_normalized": True,
    },
    {
        "name": "transformer typo topic",
        "message": "我想看 transoer 的论文，计算机方向",
        "brief": "",
        "expect_include": True,
        "normalized_has": ["Transformer"],
    },
    {
        "name": "polyurethane coating topic",
        "message": "polyurethane waterproof latent curing coating recent papers",
        "brief": "",
        "expect_include": True,
        "normalized_has": ["polyurethane"],
    },
    {
        "name": "keyword request with current brief",
        "message": "给我生成关键词吧",
        "brief": "Transformer architecture computer vision survey",
        "expect_include": False,
        "expect_empty_normalized": True,
        "reply_has_any": ["Transformer", "关键词", "keyword"],
    },
    {
        "name": "vague acknowledgement with current brief",
        "message": "好的",
        "brief": "Computer science hot research fields: artificial intelligence, machine learning, Transformer",
        "expect_include": False,
        "expect_empty_normalized": True,
        "reply_has_none": ["已确认"],
    },
    {
        "name": "topic switch",
        "message": "算了，换成 transformer 计算机视觉论文",
        "brief": "polyurethane waterproof latent curing coating",
        "expect_include": True,
        "normalized_has": ["Transformer"],
    },
]


def assert_case(case: dict[str, object], agent: dict[str, object]) -> None:
    name = str(case["name"])
    if agent.get("model") == "llm-unavailable":
        raise AssertionError(f"{name}: real provider unavailable: {agent!r}")
    include = bool(agent.get("include_in_brief"))
    if include != bool(case["expect_include"]):
        raise AssertionError(f"{name}: include mismatch: {agent!r}")
    normalized = str(agent.get("normalized_requirement") or "")
    if case.get("expect_empty_normalized") and normalized:
        raise AssertionError(f"{name}: normalized should be empty: {agent!r}")
    for expected in case.get("normalized_has", []):  # type: ignore[union-attr]
        if str(expected) not in normalized:
            raise AssertionError(f"{name}: normalized missing {expected!r}: {agent!r}")
    reply = str(agent.get("reply") or "")
    reply_has_any = [str(item) for item in case.get("reply_has_any", [])]  # type: ignore[union-attr]
    if reply_has_any and not any(item in reply for item in reply_has_any):
        raise AssertionError(f"{name}: reply missing any of {reply_has_any!r}: {agent!r}")
    reply_has_none = [str(item) for item in case.get("reply_has_none", [])]  # type: ignore[union-attr]
    if reply_has_none and any(item in reply for item in reply_has_none):
        raise AssertionError(f"{name}: reply should not contain any of {reply_has_none!r}: {agent!r}")


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise SystemExit("OPENAI_API_KEY is not set; real provider batch test cannot run.")
    print(f"real provider model={os.environ.get('LIGEN_AGENT_MODEL') or os.environ.get('OPENAI_MODEL') or 'default'}")
    for index, case in enumerate(CASES, start=1):
        agent = _build_research_agent_reply(
            message=str(case["message"]),
            history=[],
            current_brief=str(case.get("brief") or ""),
        )
        print(f"case {index}: {case['name']} -> model={agent.get('model')} include={agent.get('include_in_brief')} reply={agent.get('reply')}")
        assert_case(case, agent)
    print(f"real provider batch test passed: {len(CASES)} cases")


if __name__ == "__main__":
    main()
