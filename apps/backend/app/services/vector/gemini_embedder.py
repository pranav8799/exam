import asyncio
import hashlib
from typing import List, Optional
import httpx

from app.config.logger import setup_logger
from app.config.settings import get_settings
from app.services.vector.provider_errors import ProviderFailure

logger = setup_logger("GeminiEmbedder")


def compute_sha256(text: Optional[str]) -> Optional[str]:
    """Compute SHA-256 hash of text for change detection."""
    if not text:
        return None
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class GeminiEmbedder:
    """Gemini Developer API embedder using normal (single-content) embedContent API."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, dim: Optional[int] = None):
        settings = get_settings()
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.model = model or settings.GEMINI_EMBEDDING_MODEL
        self.dim = dim or settings.GEMINI_EMBEDDING_DIM
        self.base_url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:embedContent"
        # The last request that failed for good (HTTP status + detail); search-index runs read it to
        # stop with a clear reason (quota, bad key, ...). Callers that ignore it behave as before.
        self.last_failure: Optional[ProviderFailure] = None

        if not self.api_key:
            logger.warning("GEMINI_API_KEY is not set. Embedding calls will fail until configured.")

    async def embed_text(
        self,
        text: str,
        client: Optional[httpx.AsyncClient] = None,
        max_retries: int = 5,
        base_backoff_sec: float = 2.0,
    ) -> Optional[List[float]]:
        """Generate a dense embedding vector using the normal (single-content) embedContent API."""
        clean_text = (text or "").strip()
        if not clean_text:
            return None

        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is required to generate embeddings.")

        url = f"{self.base_url}?key={self.api_key}"
        payload = {
            "content": {
                "parts": [{"text": clean_text}]
            },
            "output_dimensionality": self.dim,
        }

        should_close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=30.0)
            should_close_client = True

        try:
            for attempt in range(1, max_retries + 1):
                try:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        values = data.get("embedding", {}).get("values")
                        return values
                    elif resp.status_code == 429:
                        # Rate limit reached: exponential backoff
                        backoff = base_backoff_sec * (2 ** (attempt - 1))
                        logger.warning(
                            f"Gemini 429 Rate Limit hit. Backing off for {backoff:.1f}s (attempt {attempt}/{max_retries})"
                        )
                        await asyncio.sleep(backoff)
                    elif resp.status_code >= 500:
                        backoff = base_backoff_sec * attempt
                        logger.warning(
                            f"Gemini server error {resp.status_code}. Retrying in {backoff:.1f}s (attempt {attempt}/{max_retries})"
                        )
                        await asyncio.sleep(backoff)
                    else:
                        logger.error(f"Gemini embedContent failed: HTTP {resp.status_code} - {resp.text[:300]}")
                        self.last_failure = ProviderFailure("gemini", resp.status_code, resp.text[:300])
                        return None
                    last = ProviderFailure("gemini", resp.status_code, resp.text[:300])
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as net_err:
                    last = ProviderFailure("gemini", None, str(net_err))
                    backoff = base_backoff_sec * attempt
                    logger.warning(f"Network error calling Gemini: {net_err}. Retrying in {backoff:.1f}s")
                    await asyncio.sleep(backoff)

            logger.error(f"Failed to generate embedding after {max_retries} attempts.")
            self.last_failure = last
            return None
        finally:
            if should_close_client:
                await client.aclose()

    async def embed_texts_batch(
        self,
        texts: List[str],
        chunk_size: int = 100,
        max_retries: int = 5,
        base_backoff_sec: float = 2.0,
        fallback_concurrency: int = 8,
    ) -> List[Optional[List[float]]]:
        """Embed many texts with `batchEmbedContents` (up to 100 texts per request), in input order.

        Empty texts give None. If a whole chunk keeps failing, its texts are retried one by one with
        `embed_text`, so one bad text never loses the rest of the chunk.
        """
        results: List[Optional[List[float]]] = [None] * len(texts)
        todo = [(i, (t or "").strip()) for i, t in enumerate(texts)]
        todo = [(i, t) for i, t in todo if t]
        if not todo:
            return results
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is required to generate embeddings.")

        batch_url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:batchEmbedContents?key={self.api_key}"
        async with httpx.AsyncClient(timeout=120.0) as client:
            for start in range(0, len(todo), chunk_size):
                chunk = todo[start:start + chunk_size]
                payload = {
                    "requests": [
                        {"model": self.model, "content": {"parts": [{"text": t}]}, "output_dimensionality": self.dim}
                        for _, t in chunk
                    ]
                }
                vectors = None
                for attempt in range(1, max_retries + 1):
                    try:
                        resp = await client.post(batch_url, json=payload)
                    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as net_err:
                        self.last_failure = ProviderFailure("gemini", None, str(net_err))
                        logger.warning(f"Network error on batch embed: {net_err}. Retrying (attempt {attempt}/{max_retries})")
                        await asyncio.sleep(base_backoff_sec * attempt)
                        continue
                    if resp.status_code == 200:
                        embeddings = resp.json().get("embeddings", [])
                        if len(embeddings) == len(chunk):
                            vectors = [e.get("values") for e in embeddings]
                        else:
                            logger.error(f"Batch embed returned {len(embeddings)} vectors for {len(chunk)} texts")
                        break
                    self.last_failure = ProviderFailure("gemini", resp.status_code, resp.text[:300])
                    if resp.status_code == 429 or resp.status_code >= 500:
                        backoff = base_backoff_sec * (2 ** (attempt - 1))
                        logger.warning(f"Gemini batch embed HTTP {resp.status_code}. Backing off {backoff:.1f}s (attempt {attempt}/{max_retries})")
                        await asyncio.sleep(backoff)
                        continue
                    logger.error(f"Gemini batchEmbedContents failed: HTTP {resp.status_code} - {resp.text[:300]}")
                    break

                if vectors is None:
                    logger.warning(f"Batch of {len(chunk)} failed; embedding those texts one by one.")
                    semaphore = asyncio.Semaphore(fallback_concurrency)

                    async def _one(text_val: str) -> Optional[List[float]]:
                        async with semaphore:
                            return await self.embed_text(text_val, client=client)

                    vectors = await asyncio.gather(*[_one(t) for _, t in chunk])

                for (idx, _), vec in zip(chunk, vectors):
                    results[idx] = vec
        return results

    async def embed_texts_concurrent(
        self,
        texts: List[str],
        max_concurrency: int = 10,
    ) -> List[Optional[List[float]]]:
        """
        Embed a list of texts concurrently using individual normal embedContent API calls.
        Controlled by an asyncio.Semaphore to respect RPM limits.
        """
        semaphore = asyncio.Semaphore(max_concurrency)
        async with httpx.AsyncClient(timeout=45.0) as client:
            async def _worker(t: str) -> Optional[List[float]]:
                async with semaphore:
                    return await self.embed_text(t, client=client)

            tasks = [_worker(t) for t in texts]
            return await asyncio.gather(*tasks)

