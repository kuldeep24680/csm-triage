from datetime import date
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRIAGE_", env_file=REPO_ROOT / ".env", extra="ignore")

    database_url: str = "postgresql://triage:triage@localhost:5432/triage"
    data_dir: Path = REPO_ROOT / "data"
    jwt_secret: str = "dev-only-change-me-not-for-production-use"
    jwt_ttl_hours: int = 12

    # The synthetic world ends "today". Fixed so runs are reproducible.
    sim_end_date: date = date(2026, 10, 16)
    sim_years: int = 3
    seed: int = 7

    # LLM. Empty key -> deterministic template writer (system still works).
    openai_api_key: str = Field("", validation_alias=AliasChoices("TRIAGE_OPENAI_API_KEY", "OPENAI_API_KEY"))
    openai_model: str = "gpt-4o-mini"
    llm_timeout_s: float = 30.0
    llm_max_accounts_per_run: int = 400   # cost guard: briefs only for the call list
    llm_concurrency: int = 16             # parallel LLM calls per run (gpt-4o-mini rate limits allow far more)
    reuse_briefs: bool = True             # reuse last run's brief when the account's facts haven't changed

    static_dir: Path = REPO_ROOT / "frontend" / "dist"

    # Operations
    use_wall_clock: bool = False          # production: score "today" (tenant-local); demo: sim_end_date
    retry_backoff_s: int = 30             # wait before the single automatic retry of a failed run
    alert_webhook_url: str = ""           # Slack-compatible incoming webhook; empty -> alerts only logged


settings = Settings()
