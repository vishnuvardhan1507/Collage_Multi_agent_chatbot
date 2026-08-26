import os
from pathlib import Path
from typing import List, Tuple

from flask import current_app, has_app_context

_collection = None
_fallback_chunks: List[Tuple[str, str]] = []

DEFAULT_CHUNK_SIZE = 450
DEFAULT_CHUNK_OVERLAP = 75


def _kb_dir() -> Path:
    if has_app_context():
        return Path(current_app.config["KNOWLEDGE_BASE_DIR"])
    return Path(__file__).resolve().parents[1] / "knowledge_base"


def _persist_dir() -> str:
    if has_app_context():
        return current_app.config["CHROMA_PERSIST_DIR"]
    return str(Path(__file__).resolve().parents[1] / "chroma_store")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _chunk_settings() -> Tuple[int, int]:
    size = _env_int("RAG_CHUNK_SIZE", DEFAULT_CHUNK_SIZE)
    overlap = _env_int("RAG_CHUNK_OVERLAP", DEFAULT_CHUNK_OVERLAP)
    if size < 200:
        size = DEFAULT_CHUNK_SIZE
    if overlap < 0 or overlap >= size:
        overlap = min(DEFAULT_CHUNK_OVERLAP, size // 4)
    return size, overlap


def _chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> List[str]:
    if size is None or overlap is None:
        size, overlap = _chunk_settings()
    chunks = []
    start = 0
    while start < len(text):
        chunk = text[start : start + size].strip()
        if chunk:
            chunks.append(chunk)
        start += size - overlap
    return chunks


def _load_chunks() -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    for path in sorted(_kb_dir().glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for index, chunk in enumerate(_chunk_text(text)):
            items.append((f"{path.stem}-{index}", chunk))
    return items


def ensure_vector_store():
    global _collection, _fallback_chunks
    _fallback_chunks = _load_chunks()
    if not _fallback_chunks:
        return None
    if os.getenv("RAG_USE_CHROMA", "0") != "1":
        _collection = None
        return None

    try:
        import chromadb
        from sentence_transformers import SentenceTransformer

        class SentenceTransformerEmbeddingFunction:
            def __init__(self):
                allow_download = os.getenv("ALLOW_MODEL_DOWNLOAD", "0") == "1"
                self.model = SentenceTransformer("all-MiniLM-L6-v2", local_files_only=not allow_download)

            def __call__(self, input):
                return self.model.encode(input, normalize_embeddings=True).tolist()

        client = chromadb.PersistentClient(path=_persist_dir())
        embedding_fn = SentenceTransformerEmbeddingFunction()
        collection = client.get_or_create_collection("college_knowledge", embedding_function=embedding_fn)
        existing = collection.count()
        if existing and existing != len(_fallback_chunks):
            existing_ids = collection.get().get("ids", [])
            if existing_ids:
                collection.delete(ids=existing_ids)
            existing = 0
        if existing == 0:
            ids = [item[0] for item in _fallback_chunks]
            docs = [item[1] for item in _fallback_chunks]
            collection.add(ids=ids, documents=docs)
        _collection = collection
    except Exception:
        _collection = None
    return _collection


def _lexical_query(question: str, k: int) -> str:
    terms = {term.lower().strip(".,?!") for term in question.split() if len(term) > 2}
    ranked = []
    for chunk_id, chunk in _fallback_chunks or _load_chunks():
        lower = chunk.lower()
        score = sum(1 for term in terms if term in lower)
        ranked.append((score, chunk_id, chunk))
    ranked.sort(reverse=True)
    return "\n\n".join(chunk for score, _, chunk in ranked[:k] if score > 0) or "\n\n".join(chunk for _, chunk in _fallback_chunks[:k])


def query(question: str, k: int = 4) -> str:
    if _collection is None:
        ensure_vector_store()
    if _collection is not None:
        result = _collection.query(query_texts=[question], n_results=k)
        return "\n\n".join(result.get("documents", [[]])[0])
    return _lexical_query(question, k)
