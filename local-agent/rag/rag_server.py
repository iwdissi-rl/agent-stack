"""Local RAG over your documents: Qwen3-Embedding (llama-server :8182) + Qdrant (:6333).

Runs as a stdio MCP server for Hermes (tools: rag_ingest, rag_search, rag_list_sources,
rag_delete_source) and as a CLI for bulk work:

    .venv/bin/python rag_server.py ingest /mnt/data/local-agent/documents
    .venv/bin/python rag_server.py search "what does the contract say about termination"
    .venv/bin/python rag_server.py list

Only files under ALLOWED_ROOTS are readable. Paths the sandboxed agent sees as
/workspace/... are mapped to the host workspace directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams

ROOT = Path("/mnt/data/local-agent")
WORKSPACE = ROOT / "workspace"
ALLOWED_ROOTS = [ROOT / "documents", WORKSPACE]
EMBED_URL = os.environ.get("RAG_EMBED_URL", "http://127.0.0.1:8182/v1/embeddings")
QDRANT_URL = os.environ.get("RAG_QDRANT_URL", "http://127.0.0.1:6333")
DEFAULT_COLLECTION = "docs"
DIM = 1024
CHUNK_CHARS = 3000      # ~750 tokens
OVERLAP_CHARS = 450     # ~15% overlap
EMBED_BATCH = 16
# Qwen3-Embedding is instruction-aware on the query side only.
QUERY_PREFIX = "Instruct: Given a question, retrieve document passages that answer it\nQuery: "
TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".csv", ".json", ".yaml", ".yml", ".py", ".log", ".tex"}
SUPPORTED_EXT = TEXT_EXT | {".pdf", ".docx", ".html", ".htm"}

_client: QdrantClient | None = None


def qdrant() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL, timeout=60)
    return _client


def ensure_collection(name: str) -> None:
    if not qdrant().collection_exists(name):
        qdrant().create_collection(name, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
        qdrant().create_payload_index(name, "source", "keyword")


def resolve_path(p: str) -> Path:
    if p.startswith("/workspace"):
        p = str(WORKSPACE) + p[len("/workspace"):]
    path = Path(p).expanduser().resolve()
    if not any(path == r or r in path.parents for r in ALLOWED_ROOTS):
        raise ValueError(f"{path} is outside the allowed roots: {', '.join(map(str, ALLOWED_ROOTS))}")
    if not path.exists():
        raise ValueError(f"{path} does not exist")
    return path


def embed(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        r = httpx.post(EMBED_URL, json={"model": "embed", "input": texts[i:i + EMBED_BATCH]}, timeout=300)
        r.raise_for_status()
        out.extend(d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"]))
    return out


def read_pages(path: Path) -> list[tuple[int | None, str]]:
    """Return [(page_number or None, text)]; PDFs keep 1-based page numbers."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        return [(i + 1, page.extract_text() or "") for i, page in enumerate(PdfReader(str(path)).pages)]
    if ext == ".docx":
        import docx
        return [(None, "\n".join(p.text for p in docx.Document(str(path)).paragraphs))]
    if ext in (".html", ".htm"):
        from bs4 import BeautifulSoup
        return [(None, BeautifulSoup(path.read_text(errors="replace"), "html.parser").get_text("\n"))]
    return [(None, path.read_text(errors="replace"))]


def chunk_pages(pages: list[tuple[int | None, str]]) -> list[dict]:
    """Split page text on paragraph boundaries into ~CHUNK_CHARS windows with overlap.
    Chunks never span pages, so every chunk has exactly one page reference."""
    chunks = []
    for page, text in pages:
        text = re.sub(r"[ \t]+", " ", text).strip()
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(len(text), start + CHUNK_CHARS)
            if end < len(text):  # prefer to break at a paragraph, then a sentence
                cut = max(text.rfind("\n\n", start + CHUNK_CHARS // 2, end), text.rfind(". ", start + CHUNK_CHARS // 2, end))
                if cut > start:
                    end = cut + 1
            chunks.append({"page": page, "text": text[start:end].strip()})
            if end >= len(text):
                break
            start = max(end - OVERLAP_CHARS, start + 1)
    return chunks


def delete_source(source: str, collection: str) -> None:
    if qdrant().collection_exists(collection):
        qdrant().delete(collection, points_selector=Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))]))


