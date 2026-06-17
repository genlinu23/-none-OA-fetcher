from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import sys
from time import perf_counter


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ligen_downloader.web_app import _build_research_agent_reply


SCENARIOS = [
    {
        "name": "casual identity",
        "message": "你是谁，可以干嘛",
        "brief": "",
        "expect_include": False,
        "expect_empty_normalized": True,
    },
    {
        "name": "greeting",
        "message": "你好呀",
        "brief": "",
        "expect_include": False,
        "expect_empty_normalized": True,
    },
    {
        "name": "random text",
        "message": "？大声道",
        "brief": "",
        "expect_include": False,
        "expect_empty_normalized": True,
    },
    {
        "name": "transformer typo",
        "message": "我想看 transoer 的论文，计算机方向",
        "brief": "",
        "expect_include": True,
        "normalized_has_any": ["Transformer", "transformer"],
    },
    {
        "name": "transformer english",
        "message": "papers about transformer architecture in computer vision",
        "brief": "",
        "expect_include": True,
        "normalized_has_any": ["Transformer", "transformer", "computer vision"],
    },
    {
        "name": "polyurethane english",
        "message": "polyurethane waterproof latent curing coating recent papers",
        "brief": "",
        "expect_include": True,
        "normalized_has_any": ["polyurethane", "聚氨酯"],
    },
    {
        "name": "latent curing chinese",
        "message": "近五年 潜伏固化 防水 涂层 论文",
        "brief": "",
        "expect_include": True,
        "normalized_has_any": ["潜伏固化", "latent curing", "防水", "涂层"],
    },
    {
        "name": "keyword request with brief",
        "message": "给我生成关键词吧",
        "brief": "Transformer architecture computer vision survey",
        "expect_include": False,
        "expect_empty_normalized": True,
        "reply_has_any": ["Transformer", "关键词", "keyword", "computer vision"],
    },
    {
        "name": "top5 with brief",
        "message": "top5",
        "brief": "polyurethane waterproof latent curing coating",
        "expect_include": False,
        "expect_empty_normalized": True,
        "reply_has_any": ["polyurethane", "聚氨酯", "关键词", "keyword"],
    },
    {
        "name": "confirm with brief",
        "message": "确认一下",
        "brief": "Transformer architecture computer vision survey",
        "expect_include": False,
        "expect_empty_normalized": True,
    },
    {
        "name": "vague acknowledgement is not confirmation",
        "message": "好的",
        "brief": "Transformer architecture computer vision survey",
        "expect_include": False,
        "expect_empty_normalized": True,
        "reply_lacks_any": ["已确认"],
    },
    {
        "name": "topic switch",
        "message": "算了，换成 transformer 计算机视觉论文",
        "brief": "polyurethane waterproof latent curing coating",
        "expect_include": True,
        "normalized_has_any": ["Transformer", "transformer"],
    },
    {
        "name": "loose topic",
        "message": "我想看防水涂料潜伏固化方面比较新的文章",
        "brief": "",
        "expect_include": True,
        "normalized_has_any": ["防水", "潜伏固化", "coating", "latent"],
    },
]


def validate(scenario: dict[str, object], agent: dict[str, object]) -> list[str]:
    failures: list[str] = []
    if agent.get("model") == "llm-unavailable":
        failures.append(f"provider unavailable: {agent.get('model_error')}")
        return failures
    include = bool(agent.get("include_in_brief"))
    if include != bool(scenario["expect_include"]):
        failures.append(f"include expected {scenario['expect_include']} got {include}")
    normalized = str(agent.get("normalized_requirement") or "")
    if scenario.get("expect_empty_normalized") and normalized:
        failures.append(f"normalized should be empty, got {normalized!r}")
    normalized_has_any = [str(item) for item in scenario.get("normalized_has_any", [])]  # type: ignore[union-attr]
    if normalized_has_any and not any(item.lower() in normalized.lower() for item in normalized_has_any):
        failures.append(f"normalized missing any of {normalized_has_any!r}: {normalized!r}")
    reply = str(agent.get("reply") or "")
    reply_has_any = [str(item) for item in scenario.get("reply_has_any", [])]  # type: ignore[union-attr]
    if reply_has_any and not any(item.lower() in reply.lower() for item in reply_has_any):
        failures.append(f"reply missing any of {reply_has_any!r}: {reply!r}")
    reply_lacks_any = [str(item) for item in scenario.get("reply_lacks_any", [])]  # type: ignore[union-attr]
    if reply_lacks_any and any(item in reply for item in reply_lacks_any):
        failures.append(f"reply contains forbidden text from {reply_lacks_any!r}: {reply!r}")
    return failures


