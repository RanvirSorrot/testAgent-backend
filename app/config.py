from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    supabase_url: Optional[str] = None
    supabase_service_key: Optional[str] = None
    max_actions_per_session: int = 50
    screenshot_quality: int = 80
    cors_origins: str = "http://localhost:3000"

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
