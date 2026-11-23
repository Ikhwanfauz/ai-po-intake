import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "postgresql+psycopg://po_demo:po_demo@127.0.0.1:55432/po_demo"
    ))
    storage: Path = field(default_factory=lambda: Path(os.getenv("PDF_STORAGE", "data/pdf")))
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"))
    model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen3:8b"))
    timeout: float = field(default_factory=lambda: float(os.getenv("OLLAMA_TIMEOUT", "180")))
    reviewer: str = field(default_factory=lambda: os.getenv("DEMO_REVIEWER", "Meridian Demo Reviewer"))
