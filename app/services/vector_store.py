from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from collections import Counter
from http import HTTPStatus
from typing import Any

from app import db
from app.services.config_store import dashscope_api_key, embedding_model, use_dashscope_embedding
from app.settings import CHROMA_COLLECTION, CHROMA_PATH, EMBEDDING_MODEL_NAME, USE_DASHSCOPE_EMBEDDING


logger = logging.getLogger(__name__)


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", text.lower())
    bigrams = [text[i : i + 2] for i in range(max(0, len(text) - 1)) if re.match(r"[\u4e00-\u9fff]{2}", text[i : i + 2])]
    legal_terms = re.findall(r"合同|借贷|利息|违约|赔偿|劳动|解除|交通事故|离婚|财产|证据|引用|货款|尾款|侵权|责任|法规|法条", text)
    return words + bigrams + legal_terms


def local_vectorize(text: str, dims: int = 128) -> list[float]:
    vector = [0.0] * dims
    for token in tokenize(text):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:2], "big") % dims
        sign = 1 if digest[2] % 2 == 0 else -1
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def dashscope_embedding(text: str) -> list[float] | None:
    api_key = dashscope_api_key()
    if not use_dashscope_embedding() or not api_key:
        return None
    try:
        import dashscope

        response = dashscope.TextEmbedding.call(
            model=embedding_model(),
            input=text[:6000],
            api_key=api_key,
        )
        if response.status_code != HTTPStatus.OK:
            return None
        embeddings = response.output.get("embeddings") or []
        if not embeddings:
            return None
        return embeddings[0]["embedding"]
    except Exception:
        logger.warning("DashScope embedding failed; falling back to local vectors.", exc_info=True)
        return None


def embed_text(text: str) -> list[float]:
    return dashscope_embedding(text) or local_vectorize(text)


def _client():
    import chromadb

    return chromadb.PersistentClient(path=str(CHROMA_PATH))


def collection():
    return _client().get_or_create_collection(
        name=CHROMA_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


def chunk_chroma_id(chunk_id: int) -> str:
    return f"chunk-{chunk_id}"


def upsert_chunk(chunk_id: int, content: str, metadata: dict[str, Any]) -> None:
    safe_metadata = {key: "" if value is None else value for key, value in metadata.items()}
    collection().upsert(
        ids=[chunk_chroma_id(chunk_id)],
        documents=[content],
        embeddings=[embed_text(content)],
        metadatas=[safe_metadata],
    )


def delete_chunks(chunk_ids: list[int]) -> None:
    if not chunk_ids:
        return
    collection().delete(ids=[chunk_chroma_id(chunk_id) for chunk_id in chunk_ids])


def delete_document(document_id: int) -> None:
    collection().delete(where={"document_id": document_id})


def query(query_text: str, where: dict[str, Any] | None = None, limit: int = 6) -> list[dict[str, Any]]:
    results = collection().query(
        query_embeddings=[embed_text(query_text)],
        n_results=limit,
        where=where or None,
        include=["documents", "metadatas", "distances"],
    )
    rows: list[dict[str, Any]] = []
    ids = results.get("ids", [[]])[0]
    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]
    for item_id, document, metadata, distance in zip(ids, documents, metadatas, distances):
        rows.append(
            {
                "id": item_id,
                "content": document,
                "metadata": metadata or {},
                "vector_score": round(max(0.0, 1.0 - float(distance)), 4),
            }
        )
    return rows


def reset_collection() -> None:
    client = _client()
    try:
        client.delete_collection(CHROMA_COLLECTION)
    except Exception:
        pass
    client.get_or_create_collection(name=CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"})


def list_chunk_ids() -> set[int]:
    data = collection().get(include=["metadatas"])
    chunk_ids: set[int] = set()
    for item_id, metadata in zip(data.get("ids", []), data.get("metadatas", [])):
        chunk_id = (metadata or {}).get("chunk_id")
        if chunk_id is None and isinstance(item_id, str) and item_id.startswith("chunk-"):
            chunk_id = item_id.removeprefix("chunk-")
        try:
            chunk_ids.add(int(chunk_id))
        except (TypeError, ValueError):
            continue
    return chunk_ids


def count() -> int:
    return collection().count()
