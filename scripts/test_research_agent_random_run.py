from __future__ import annotations

import os
from pathlib import Path
import random
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ligen_downloader.web_app import LigenWebController
from ligen_downloader.web_app import _build_research_agent_reply
from ligen_downloader.web_app import _keyword_terms_from_brief


NOISE = [
    "你好呀",
    "你是谁，可以干嘛",
    "给我生成关键词",
    "哈哈这个感觉不太行",
    "可以吗",
    "确认一下",
    "你看着办",
    "我想看",
    "不是",
]

TOPICS = [
    "polyurethane waterproof latent curing coating recent papers",
    "算了，换成 transformer 计算机论文",
    "papers about transoer architecture in computer vision",
]


def run_dialog(seed: int) -> tuple[str, list[str], list[dict[str, object]]]:
    rng = random.Random(seed)
    messages = NOISE[:]
    rng.shuffle(messages)
    messages.insert(rng.randrange(0, len(messages) + 1), TOPICS[0])
    messages.insert(rng.randrange(0, len(messages) + 1), TOPICS[1])
    messages.append(TOPICS[2])
    messages.append(rng.choice(["top5", "随便找几个", "给我生成关键词吧"]))

    history: list[dict[str, object]] = []
    brief_parts: list[str] = []
    current_brief = ""
    for message in messages:
        agent = _build_research_agent_reply(message=message, history=history[-4:], current_brief=current_brief)
        if agent.get("model") == "llm-unavailable":
            raise AssertionError(f"LLM unavailable during real random run: {agent!r}")
        include = bool(agent.get("include_in_brief"))
        normalized = str(agent.get("normalized_requirement") or "")
        reply = str(agent.get("reply") or "")
        if bool(agent.get("replace_brief")):
            brief_parts = []
        if include and normalized:
            brief_parts.append(normalized)
            current_brief = "\n\n".join(brief_parts)
        history.extend([
            {"role": "user", "text": message, "includeInBrief": include, "normalizedRequirement": normalized},
            {"role": "agent", "text": reply},
        ])

    if "Transformer" not in current_brief:
        raise AssertionError(f"seed {seed}: brief did not converge to Transformer: {current_brief!r}")
    return current_brief, _keyword_terms_from_brief(current_brief)[:5], history


def run_confirm_and_search(brief: str, terms: list[str]) -> None:
    controller = LigenWebController()
    payload = {
        "task_name": "random-agent-transformer-test",
        "research_query_text": "10.1145/3368089.3409717\n" + brief,
        "research_confirmed_terms_text": "\n".join(terms),
        "research_limit_per_provider": "3",
        "research_provider_crossref": False,
        "research_provider_openalex": False,
        "research_provider_local_manual": True,
        "research_keywords_confirmed": False,
    }
    confirmed = controller.confirm_research_terms(payload)
    if not confirmed["research"]["keywords_confirmed"]:
        raise AssertionError("keywords were not confirmed")
    searched = controller.run_research_search({
        **payload,
        "research_keywords_confirmed": True,
        "research_last_keyword_set_id": confirmed["research"]["keyword_set_id"],
    })
    research = searched["research"]
    if not research["run_id"]:
        raise AssertionError("search run was not saved")
    if not research["records"]:
        raise AssertionError("local_manual search returned no records")
    print(f"confirmed keyword_set={research['keyword_set_id']} run={research['run_id']} records={len(research['records'])}")


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print("skipped real random LLM dialog test: OPENAI_API_KEY is not set")
        return

    final_brief = ""
    final_terms: list[str] = []
    for seed in range(3):
        brief, terms, _history = run_dialog(seed)
        print(f"seed={seed} brief={brief!r} terms={terms}")
        final_brief, final_terms = brief, terms
    run_confirm_and_search(final_brief, final_terms)
    print("real random loose dialog to search test passed")


if __name__ == "__main__":
    main()