def run_single_scenarios(rounds: int) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for round_index in range(rounds):
        for scenario in SCENARIOS:
            started = perf_counter()
            agent = _build_research_agent_reply(
                message=str(scenario["message"]),
                history=[],
                current_brief=str(scenario.get("brief") or ""),
            )
            elapsed = perf_counter() - started
            failures = validate(scenario, agent)
            results.append({
                "kind": "single",
                "round": round_index + 1,
                "name": scenario["name"],
                "message": scenario["message"],
                "model": agent.get("model"),
                "include": agent.get("include_in_brief"),
                "reply": agent.get("reply"),
                "normalized": agent.get("normalized_requirement"),
                "error": agent.get("model_error", ""),
                "elapsed_seconds": round(elapsed, 2),
                "passed": not failures,
                "failures": failures,
            })
    return results


def run_dialog(seed: int) -> dict[str, object]:
    rng = random.Random(seed)
    messages = [
        "你好呀",
        "你是谁，可以干嘛",
        "我想看 transoer 的论文，计算机方向",
        "给我生成关键词吧",
        "算了，换成 polyurethane waterproof latent curing coating recent papers",
        "top5",
        "确认一下",
    ]
    noise = messages[:2]
    body = messages[2:]
    rng.shuffle(noise)
    rng.shuffle(body)
    sequence = noise + body

    history: list[dict[str, object]] = []
    brief_parts: list[str] = []
    current_brief = ""
    turns: list[dict[str, object]] = []
    failures: list[str] = []
    for message in sequence:
        started = perf_counter()
        agent = _build_research_agent_reply(message=message, history=history[-4:], current_brief=current_brief)
        elapsed = perf_counter() - started
        include = bool(agent.get("include_in_brief"))
        normalized = str(agent.get("normalized_requirement") or "")
        if agent.get("model") == "llm-unavailable":
            failures.append(f"provider unavailable on {message!r}: {agent.get('model_error')}")
        if bool(agent.get("replace_brief")):
            brief_parts = []
        if include and normalized:
            brief_parts.append(normalized)
            current_brief = "\n\n".join(brief_parts)
        history.extend([
            {"role": "user", "text": message, "includeInBrief": include, "normalizedRequirement": normalized},
            {"role": "agent", "text": str(agent.get("reply") or "")},
        ])
        turns.append({
            "message": message,
            "model": agent.get("model"),
            "include": include,
            "reply": agent.get("reply"),
            "normalized": normalized,
            "error": agent.get("model_error", ""),
            "elapsed_seconds": round(elapsed, 2),
        })

    if not current_brief:
        failures.append("dialog produced empty final brief")
    if any(noise_text in current_brief for noise_text in ["你好", "你是谁"]):
        failures.append(f"dialog brief polluted by casual chat: {current_brief!r}")
    return {
        "kind": "dialog",
        "seed": seed,
        "passed": not failures,
        "failures": failures,
        "final_brief": current_brief,
        "turns": turns,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real-provider stress tests for the Research Agent.")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--dialogs", type=int, default=3)
    parser.add_argument("--output", default=str(ROOT / "outputs" / "research_agent_real_provider_stress.json"))
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise SystemExit("OPENAI_API_KEY is not set; real-provider stress test cannot run.")

    results: list[dict[str, object]] = []
    results.extend(run_single_scenarios(max(1, args.rounds)))
    for seed in range(max(0, args.dialogs)):
        results.append(run_dialog(seed))

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = sum(1 for item in results if item.get("passed"))
    failed = len(results) - passed
    print(f"real provider stress results: passed={passed} failed={failed} total={len(results)}")
    print(f"output={output_path}")
    for item in results:
        if not item.get("passed"):
            print(f"FAIL {item.get('kind')} {item.get('name', item.get('seed'))}: {item.get('failures')}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
