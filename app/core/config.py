from typing import Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load variables from .env file into os.environ
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = Field(default="", repr=False)
    SUPABASE_SERVICE_ROLE_KEY: str = Field(default="", repr=False)
    FRONTEND_URL: str = "https://chronosync-lovat.vercel.app"
    ENCRYPTION_MASTER_KEY: str = Field(default="", repr=False)
    RESEND_API_KEY: str = Field(default="", repr=False)
    EMAIL_FROM: str = ""
    EMAIL_REPLY_TO: str = ""
    EMAIL_WORKER_ENABLED: bool = True
    EMAIL_POLL_SECONDS: float = Field(default=15, ge=1, le=300)
    INVOICE_EMAIL_TRANSPORT: Literal["auto", "provider", "resend"] = "auto"


settings = Settings()
