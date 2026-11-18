import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")
load_dotenv(BASE_DIR / ".env")


def configured_path(name, default):
    """Resolve .env paths from the project root, regardless of launch directory."""
    path = Path(os.getenv(name) or default).expanduser()
    if not path.is_absolute():
        path = BASE_DIR.parent / path
    return str(path.resolve())


class Config:
    COLLEGE_TIMEZONE = os.getenv("COLLEGE_TIMEZONE", "Asia/Kolkata")
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_SECURITY = os.getenv("SMTP_SECURITY", "starttls")
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "")
    SMTP_TIMEOUT = 15
    PUBLIC_APP_URL = os.getenv("PUBLIC_APP_URL", "http://localhost:5173")
    PASSWORD_RESET_TTL_SECONDS = 600
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
    DATABASE_PATH = configured_path("DATABASE_PATH", BASE_DIR / "db" / "college.db")
    CHROMA_PERSIST_DIR = configured_path("CHROMA_PERSIST_DIR", BASE_DIR / "chroma_store")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    CHAT_MEMORY_DIR = configured_path("CHAT_MEMORY_DIR", BASE_DIR / "chat_memory")
    KNOWLEDGE_BASE_DIR = configured_path("KNOWLEDGE_BASE_DIR", BASE_DIR / "knowledge_base")
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173")
