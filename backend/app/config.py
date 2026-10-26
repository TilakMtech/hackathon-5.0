from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Leadership targets
    otd_target: float = 0.85
    otd_benchmark: float = 0.94


settings = Settings()
