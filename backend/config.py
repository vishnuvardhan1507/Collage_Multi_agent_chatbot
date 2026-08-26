import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")
load_dotenv(BASE_DIR / ".env")


class Config:
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-secret-change-me-please-set-a-real-32-byte-key")
    DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "db" / "college.db"))
    CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", str(BASE_DIR / "chroma_store"))
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    CHAT_MEMORY_DIR = os.getenv("CHAT_MEMORY_DIR", str(BASE_DIR / "chat_memory"))
    KNOWLEDGE_BASE_DIR = os.getenv("KNOWLEDGE_BASE_DIR", str(BASE_DIR / "knowledge_base"))
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173")
