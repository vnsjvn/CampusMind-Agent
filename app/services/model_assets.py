from __future__ import annotations

from pathlib import Path

from app.core.config import Settings


def finetuned_model_status(settings: Settings) -> dict:
    root = settings.project_root
    model_dir = resolve_model_dir(settings)
    gguf_path = model_dir / settings.finetuned_model_file
    modelfile_path = model_dir / "Modelfile"
    return {
        "name": settings.finetuned_model_name,
        "directory": str(model_dir.relative_to(root)) if model_dir.is_relative_to(root) else str(model_dir),
        "ggufFile": settings.finetuned_model_file,
        "ggufExists": gguf_path.exists(),
        "ggufSizeBytes": gguf_path.stat().st_size if gguf_path.exists() else 0,
        "modelfileExists": modelfile_path.exists(),
        "ollamaCreateCommand": f"scripts/create-finetuned-model.sh",
    }


def resolve_model_dir(settings: Settings) -> Path:
    path = Path(settings.finetuned_model_dir)
    return path if path.is_absolute() else settings.project_root / path
