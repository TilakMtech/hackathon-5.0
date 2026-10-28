from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load backend/.env into the process so vendor SDK variables (ANTHROPIC_FOUNDRY_*) are visible too.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class Settings(BaseSettings):
    """All settings come from environment variables (Azure Container Apps sets them).

    Secrets (Foundry key) are injected from Key Vault as secret refs, never committed.
    """

    model_config = SettingsConfigDict(env_prefix="OTD_", env_file=".env", extra="ignore")

    data_dir: str = str(Path(__file__).resolve().parent.parent / "data")
    cors_origins: str = "*"

    # Bad-lane rule: late rate on clean orders >= multiple x network rate, above floor, enough volume
    bad_lane_multiple: float = 2.0
    bad_lane_floor: float = 0.15
    bad_lane_min_n: int = 100

    # LLM provider: mock (offline, deterministic) | claude_foundry | azure_openai
    llm_provider: str = "mock"
    claude_fast_model: str = "claude-haiku-5-5"      # Foundry deployment names
    claude_smart_model: str = "claude-sonnet-5-5"
    aoai_base_url: str = ""                          # e.g. https://<resource>.openai.azure.com/openai/v1
    aoai_api_key: str = ""
    aoai_fast_deployment: str = "gpt-4.1-mini"
    aoai_smart_deployment: str = "gpt-4.1"
    llm_send_temperature: bool = True               # auto-retries without it if a model rejects temperature
    llm_timeout_s: float = 60.0
    llm_log_file: str = ""                          # optional JSONL log of every call (prompt version, tokens, latency)

    # Intake guardrails
    intake_auto_accept_confidence: float = 0.9
    sql_row_limit: int = 200

    # Endpoints that spend money on model calls (intake, alerts, ask, eval runs)
    api_key: str = ""            # when set, those endpoints need header X-API-Key; set it whenever the API is public
    rate_limit_per_min: int = 30  # per client IP, on those endpoints

    # Leadership targets
    otd_target: float = 0.85
    otd_benchmark: float = 0.94


settings = Settings()
