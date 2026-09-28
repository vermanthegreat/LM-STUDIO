"""Local embedding provider boundary (Phase K1).

Embeddings are disabled by default. When enabled, text is sent only to the
configured OpenAI-compatible ``/embeddings`` endpoint (LM Studio serves one at
``LMSTUDIO_BASE_URL``). Non-loopback endpoints are refused unless explicitly
allowed, so knowledge content is never sent off-machine by accident.

Every provider response is validated; any defect raises ``EmbeddingError`` with
a stable code. Callers fall back to lexical retrieval instead of trusting a
partial or malformed result.
"""

from __future__ import annotations

import logging
import math
from array import array
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class EmbeddingError(Exception):
    """Provider or validation failure with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# Error codes that mean "the provider is not usable right now" (as opposed to
# a defect in one specific input). Indexing stops the run on these instead of
# retrying chunk by chunk.
PROVIDER_UNAVAILABLE_CODES = frozenset(
    {"embedding_timeout", "embedding_unreachable", "embedding_http_error", "embedding_disabled"}
)


class EmbeddingProvider(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def validate_vector(raw: Any) -> list[float]:
    if not isinstance(raw, list) or not raw:
        raise EmbeddingError("embedding_empty_vector", "Provider returned an empty or non-list vector.")
    out: list[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingError("embedding_non_numeric", "Provider returned a non-numeric vector component.")
        number = float(value)
        if not math.isfinite(number):
            raise EmbeddingError("embedding_non_numeric", "Provider returned a non-finite vector component.")
        out.append(number)
    return out


def validate_batch(raw_vectors: list[Any], expected_count: int) -> list[list[float]]:
    if len(raw_vectors) != expected_count:
        raise EmbeddingError(
            "embedding_count_mismatch",
            f"Provider returned {len(raw_vectors)} vectors for {expected_count} inputs.",
        )
    vectors = [validate_vector(v) for v in raw_vectors]
    dims = {len(v) for v in vectors}
    if len(dims) > 1:
        raise EmbeddingError("embedding_dimension_inconsistent", "Provider returned vectors of differing dimensions.")
    return vectors


def l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        raise EmbeddingError("embedding_zero_vector", "Zero vector cannot be used for cosine similarity.")
    return [x / norm for x in vector]


def pack_vector(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def unpack_vector(blob: bytes) -> array:
    values = array("f")
    values.frombytes(blob)
    return values


class OpenAICompatibleEmbeddingProvider:
    """POST {base_url}/embeddings with {"model", "input": [...]}."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout: float,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.endpoint = base_url.rstrip("/") + "/embeddings"
        self.model = model
        self.timeout = timeout
        self._transport = transport

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": self.model, "input": texts}
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                resp = client.post(self.endpoint, json=payload)
        except httpx.TimeoutException:
            raise EmbeddingError("embedding_timeout", "Embedding request timed out.") from None
        except httpx.HTTPError as exc:
            raise EmbeddingError("embedding_unreachable", f"Embedding endpoint unreachable ({type(exc).__name__}).") from None
        if resp.status_code >= 400:
            raise EmbeddingError("embedding_http_error", f"Embedding endpoint returned HTTP {resp.status_code}.")
        try:
            body = resp.json()
        except ValueError:
            raise EmbeddingError("embedding_malformed_response", "Embedding response is not JSON.") from None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise EmbeddingError("embedding_malformed_response", "Embedding response has no 'data' list.")
        if not all(isinstance(entry, dict) and "embedding" in entry for entry in data):
            raise EmbeddingError("embedding_malformed_response", "Embedding entries lack 'embedding'.")
        if all(isinstance(entry.get("index"), int) for entry in data):
            indexes = [entry["index"] for entry in data]
            if sorted(indexes) != list(range(len(data))):
                raise EmbeddingError("embedding_malformed_response", "Embedding indexes are not a permutation.")
            data = sorted(data, key=lambda entry: entry["index"])
        return validate_batch([entry["embedding"] for entry in data], len(texts))


@dataclass(frozen=True)
class EmbeddingSettings:
    enabled: bool = False
    base_url: str = "http://localhost:1234/v1"
    model: Optional[str] = None
    timeout: float = 30.0
    batch_size: int = 16
    allow_remote: bool = False
    embed_on_ingest: bool = True
    min_score: float = 0.25
    max_candidates: int = 5000


@dataclass
class EmbeddingRuntime:
    """Resolved embedding configuration plus provider (None when disabled)."""

    settings: EmbeddingSettings
    provider: Optional[EmbeddingProvider] = None
    disabled_reason: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.provider is not None

    @property
    def model(self) -> Optional[str]:
        return self.provider.model if self.provider is not None else self.settings.model

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "disabled_reason": self.disabled_reason,
            "batch_size": self.settings.batch_size,
            "min_score": self.settings.min_score,
        }


def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def build_embedding_runtime(
    settings: EmbeddingSettings,
    *,
    provider: Optional[EmbeddingProvider] = None,
) -> EmbeddingRuntime:
    """Resolve settings into a runtime. ``provider`` is an injection point for tests."""
    if not settings.enabled:
        return EmbeddingRuntime(settings=settings, disabled_reason="embeddings_disabled")
    if provider is not None:
        return EmbeddingRuntime(settings=settings, provider=provider)
    if not settings.model:
        return EmbeddingRuntime(settings=settings, disabled_reason="embedding_model_not_configured")
    if not _is_loopback(settings.base_url) and not settings.allow_remote:
        return EmbeddingRuntime(settings=settings, disabled_reason="embedding_remote_endpoint_not_allowed")
    return EmbeddingRuntime(
        settings=settings,
        provider=OpenAICompatibleEmbeddingProvider(
            base_url=settings.base_url,
            model=settings.model,
            timeout=settings.timeout,
        ),
    )


def settings_from_config(cfg: Any) -> EmbeddingSettings:
    return EmbeddingSettings(
        enabled=cfg.knowledge_embeddings_enabled,
        base_url=cfg.knowledge_embedding_base_url or cfg.lmstudio_base_url,
        model=cfg.knowledge_embedding_model,
        timeout=cfg.knowledge_embedding_timeout,
        batch_size=max(1, cfg.knowledge_embedding_batch_size),
        allow_remote=cfg.knowledge_embedding_allow_remote,
        embed_on_ingest=cfg.knowledge_embed_on_ingest,
        min_score=cfg.knowledge_semantic_min_score,
        max_candidates=max(1, cfg.knowledge_semantic_max_candidates),
    )


_UNCONFIGURED = EmbeddingRuntime(settings=EmbeddingSettings(), disabled_reason="embedding_runtime_not_configured")


def attach_embedding_runtime(store: Any, runtime: EmbeddingRuntime) -> None:
    """Make the runtime reachable from code paths that only receive the store (tools, /ask)."""
    store.knowledge_embedding_runtime = runtime


def get_embedding_runtime(store: Any) -> EmbeddingRuntime:
    """Runtime attached by ``create_app``; unattached stores are embedding-disabled (never env-driven)."""
    runtime = getattr(store, "knowledge_embedding_runtime", None)
    return runtime if isinstance(runtime, EmbeddingRuntime) else _UNCONFIGURED
