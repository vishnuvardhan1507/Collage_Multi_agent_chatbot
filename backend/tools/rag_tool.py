import hashlib
import os
import re
from pathlib import Path
from threading import RLock

from flask import current_app, has_app_context

_lock = RLock()
_collection = None
_embedder = None
_cache_key = None
_chunks = []


def _kb_dir():
    return Path(current_app.config["KNOWLEDGE_BASE_DIR"]) if has_app_context() else Path(__file__).resolve().parents[1] / "knowledge_base"


def _persist_dir():
    return str(current_app.config["CHROMA_PERSIST_DIR"]) if has_app_context() else str(Path(__file__).resolve().parents[1] / "chroma_store")


def _load_chunks():
    chunks = []
    for path in sorted(_kb_dir().glob("*.md")):
        content = path.read_text(encoding="utf-8")
        for index, start in enumerate(range(0, len(content), 780)):
            chunks.append({"id": f"{path.stem}-{index}", "text": content[start:start + 900], "source": path.name})
    return chunks


def ensure_vector_store():
    global _collection, _embedder, _cache_key, _chunks
    with _lock:
        chunks = _load_chunks()
        fingerprint = hashlib.sha256(repr(chunks).encode()).hexdigest()
        key = (str(_kb_dir()), _persist_dir(), fingerprint, os.getenv("RAG_USE_CHROMA", "0"), os.getenv("ALLOW_MODEL_DOWNLOAD", "0"))
        if key == _cache_key:
            return _collection
        _chunks, _collection, _embedder, _cache_key = chunks, None, None, key
        if not chunks or key[3] != "1":
            return None
        try:
            import chromadb
            from sentence_transformers import SentenceTransformer
            embedder = SentenceTransformer("all-MiniLM-L6-v2", local_files_only=key[4] != "1")
            client = chromadb.PersistentClient(path=_persist_dir())
            collection = client.get_or_create_collection("college_knowledge_v2", embedding_function=None)
            if (collection.metadata or {}).get("fingerprint") != fingerprint:
                ids = [item["id"] for item in chunks]
                old_ids = collection.get().get("ids", [])
                stale = list(set(old_ids) - set(ids))
                if stale:
                    collection.delete(ids=stale)
                docs = [item["text"] for item in chunks]
                collection.upsert(ids=ids, documents=docs,
                    metadatas=[{"source": item["source"]} for item in chunks],
                    embeddings=embedder.encode(docs, normalize_embeddings=True).tolist())
                collection.modify(metadata={"fingerprint": fingerprint})
            _collection, _embedder = collection, embedder
        except Exception:
            # Local lexical retrieval remains available without model downloads/network.
            _collection, _embedder = None, None
        return _collection


def retrieve(question, k=4):
    with _lock:
        ensure_vector_store()
        chosen = []
        if not chosen and _collection is not None:
            try:
                result = _collection.query(query_embeddings=_embedder.encode([question], normalize_embeddings=True).tolist(), n_results=min(k, _collection.count()))
                chosen = [{"text": doc, "source": meta["source"]} for doc, meta in zip(result["documents"][0], result["metadatas"][0])]
            except Exception:
                chosen = []
        if not chosen:
            terms = {word for word in re.findall(r"[a-z]+", question.lower()) if len(word) > 2}
            ranked = sorted(_chunks, key=lambda item: sum(term in item["text"].lower() for term in terms), reverse=True)
            chosen = [item for item in ranked if any(term in item["text"].lower() for term in terms)][:k]
        return {
            "context": "\n\n".join(f"[{item['source']}]\n{item['text']}" for item in chosen),
            "sources": list(dict.fromkeys(item["source"] for item in chosen)),
        }


def query(question, k=4):
    return retrieve(question, k)["context"]
