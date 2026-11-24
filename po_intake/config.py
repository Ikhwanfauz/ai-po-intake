import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def env_flag(name):
    return os.getenv(name, "").strip().lower() in {"true", "1"}


def env_mapping(name):
    value = json.loads(os.getenv(name, "{}"))
    if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
        raise ValueError(f"{name} must be a JSON object of strings.")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "postgresql+psycopg://po_demo:po_demo@127.0.0.1:55432/po_demo"
    ))
    storage: Path = field(default_factory=lambda: Path(os.getenv("PDF_STORAGE", "data/pdf")))
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"))
    model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen3:8b"))
    timeout: float = field(default_factory=lambda: float(os.getenv("OLLAMA_TIMEOUT", "180")))
    graph_timeout: float = field(default_factory=lambda: float(os.getenv("PO_GRAPH_TIMEOUT", "45")))
    mcp_api_url: str = field(default_factory=lambda: os.getenv("PO_MCP_API_URL", "http://127.0.0.1:8000"))
    mcp_timeout: float = field(default_factory=lambda: float(os.getenv("PO_MCP_TIMEOUT", "12")))
    chat_timeout: float = field(default_factory=lambda: float(os.getenv("PO_CHAT_TIMEOUT", "20")))
    sop_index_path: Path = field(default_factory=lambda: Path(os.getenv("PO_SOP_INDEX", "data/sop-index.json")))
    graph_auto: bool = field(default_factory=lambda: os.getenv("PO_GRAPH_AUTO", "true").strip().lower() in {"true", "1"})
    reviewer: str = field(default_factory=lambda: os.getenv("DEMO_REVIEWER", "Meridian Demo Reviewer"))
    foreground: bool = field(default_factory=lambda: env_flag("PO_FOREGROUND_EXTRACTION"))
    extraction_lease: int = field(default_factory=lambda: int(os.getenv("PO_EXTRACTION_LEASE_SECONDS", "0")))
    extraction_max_attempts: int = field(default_factory=lambda: int(os.getenv("PO_EXTRACTION_MAX_ATTEMPTS", "3")))
    extraction_backoff: int = field(default_factory=lambda: int(os.getenv("PO_EXTRACTION_BACKOFF_SECONDS", "5")))
    integration_key: str = field(default_factory=lambda: os.getenv("PO_INTEGRATION_KEY", ""))
    staff_key: str = field(default_factory=lambda: os.getenv("PO_STAFF_KEY", ""))
    outbound_enabled: bool = field(default_factory=lambda: env_flag("PO_OUTBOUND_ENABLED"))
    telegram_chat: str = field(default_factory=lambda: os.getenv("PO_TELEGRAM_CHAT_ID", "").strip())
    dashboard_url: str = field(default_factory=lambda: os.getenv("PO_DASHBOARD_URL", "http://127.0.0.1:8503"))
    gmail_senders: dict[str, str] = field(default_factory=lambda: {
        email.strip().lower(): customer.strip() for email, customer in env_mapping("PO_GMAIL_SENDERS").items()
    })
    gmail_recipients: dict[str, str] = field(default_factory=lambda: {
        customer.strip(): email.strip() for customer, email in env_mapping("PO_GMAIL_RECIPIENTS").items()
    })
    notification_lease: int = field(default_factory=lambda: int(os.getenv("PO_NOTIFICATION_LEASE_SECONDS", "180")))
    notification_max_attempts: int = field(default_factory=lambda: int(os.getenv("PO_NOTIFICATION_MAX_ATTEMPTS", "3")))
    notification_backoff: int = field(default_factory=lambda: int(os.getenv("PO_NOTIFICATION_BACKOFF_SECONDS", "30")))

    def __post_init__(self):
        if min(self.extraction_max_attempts, self.notification_max_attempts, self.notification_lease,
               self.timeout, self.graph_timeout, self.mcp_timeout, self.chat_timeout) <= 0:
            raise ValueError("Timeouts, notification leases, and attempt limits must be positive.")
        if min(self.extraction_lease, self.extraction_backoff, self.notification_backoff) < 0:
            raise ValueError("Extraction lease and retry delays must be nonnegative.")
        if any(not EMAIL_ADDRESS.fullmatch(address)
               for address in [*self.gmail_senders, *self.gmail_recipients.values()]):
            raise ValueError("Configure one bare email address per Gmail sender or recipient mapping.")
        if self.telegram_chat and not re.fullmatch(r"-?\d+", self.telegram_chat):
            raise ValueError("PO_TELEGRAM_CHAT_ID must be a numeric demo chat ID.")
        if self.staff_key and self.staff_key == self.integration_key:
            raise ValueError("Use different staff and integration keys.")
