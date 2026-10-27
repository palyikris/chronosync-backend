import os
from dotenv import load_dotenv

# Load variables from .env file into os.environ
load_dotenv()


class Settings:
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "")
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "https://chronosync-lovat.vercel.app")
    ENCRYPTION_MASTER_KEY: str = os.getenv("ENCRYPTION_MASTER_KEY", "")


settings = Settings()
