from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from ragas.embeddings.base import embedding_factory
from ragas.llms import llm_factory
from ragas.metrics.collections import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness


async def evaluate(input_path: Path, output_path: Path, limit: int | None = None) -> dict:
    load_dotenv()
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    samples = payload["samples"]
    if limit is not None:
        samples = samples[: max(0, limit)]
    if not samples:
        raise RuntimeError("RAGAS input contains no samples")

    base_url = os.getenv("RAGAS_JUDGE_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("RAGAS_JUDGE_API_KEY") or os.getenv("OPENAI_API_KEY")
    judge_model = os.getenv("RAGAS_JUDGE_MODEL", "qwen-plus")
    judge_max_tokens = int(os.getenv("RAGAS_JUDGE_MAX_TOKENS", "4096"))
    embedding_model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-v3")
    if not base_url or not api_key:
        raise RuntimeError("RAGAS judge requires RAGAS_JUDGE_BASE_URL/KEY or OPENAI_BASE_URL/KEY")

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=120, max_retries=2)
    llm = llm_factory(judge_model, client=client, temperature=0, max_tokens=judge_max_tokens)
    embeddings = embedding_factory("openai", model=embedding_model, client=client)
    metrics = {
        "context_precision": ContextPrecision(llm=llm),
        "context_recall": ContextRecall(llm=llm),
        "faithfulness": Faithfulness(llm=llm),
        "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=embeddings),
    }

    rows = []
    for sample in samples:
        common = {
            "user_input": sample["user_input"],
            "response": sample["response"],
            "reference": sample["reference"],
            "retrieved_contexts": sample["retrieved_contexts"],
        }
        results = {
            "context_precision": await metrics["context_precision"].ascore(
                user_input=common["user_input"],
                reference=common["reference"],
                retrieved_contexts=common["retrieved_contexts"],
            ),
            "context_recall": await metrics["context_recall"].ascore(
                user_input=common["user_input"],
                reference=common["reference"],
                retrieved_contexts=common["retrieved_contexts"],
            ),
            "faithfulness": await metrics["faithfulness"].ascore(
                user_input=common["user_input"],
                response=common["response"],
                retrieved_contexts=common["retrieved_contexts"],
            ),
            "answer_relevancy": await metrics["answer_relevancy"].ascore(
                user_input=common["user_input"], response=common["response"]
            ),
        }
        rows.append(
            {
                "id": sample["id"],
                "scores": {name: float(result.value) for name, result in results.items()},
                "reasons": {name: result.reason for name, result in results.items() if result.reason},
            }
        )
        print(sample["id"], json.dumps(rows[-1]["scores"], ensure_ascii=False))
        write_checkpoint(
            output_path,
            payload,
            rows,
            base_url,
            judge_model,
            judge_max_tokens,
            embedding_model,
            complete=False,
        )

    averages = {
        name: sum(row["scores"][name] for row in rows) / len(rows)
        for name in metrics
    }
    report = {
        "createdAt": datetime.utcnow().isoformat(),
        "complete": True,
        "ragasVersion": "0.4.3",
        "judgeBaseUrl": base_url,
        "judgeModel": judge_model,
        "judgeMaxTokens": judge_max_tokens,
        "embeddingModel": embedding_model,
        "generatorProvider": payload.get("generatorProvider"),
        "generatorModel": payload.get("generatorModel"),
        "totalCases": len(rows),
        "metrics": averages,
        "results": rows,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def write_checkpoint(
    output_path: Path,
    payload: dict,
    rows: list[dict],
    base_url: str,
    judge_model: str,
    judge_max_tokens: int,
    embedding_model: str,
    complete: bool,
) -> None:
    metric_names = ["context_precision", "context_recall", "faithfulness", "answer_relevancy"]
    checkpoint = {
        "createdAt": datetime.utcnow().isoformat(),
        "complete": complete,
        "ragasVersion": "0.4.3",
        "judgeBaseUrl": base_url,
        "judgeModel": judge_model,
        "judgeMaxTokens": judge_max_tokens,
        "embeddingModel": embedding_model,
        "generatorProvider": payload.get("generatorProvider"),
        "generatorModel": payload.get("generatorModel"),
        "totalCases": len(rows),
        "metrics": {
            name: sum(row["scores"][name] for row in rows) / len(rows)
            for name in metric_names
        },
        "results": rows,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run isolated RAGAS 0.4 evaluation.")
    parser.add_argument("--input", default="target/ragas-input.json")
    parser.add_argument("--output", default="target/ragas-report.json")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    result = asyncio.run(evaluate(Path(args.input), Path(args.output), args.limit))
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
