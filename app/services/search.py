from __future__ import annotations

import re
from collections import Counter
from typing import Any

from app import db
from app.services import vector_store
from app.settings import KEYWORD_WEIGHT, TOP_K, VECTOR_WEIGHT


SEARCH_MODES = {"keyword", "vector", "hybrid"}


def normalize_search_mode(search_mode: str | None) -> str:
    mode = (search_mode or "hybrid").strip().lower()
    return mode if mode in SEARCH_MODES else "hybrid"


def tokenize(text: str) -> list[str]:
    return vector_store.tokenize(text)


def keyword_score(query: str, content: str) -> float:
    query_tokens = tokenize(query)
    if not query_tokens:
        return 0.0
    content_tokens = Counter(tokenize(content))
    matched = sum(1 for token in query_tokens if content_tokens[token] > 0)
    exact_bonus = 0.25 if query in content else 0.0
    return min(1.0, matched / len(set(query_tokens)) + exact_bonus)


def chunk_text(content: str, size: int = 450, overlap: int = 60) -> list[str]:
    content = re.sub(r"\r\n?", "\n", content).strip()
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", content) if part.strip()]
    chunks: list[str] = []
    for paragraph in paragraphs or [content]:
        if len(paragraph) <= size:
            chunks.append(paragraph)
            continue
        start = 0
        while start < len(paragraph):
            chunks.append(paragraph[start : start + size].strip())
            start += max(1, size - overlap)
    return [chunk for chunk in chunks if chunk]


def _filters(
    case_cause: str = "",
    region: str = "",
    year: str = "",
    document_type: str = "",
) -> tuple[list[str], list[Any], dict[str, Any]]:
    sql_filters: list[str] = ["d.status = 'published'"]
    params: list[Any] = []
    chroma_filters: list[dict[str, Any]] = [{"status": "published"}]
    if case_cause and case_cause != "全部":
        sql_filters.append("d.case_cause = ?")
        params.append(case_cause)
        chroma_filters.append({"case_cause": case_cause})
    if region and region != "全部":
        sql_filters.append("d.region = ?")
        params.append(region)
        chroma_filters.append({"region": region})
    if year and year != "全部":
        year_value = int(year)
        sql_filters.append("d.year = ?")
        params.append(year_value)
        chroma_filters.append({"year": year_value})
    if document_type and document_type != "全部":
        sql_filters.append("d.document_type = ?")
        params.append(document_type)
        chroma_filters.append({"document_type": document_type})
    where = chroma_filters[0] if len(chroma_filters) == 1 else {"$and": chroma_filters}
    return sql_filters, params, where


def _db_chunks(sql_filters: list[str], params: list[Any]) -> list[dict[str, Any]]:
    sql = f"""
        SELECT c.id AS chunk_id, c.content, c.section_name, c.paragraph_no,
               d.id AS document_id, d.title, d.document_type, d.case_cause,
               d.region, d.year, d.source_name, d.validity_status, d.validity_date, d.validity_note
        FROM document_chunks c
        JOIN legal_documents d ON d.id = c.document_id
        WHERE {' AND '.join(sql_filters)}
    """
    with db.connect() as conn:
        return db.rows_to_dicts(conn.execute(sql, params).fetchall())


def _enrich_from_db(chunk_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    with db.connect() as conn:
        rows = db.rows_to_dicts(
            conn.execute(
                f"""
                SELECT c.id AS chunk_id, c.content, c.section_name, c.paragraph_no,
                       d.id AS document_id, d.title, d.document_type, d.case_cause,
                       d.region, d.year, d.source_name, d.validity_status, d.validity_date, d.validity_note
                FROM document_chunks c
                JOIN legal_documents d ON d.id = c.document_id
                WHERE c.id IN ({placeholders}) AND d.status = 'published'
                """,
                chunk_ids,
            ).fetchall()
        )
    return {int(row["chunk_id"]): row for row in rows}


def search(
    query: str,
    case_cause: str = "",
    region: str = "",
    year: str = "",
    document_type: str = "",
    search_mode: str = "hybrid",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    limit = limit or db.get_int_config("top_k", TOP_K)
    keyword_weight = db.get_float_config("keyword_weight", KEYWORD_WEIGHT)
    vector_weight = db.get_float_config("vector_weight", VECTOR_WEIGHT)
    mode = normalize_search_mode(search_mode)
    sql_filters, params, chroma_where = _filters(case_cause, region, year, document_type)

    scored: dict[int, dict[str, Any]] = {}
    if mode in {"keyword", "hybrid"}:
        for row in _db_chunks(sql_filters, params):
            chunk_id = int(row["chunk_id"])
            k_score = keyword_score(query, row["content"] + row["title"] + (row["case_cause"] or ""))
            if k_score > 0:
                scored[chunk_id] = row | {"keyword_score": round(k_score, 4), "vector_score": 0.0}

    if mode in {"vector", "hybrid"}:
        vector_limit = limit if mode == "vector" else max(limit * 3, 12)
        vector_hits = vector_store.query(query, where=chroma_where, limit=vector_limit)
        hit_ids = [int(hit["metadata"]["chunk_id"]) for hit in vector_hits if hit.get("metadata", {}).get("chunk_id")]
        db_rows = _enrich_from_db(hit_ids)
        for hit in vector_hits:
            metadata = hit.get("metadata") or {}
            chunk_id = metadata.get("chunk_id")
            if not chunk_id:
                continue
            chunk_id = int(chunk_id)
            row = scored.get(chunk_id) or db_rows.get(chunk_id)
            if not row:
                continue
            row["vector_score"] = max(float(row.get("vector_score", 0.0)), float(hit["vector_score"]))
            row.setdefault("keyword_score", round(keyword_score(query, row["content"] + row["title"] + (row["case_cause"] or "")), 4))
            scored[chunk_id] = row

    results = []
    for row in scored.values():
        keyword_value = float(row.get("keyword_score", 0.0))
        vector_value = float(row.get("vector_score", 0.0))
        if mode == "keyword":
            score = keyword_value
        elif mode == "vector":
            score = vector_value
        else:
            score = keyword_weight * keyword_value + vector_weight * vector_value
        if score <= 0:
            continue
        row["relevance_score"] = round(score, 4)
        row["match_mode"] = mode
        row["snippet"] = row["content"][:160] + ("..." if len(row["content"]) > 160 else "")
        results.append(row)

    results.sort(key=lambda item: item["relevance_score"], reverse=True)
    return results[:limit]
