"""Local BM25 + ONNX embeddings + RRF. No task text is sent to an embedding API."""

import collections
import math
import os
import re
from pathlib import Path

from .execution import digest

FIELDS = ("summary", "failure", "remedy", "applicability")


def lesson_text(data):
    return "\n".join(str(data.get(k, "")) for k in FIELDS)


def tokenize(text):
    text = text.lower()
    tokens = re.findall(r"[a-z_][a-z_0-9]*|[0-9]+", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            tokens.append(run)
    return tokens


def bm25(query, documents):
    counts = [collections.Counter(tokenize(d)) for d in documents]
    lengths = [sum(c.values()) for c in counts]
    avg = sum(lengths) / max(1, len(lengths)) or 1
    scores = [0.0] * len(documents)
    for word in set(tokenize(query)):
        df = sum(word in c for c in counts)
        idf = math.log(1 + (len(counts) - df + 0.5) / (df + 0.5))
        for i, c in enumerate(counts):
            tf = c[word]
            if tf:
                scores[i] += (
                    idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * lengths[i] / avg))
                )
    return scores


def normalize(vector):
    if not isinstance(vector, list) or not vector or len(vector) > 32768:
        raise ValueError("Invalid embedding shape")
    if any(
        isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x)
        for x in vector
    ):
        raise ValueError("Invalid embedding values")
    norm = math.sqrt(sum(x * x for x in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Invalid embedding norm")
    return [x / norm for x in vector]


class LocalEmbedding:
    def __init__(self):
        self.model_name = "BAAI/bge-small-zh-v1.5"
        self.cache = Path(
            os.getenv(
                "REPO_AGENT_MEMORY_MODEL_DIR",
                str(Path(__file__).resolve().parents[2] / ".runtime/memory-model"),
            )
        )
        self.identity = digest([self.model_name, "fastembed-0.9.1", "local-v1"])
        self.model = None

    def embed(self, texts, query=False):
        if self.model is None:
            from fastembed import TextEmbedding

            self.model = TextEmbedding(
                self.model_name,
                cache_dir=str(self.cache),
                local_files_only=True,
                threads=2,
            )
        values = (
            self.model.query_embed(texts)
            if query
            else self.model.embed(texts, batch_size=16)
        )
        return [normalize(v.tolist()) for v in values]


class HybridMemory:
    def __init__(self, embedding=None):
        self.embedding = embedding or LocalEmbedding()
        self.query_cache = collections.OrderedDict()

    def rank(self, query, rows, save_cache):
        # Latest text gets precedence if accumulated requests exceed this bound.
        query = query[-16000:]
        texts = [lesson_text(r["data"]) for r in rows]
        lexical = bm25(query, texts)
        report = {"mode": "bm25", "candidates": len(rows), "embedding_error": None}
        dense = [0.0] * len(rows)
        if rows:
            try:
                qkey = digest([self.embedding.identity, query])
                q = self.query_cache.get(qkey)
                if q is None:
                    q = normalize(self.embedding.embed([query], query=True)[0])
                    self.query_cache[qkey] = q
                    if len(self.query_cache) > 128:
                        self.query_cache.popitem(last=False)
                vectors = [None] * len(rows)
                missing = []
                for i, row in enumerate(rows):
                    cache = row["data"].get("_embedding", {})
                    if cache.get("identity") == self.embedding.identity and cache.get(
                        "text_hash"
                    ) == digest(texts[i]):
                        try:
                            v = normalize(cache["vector"])
                            if len(v) == len(q):
                                vectors[i] = v
                        except (ValueError, KeyError, TypeError):
                            pass
                    if vectors[i] is None:
                        missing.append(i)
                if missing:
                    values = self.embedding.embed([texts[i] for i in missing])
                    if len(values) != len(missing):
                        raise ValueError("Invalid batch length")
                    for i, v in zip(missing, values):
                        v = normalize(v)
                        if len(v) != len(q):
                            raise ValueError("Embedding dimensions changed")
                        vectors[i] = v
                        save_cache(
                            rows[i],
                            dict(
                                identity=self.embedding.identity,
                                text_hash=digest(texts[i]),
                                vector=v,
                            ),
                        )
                dense = [sum(a * b for a, b in zip(q, v)) for v in vectors]
                report["mode"] = "hybrid_rrf"
            except Exception as exc:
                report["embedding_error"] = type(exc).__name__
                dense = [0.0] * len(rows)
        floor = float(os.getenv("REPO_AGENT_MEMORY_MIN_COSINE", "0.6"))
        if not math.isfinite(floor) or not -1 <= floor <= 1:
            raise ValueError("Invalid cosine floor")
        sparse_order = sorted(
            (i for i, v in enumerate(lexical) if v > 0), key=lambda i: -lexical[i]
        )[:10]
        dense_order = sorted(
            (
                i
                for i, v in enumerate(dense)
                if report["mode"] == "hybrid_rrf" and v >= floor
            ),
            key=lambda i: -dense[i],
        )[:10]
        fused = {}
        for ranking in (sparse_order, dense_order):
            for rank, i in enumerate(ranking, 1):
                fused[i] = fused.get(i, 0) + 1 / (60 + rank)
        selected = sorted(fused, key=lambda i: -fused[i])[:3]
        report["selected"] = [
            dict(
                id=rows[i]["id"],
                bm25=lexical[i],
                cosine=dense[i] if report["mode"] == "hybrid_rrf" else None,
                rrf=fused[i],
            )
            for i in selected
        ]
        return [rows[i] for i in selected], report