def ingest_file(path: Path, collection: str) -> dict:
    pages = read_pages(path)
    chunks = chunk_pages(pages)
    if not chunks:
        return {"source": str(path), "chunks": 0, "note": "no extractable text (scanned PDF? needs OCR)"}
    source = str(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    delete_source(source, collection)  # re-ingest replaces old chunks
    vectors = embed([c["text"] for c in chunks])
    points = [
        PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}#{digest}#{i}")),
            vector=v,
            payload={"source": source, "file": path.name, "page": c["page"], "chunk": i, "text": c["text"], "sha": digest},
        )
        for i, (c, v) in enumerate(zip(chunks, vectors))
    ]
    for i in range(0, len(points), 256):
        qdrant().upsert(collection, points[i:i + 256], wait=True)
    n_pages = sum(1 for p, _ in pages if p is not None) or None
    return {"source": source, "chunks": len(points), "pages": n_pages}


def rag_ingest_impl(path: str, collection: str = DEFAULT_COLLECTION) -> dict:
    target = resolve_path(path)
    ensure_collection(collection)
    files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXT)
    results, errors = [], []
    for f in files:
        try:
            results.append(ingest_file(f, collection))
        except Exception as exc:  # keep going; report per-file failures
            errors.append({"source": str(f), "error": f"{type(exc).__name__}: {exc}"})
    count = qdrant().count(collection, exact=True).count
    return {"collection": collection, "ingested": results, "errors": errors, "collection_total_chunks": count}


def rag_search_impl(query: str, k: int = 6, collection: str = DEFAULT_COLLECTION, source: str = "") -> dict:
    if not qdrant().collection_exists(collection):
        return {"results": [], "note": f"collection '{collection}' is empty; ingest documents first"}
    flt = None
    if source:
        flt = Filter(must=[FieldCondition(key="source", match=MatchValue(value=str(resolve_path(source))))])
    vec = embed([QUERY_PREFIX + query])[0]
    hits = qdrant().query_points(collection, query=vec, limit=max(1, min(int(k), 20)), query_filter=flt, with_payload=True).points
    return {"results": [
        {"score": round(h.score, 4), "file": h.payload["file"], "source": h.payload["source"],
         "page": h.payload.get("page"), "chunk": h.payload.get("chunk"), "text": h.payload["text"]}
        for h in hits
    ]}


def rag_list_impl(collection: str = DEFAULT_COLLECTION) -> dict:
    if not qdrant().collection_exists(collection):
        return {"collection": collection, "sources": []}
    counts: dict[str, int] = {}
    offset = None
    while True:
        pts, offset = qdrant().scroll(collection, limit=1024, offset=offset, with_payload=["source"], with_vectors=False)
        for p in pts:
            counts[p.payload["source"]] = counts.get(p.payload["source"], 0) + 1
        if offset is None:
            break
    return {"collection": collection, "sources": [{"source": s, "chunks": n} for s, n in sorted(counts.items())]}


def build_server():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name="local-rag", instructions=(
        "Local document retrieval. rag_search returns passages with file and page numbers; cite them. "
        "Documents live in /workspace or /mnt/data/local-agent/documents; ingest before searching."))

    @server.tool()
    def rag_ingest(path: str, collection: str = DEFAULT_COLLECTION) -> str:
        """Index a file or a whole directory (pdf, docx, html, md, txt, code) into the local vector store.
        Re-ingesting a file replaces its old chunks. Paths: /workspace/... or /mnt/data/local-agent/documents/...
        Returns per-file chunk counts and errors — check them; 0 chunks means no extractable text."""
        return json.dumps(rag_ingest_impl(path, collection))

    @server.tool()
    def rag_search(query: str, k: int = 6, collection: str = DEFAULT_COLLECTION, source: str = "") -> str:
        """Semantic search over ingested documents. Returns the top-k passages with score, file, page and text.
        Set `source` to a file path to search within one document only."""
        return json.dumps(rag_search_impl(query, k, collection, source))

    @server.tool()
    def rag_list_sources(collection: str = DEFAULT_COLLECTION) -> str:
        """List the documents currently indexed and how many chunks each has. Use it to verify an ingest worked."""
        return json.dumps(rag_list_impl(collection))

    @server.tool()
    def rag_delete_source(source: str, collection: str = DEFAULT_COLLECTION) -> str:
        """Remove one document's chunks from the index."""
        src = str(resolve_path(source)) if Path(source.replace("/workspace", str(WORKSPACE), 1)).exists() else source
        delete_source(src, collection)
        return json.dumps(rag_list_impl(collection))

    return server


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "serve":
        build_server().run("stdio")
    elif args[0] == "ingest":
        print(json.dumps(rag_ingest_impl(args[1], *(args[2:3] or [DEFAULT_COLLECTION])), indent=2))
    elif args[0] == "search":
        print(json.dumps(rag_search_impl(" ".join(args[1:])), indent=2))
    elif args[0] == "list":
        print(json.dumps(rag_list_impl(*(args[1:2] or [DEFAULT_COLLECTION])), indent=2))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
