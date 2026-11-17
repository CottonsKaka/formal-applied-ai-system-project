"""Small, local retrieval helper for PawPal+ knowledge-grounded responses."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DocumentChunk:
    source: str
    text: str


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _chunk_text(source: str, text: str) -> list[DocumentChunk]:
    chunks = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = " ".join(paragraph.split())
        if paragraph:
            chunks.append(DocumentChunk(source=source, text=paragraph))
    return chunks


def load_chunks(directory: str | Path) -> list[DocumentChunk]:
    """Load Markdown or text files as paragraph-sized chunks."""
    directory = Path(directory)
    chunks: list[DocumentChunk] = []
    if not directory.exists():
        return chunks

    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in {".md", ".txt"}:
            chunks.extend(_chunk_text(path.name, path.read_text(encoding="utf-8")))
    return chunks


def cosine_similarity(left: dict[str, int], right: dict[str, int]) -> float:
    dot = sum(left.get(token, 0) * count for token, count in right.items())
    left_norm = math.sqrt(sum(count * count for count in left.values()))
    right_norm = math.sqrt(sum(count * count for count in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


class PetCareKnowledgeBase:
    """A dependency-free retriever using term-frequency cosine similarity."""

    def __init__(self, directory: str | Path):
        self.chunks = load_chunks(directory)

    def retrieve(self, query: str, top_k: int = 3) -> list[DocumentChunk]:
        if not query.strip() or not self.chunks:
            return []

        query_counts: dict[str, int] = {}
        for token in _tokens(query):
            query_counts[token] = query_counts.get(token, 0) + 1

        ranked = []
        for chunk in self.chunks:
            chunk_counts: dict[str, int] = {}
            for token in _tokens(chunk.text):
                chunk_counts[token] = chunk_counts.get(token, 0) + 1
            ranked.append((cosine_similarity(query_counts, chunk_counts), chunk))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [chunk for score, chunk in ranked[:top_k] if score > 0]


def format_retrieved_context(chunks: list[DocumentChunk]) -> str:
    if not chunks:
        return "No relevant local pet-care guidance was found."
    return "\n\n".join(f"Source: {chunk.source}\n{chunk.text}" for chunk in chunks)
