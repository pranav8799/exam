import asyncio
from typing import Any, Dict, List, Optional
import httpx

from app.config.logger import setup_logger
from app.config.settings import get_settings
from app.services.vector.provider_errors import ProviderFailure, classify, missing_key

logger = setup_logger("JinaReranker")


class JinaReranker:
    """Client for Jina AI Reranker API (jina-reranker-v3.5).

    Default (live page views): on any failure the documents keep their input order, so a student
    still sees suggestions. `raise_on_error=True` (search-index runs): a failure raises
    `ProviderError` instead, so a precomputed list is never stored in an unreranked order.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, raise_on_error: bool = False):
        settings = get_settings()
        self.api_key = api_key or settings.JINA_API_KEY
        self.model = model or settings.JINA_RERANKER_MODEL
        self.endpoint = "https://api.jina.ai/v1/rerank"
        self.raise_on_error = raise_on_error
        # Search-index runs attach a TokenRateLimiter (rate_limiter.py) to pace calls under the key's limit
        self.limiter = None

        if not self.api_key:
            logger.warning("JINA_API_KEY is not set. Reranker calls will fail until configured.")

    @staticmethod
    def _input_order(documents: List[str], top_n: Optional[int]) -> List[Dict[str, Any]]:
        return [
            {"index": idx, "relevance_score": round(1.0 - (idx * 0.01), 3), "document": doc}
            for idx, doc in enumerate(documents[:top_n] if top_n else documents)
        ]

    async def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
        max_retries: Optional[int] = None,
        base_backoff_sec: float = 2.0,
    ) -> List[Dict[str, Any]]:
        """
        Rerank a list of documents against the query.
        Returns a list of dicts: [{'index': int, 'relevance_score': float, 'document': ...}, ...]
        sorted by relevance_score descending.
        """
        if not documents:
            return []
        if max_retries is None:
            # A page view must stay quick; a search-index run can wait out a rate-limit burst (~1 min)
            max_retries = 6 if self.raise_on_error else 3

        if not self.api_key:
            if self.raise_on_error:
                raise missing_key("jina")
            logger.error("JINA_API_KEY is missing. Cannot call Jina Reranker.")
            return self._input_order(documents, top_n)

        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            payload["top_n"] = top_n

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_failure: Optional[ProviderFailure] = None
        # Rough token count (query + documents + per-document overhead); corrected from Jina's usage
        estimate = (len(query) + sum(len(d) for d in documents)) // 3 + 20 * len(documents)
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(1, max_retries + 1):
                try:
                    if self.limiter is not None:
                        # Slot first, then the pace check: a request queued for the slot must still see
                        # a pause that started while it was waiting
                        async with self.limiter.slot():
                            await self.limiter.acquire(estimate)
                            resp = await client.post(self.endpoint, headers=headers, json=payload)
                    else:
                        resp = await client.post(self.endpoint, headers=headers, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        if self.limiter is not None:
                            self.limiter.record(estimate, int((data.get("usage") or {}).get("total_tokens") or 0))
                        return data.get("results", [])
                    last_failure = ProviderFailure("jina", resp.status_code, resp.text[:300])
                    if resp.status_code == 429 and "CONCURRENCY" in resp.text.upper():
                        # Too many requests in flight on this key (e.g. students' page views at the same
                        # moment): wait briefly; the token pace is not the problem
                        backoff = 1.0 * attempt
                        logger.warning(f"Jina concurrency limit; retrying in {backoff:.0f}s (attempt {attempt}/{max_retries})")
                        await asyncio.sleep(backoff)
                    elif resp.status_code == 429 and self.limiter is not None:
                        wait = self.limiter.rate_limited(resp.text)
                        logger.warning(
                            f"Jina 429 rate limit; pausing the run {wait:.0f}s and lowering its pace to "
                            f"{self.limiter.budget:,} tokens/min (attempt {attempt}/{max_retries}): {resp.text[:200]}"
                        )
                    elif resp.status_code == 429:
                        backoff = base_backoff_sec * (2 ** (attempt - 1))
                        logger.warning(
                            f"Jina 429 Rate Limit hit. Backing off for {backoff:.1f}s (attempt {attempt}/{max_retries})"
                        )
                        await asyncio.sleep(backoff)
                    elif resp.status_code >= 500:
                        backoff = base_backoff_sec * attempt
                        logger.warning(
                            f"Jina server error {resp.status_code}. Retrying in {backoff:.1f}s"
                        )
                        await asyncio.sleep(backoff)
                    else:
                        logger.error(f"Jina rerank failed: HTTP {resp.status_code} - {resp.text[:300]}")
                        break
                except (httpx.ConnectError, httpx.ReadTimeout) as net_err:
                    last_failure = ProviderFailure("jina", None, str(net_err))
                    backoff = base_backoff_sec * attempt
                    logger.warning(f"Network error calling Jina: {net_err}. Retrying in {backoff:.1f}s")
                    await asyncio.sleep(backoff)

        if self.raise_on_error and last_failure is not None:
            raise classify(last_failure)
        # Fallback on failure
        logger.warning("Jina rerank failed completely; returning original order as fallback.")
        return self._input_order(documents, top_n)
