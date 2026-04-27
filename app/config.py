from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DHCP_API_TOKEN: str = ""  # empty = auth disabled
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    LOG_LEVEL: str = "INFO"
    POWERSHELL_COMMAND_TIMEOUT_SECONDS: int = Field(default=60, ge=1)
    POWERSHELL_ENV_CHECK_TIMEOUT_SECONDS: int = Field(default=15, ge=1)
    POWERSHELL_MAX_CONCURRENCY: int = Field(default=5, ge=1)


settings = Settings()
