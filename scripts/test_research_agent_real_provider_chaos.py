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


NOISE = [
    "你好",
    "你能干嘛",
    "你是不是大模型",
    "哈哈",
    "？",
    "不太对",
    "随便吧",
    "可以吗",
    "确认一下",
]

TOPICS = [
    ("transformer", "我想看 transoer 计算机视觉方向论文", ["Transformer", "transformer"]),
    ("polyurethane", "polyurethane waterproof latent curing coating recent papers", ["polyurethane", "聚氨酯"]),
    ("coating_cn", "近五年 潜伏固化 防水 涂层 论文", ["潜伏固化", "防水", "涂层", "latent"]),
    ("battery", "solid state battery electrolyte interface review papers", ["battery", "electrolyte", "interface"]),
    ("catalysis", "CO2 reduction photocatalysis TEOA sacrificial donor papers", ["CO2", "photocatalysis", "TEOA"]),
]


def run_seed(seed: int) -> dict[str, object]:
    rng = random.Random(seed)
    selected_topics = rng.sample(TOPICS, 3)
    sequence: list[tuple[str, str, list[str]]] = []
    for topic_id, message, markers in selected_topics:
        sequence.append((topic_id, message, markers))
        if rng.random() < 0.8:
            sequence.append(("noise", rng.choice(NOISE), []))
        if rng.random() < 0.6:
            sequence.append(("keyword", rng.choice(["给我生成关键词", "top5", "随便找几个关键词"]), markers))
    rng.shuffle(sequence)

    history: list[dict[str, object]] = []
    brief_parts: list[str] = []
    current_brief = ""
    turns: list[dict[str, object]] = []
    failures: list[str] = []
    included_markers: list[str] = []

    for topic_id, message, markers in sequence:
        started = perf_counter()
        agent = _build_research_agent_reply(message=message, history=history[-4:], current_brief=current_brief)
        elapsed = perf_counter() - started
        include = bool(agent.get("include_in_brief"))
        normalized = str(agent.get("normalized_requirement") or "")
        if agent.get("model") == "llm-unavailable":
            failures.append(f"provider unavailable on {message!r}: {agent.get('model_error')}")
        if topic_id == "noise" and include:
            failures.append(f"noise was included in brief: {message!r} -> {normalized!r}")
        if topic_id != "noise" and topic_id != "keyword" and include:
            if not any(marker.lower() in normalized.lower() for marker in markers):
                failures.append(f"topic marker missing for {topic_id}: {normalized!r}")
            included_markers.extend(markers)
        if topic_id == "keyword" and include:
            failures.append(f"keyword command should not append brief: {message!r} -> {normalized!r}")
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
            "topic_id": topic_id,
            "message": message,
            "model": agent.get("model"),
            "include": include,
            "reply": agent.get("reply"),
            "normalized": normalized,
            "error": agent.get("model_error", ""),
            "elapsed_seconds": round(elapsed, 2),
        })

    if not current_brief:
        failures.append("empty final brief")
    if any(noise in current_brief for noise in ["你好", "哈哈", "你能干嘛", "不太对"]):
        failures.append(f"final brief polluted by casual noise: {current_brief!r}")
    return {
        "seed": seed,
        "passed": not failures,
        "failures": failures,
        "final_brief": current_brief,
        "turns": turns,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real-provider chaos tests for the Research Agent.")
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--output", default=str(ROOT / "outputs" / "research_agent_real_provider_chaos.json"))
    args = parser.parse_args()
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise SystemExit("OPENAI_API_KEY is not set; real-provider chaos test cannot run.")

    results = [run_seed(seed) for seed in range(max(1, args.seeds))]
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = sum(1 for item in results if item["passed"])
    failed = len(results) - passed
    print(f"real provider chaos results: passed={passed} failed={failed} total={len(results)}")
    print(f"output={output_path}")
    for item in results:
        if not item["passed"]:
            print(f"FAIL seed={item['seed']}: {item['failures']}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
