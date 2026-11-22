"""Errors from the paid AI providers (Gemini embeddings, Jina reranker), with a plain-language reason.

A search-index run stops on these and records `code` + `message` so an admin can see why, fix it
(add credits, replace a key) and start the run again; it then resumes where it stopped.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class ProviderFailure:
    provider: str          # "gemini" | "jina"
    status: Optional[int]  # HTTP status, None for network errors
    detail: str


class ProviderError(Exception):
    def __init__(self, code: str, message: str, failure: Optional[ProviderFailure] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.failure = failure


def classify(failure: ProviderFailure) -> ProviderError:
    """Turn an HTTP failure into a stable error code and a message an admin can act on."""
    name = "Gemini" if failure.provider == "gemini" else "Jina"
    key = "GEMINI_API_KEY" if failure.provider == "gemini" else "JINA_API_KEY"
    s = failure.status
    if s is None:
        return ProviderError(f"{failure.provider}_unreachable",
                             f"{name} could not be reached (network error). Check the internet connection and try again.", failure)
    if s in (401, 403):
        return ProviderError(f"{failure.provider}_auth",
                             f"{name} rejected the API key (HTTP {s}). Check {key} in the backend .env.", failure)
    if s == 402:
        return ProviderError(f"{failure.provider}_no_credits",
                             f"{name} account has no credits left (HTTP 402). Add credits, then start the run again; it resumes where it stopped.", failure)
    if s == 429 and "concurrency" in (failure.detail or "").lower():
        return ProviderError(f"{failure.provider}_concurrency",
                             f"{name} refused the request: too many requests at the same time on this API key (HTTP 429, "
                             f"concurrency limit). Wait a moment and start the run again; it resumes where it stopped. "
                             f"A higher {name} plan allows more parallel requests.", failure)
    if s == 429:
        return ProviderError(f"{failure.provider}_quota",
                             f"{name} rate limit or quota exhausted (HTTP 429) even after retries. Wait or raise the quota, then start the run again; it resumes where it stopped.", failure)
    if s >= 500:
        return ProviderError(f"{failure.provider}_server",
                             f"{name} had a server error (HTTP {s}). Try again later; the run resumes where it stopped.", failure)
    return ProviderError(f"{failure.provider}_http_{s}", f"{name} request failed (HTTP {s}): {failure.detail[:200]}", failure)


def missing_key(provider: str) -> ProviderError:
    name, key = ("Gemini", "GEMINI_API_KEY") if provider == "gemini" else ("Jina", "JINA_API_KEY")
    return ProviderError(f"{provider}_no_key", f"{key} is not set in the backend .env, so {name} cannot be used.")
