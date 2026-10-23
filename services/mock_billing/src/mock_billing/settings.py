from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings (BILLING_DATABASE_URL, BILLING_API_KEY, ...)."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    billing_database_url: str
    billing_api_key: str
    fault_injection_enabled: bool = False
    fault_admin_key: str = ""
    personas_path: Path = Path("data/synthetic/personas.json")
