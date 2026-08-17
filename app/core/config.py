from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    agent_framework: str = "langgraph"
    ai_provider: str = "ollama"
    ai_temperature: float = 0.35
    ai_max_tokens: int = 512
    agent_plan_max_attempts: int = 2
    agent_plan_retry_delay_seconds: float = 0.25
    trace_planning_min_sample_size: int = 20
    trace_planning_retry_warning_rate: float = 0.10
    trace_planning_failure_critical_rate: float = 0.05
    operational_alert_monitor_enabled: bool = True
    operational_alert_monitor_interval_seconds: float = 60.0
    operational_alert_monitor_trace_limit: int = 500
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "mindbridge-qwen2.5-7b-ft:latest"
    ollama_timeout_seconds: float = 180.0
    finetuned_model_name: str = "mindbridge-qwen2.5-7b-ft:latest"
    finetuned_model_dir: str = "models/mindbridge-qwen2.5-7b-ft"
    finetuned_model_file: str = "mindbridge-qwen2.5-7b-ft-q4_k_m.gguf"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    database_url: str = "mysql+pymysql://mindbridge:mindbridge@127.0.0.1:3306/mindbridge?charset=utf8mb4"
    chat_history_limit: int = 10
    knowledge_top_k: int = 4
    knowledge_candidate_k: int = 16
    knowledge_chunk_size: int = 512
    knowledge_chunk_overlap: int = 64
    knowledge_hybrid_vector_weight: float = 0.65
    knowledge_hybrid_bm25_weight: float = 0.35
    knowledge_rerank_enabled: bool = True
    knowledge_vector_enabled: bool = True
    knowledge_vector_required: bool = False
    chroma_persist_dir: str = "data/chroma"
    chroma_collection_name: str = "mindbridge_knowledge"
    chroma_snapshot_dir: str = "data/chroma-snapshots"
    chroma_snapshot_keep: int = 5
    embedding_timeout_seconds: float = 30.0
    rag_eval_dataset: str = "app/rag_eval/mindbridge-rag-eval.json"
    rag_eval_output: str = "target/rag-eval-report.json"
    rag_eval_enabled: bool = False
    rag_eval_exit_after_run: bool = False
    ragas_eval_dataset: str = "app/rag_eval/mindbridge-ragas-eval.json"
    ragas_eval_input: str = "target/ragas-input.json"
    ragas_eval_output: str = "target/ragas-report.json"
    ragas_judge_model: str = "qwen-plus"
    ragas_judge_max_tokens: int = 4096
    excel_path: str = "data/mindbridge-risk-ledger.xlsx"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_memory_ttl_seconds: int = 86400
    redis_memory_max_messages: int = 40
    redis_socket_timeout_seconds: float = 2.0
    redis_memory_required: bool = False
    memory_compaction_enabled: bool = True
    memory_compaction_recent_messages: int = 8
    memory_summary_max_chars: int = 500
    long_term_memory_enabled: bool = True
    context_max_chars: int = 12000
    context_history_max_chars: int = 4000
    context_memory_max_chars: int = 800
    context_knowledge_max_chars: int = 5000
    context_skill_max_chars: int = 3000
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = 10.0
    alert_email_delivery_mode: str = "log"
    alert_email_from: str = ""
    alert_email_to: str = ""
    alert_email_subject_prefix: str = "[MindBridge 高风险预警]"
    tool_queue_enabled: bool = True
    tool_queue_poll_interval_seconds: float = 1.0
    tool_queue_batch_size: int = 10
    tool_queue_max_attempts: int = 3
    tool_queue_retry_delay_seconds: float = 15.0
    tool_queue_retry_max_delay_seconds: float = 300.0
    tool_queue_excel_workers: int = 1
    tool_queue_email_workers: int = 2
    alert_email_rate_limit_per_minute: int = 30
    mcp_staff_approval_token: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]


@lru_cache
def get_settings() -> Settings:
    return Settings()
