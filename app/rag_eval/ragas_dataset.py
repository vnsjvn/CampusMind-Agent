from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from app.core.bootstrap import create_schema, seed_data
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient
from app.services.knowledge import KnowledgeService


def build_dataset(limit: int | None = None) -> dict:
    settings = get_settings()
    if settings.ai_provider.lower() == "mock":
        raise RuntimeError("RAGAS dataset generation requires a real answer model; AI_PROVIDER=mock is not allowed")
    if not settings.knowledge_vector_enabled or not settings.knowledge_vector_required:
        raise RuntimeError("RAGAS dataset generation requires real vector retrieval with fallback disabled")

    cases = json.loads(Path(settings.ragas_eval_dataset).read_text(encoding="utf-8"))
    if limit is not None:
        cases = cases[: max(0, limit)]

    create_schema()
    db = SessionLocal()
    try:
        seed_data(db)
        knowledge = KnowledgeService(db, settings)
        ai = AiClient(settings)
        samples = []
        for case in cases:
            retrieved = knowledge.retrieve(case["question"], settings.knowledge_top_k)
            contexts = [item.content for item in retrieved]
            response = generate_grounded_response(ai, case["question"], contexts)
            samples.append(
                {
                    "id": case["id"],
                    "user_input": case["question"],
                    "response": response,
                    "reference": case["reference"],
                    "retrieved_contexts": contexts,
                    "retrieved_sources": [item.source for item in retrieved],
                }
            )
    finally:
        db.close()

    payload = {
        "createdAt": datetime.utcnow().isoformat(),
        "generatorProvider": settings.ai_provider,
        "generatorModel": settings.ollama_model if settings.ai_provider.lower() == "ollama" else settings.openai_model,
        "retrieval": "Chroma + BM25 + local rerank",
        "topK": settings.knowledge_top_k,
        "samples": samples,
    }
    output = Path(settings.ragas_eval_input)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def generate_grounded_response(ai: AiClient, question: str, contexts: list[str]) -> str:
    context = "\n\n".join(f"[{index}] {text}" for index, text in enumerate(contexts, start=1))
    return ai.complete(
        [
            AiMessage(
                role="system",
                content=(
                    "你是校园心理知识问答助手。只根据给定检索上下文回答；信息不足时明确说明。"
                    "回答简洁、非诊断、不提供药物或危险操作建议。高风险问题优先给出现实求助和安全建议。"
                ),
            ),
            AiMessage(role="user", content=f"检索上下文：\n{context}\n\n问题：\n{question}"),
        ]
    ).strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate real RAG inputs for isolated RAGAS evaluation.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    result = build_dataset(args.limit)
    print(f"RAGAS input generated: samples={len(result['samples'])}")
