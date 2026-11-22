import logging
from datetime import datetime, timezone
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models.site_settings import (
    DEFAULT_SIMILAR_QUESTIONS_LIMIT,
    DEFAULT_THEME,
    MAX_SIMILAR_QUESTIONS_LIMIT,
    MIN_SIMILAR_QUESTIONS_LIMIT,
    SINGLETON_ID,
    SiteSettings,
)
from app.schemas.site_settings import ALLOWED_THEMES

SIMILAR_LIMIT_CACHE_KEY = "ep:site_settings:similar_questions_limit"

logger = logging.getLogger(__name__)


class SiteSettingsService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _ensure_table_exists(self):
        try:
            await self.session.execute(
                text("""
                    CREATE TABLE IF NOT EXISTS site_settings (
                        id BIGINT PRIMARY KEY,
                        theme_name VARCHAR(50) NOT NULL DEFAULT 'crimson',
                        similar_questions_limit INTEGER NOT NULL DEFAULT 6,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_by_id BIGINT REFERENCES users(id) ON DELETE SET NULL
                    );
                """)
            )
            await self.session.execute(
                text("""
                    INSERT INTO site_settings (id, theme_name)
                    VALUES (1, 'crimson')
                    ON CONFLICT (id) DO NOTHING;
                """)
            )
            await self.session.commit()
        except Exception as e:
            logger.warning(f"Could not auto-create site_settings table: {e}")
            await self.session.rollback()

    async def get_theme(self) -> SiteSettings:
        from app.cache import get_cache
        cache = get_cache()
        cache_key = "ep:site_settings:theme"
        cached_data = await cache.get(cache_key)
        if cached_data is not None and isinstance(cached_data, dict):
            return SiteSettings(**cached_data)

        try:
            settings = await self.session.get(SiteSettings, SINGLETON_ID)
        except Exception as e:
            logger.warning(f"site_settings query failed (table may not exist yet): {e}. Running self-healing table setup...")
            await self.session.rollback()
            await self._ensure_table_exists()
            settings = await self.session.get(SiteSettings, SINGLETON_ID)

        if settings is None:
            await self._ensure_table_exists()
            try:
                settings = await self.session.get(SiteSettings, SINGLETON_ID)
            except Exception:
                await self.session.rollback()
                settings = None

            if settings is None:
                settings = SiteSettings(id=SINGLETON_ID, theme_name=DEFAULT_THEME)
                self.session.add(settings)
                await self.session.commit()
                await self.session.refresh(settings)

        if settings is not None:
            await cache.set(cache_key, settings.model_dump(mode="json"), ttl_seconds=3600)

        return settings

    async def get_similar_questions_limit(self) -> int:
        """How many similar questions students see per question. Falls back to the default on any error,
        so similar-question requests never fail because of this setting."""
        from app.cache import get_cache
        cache = get_cache()
        cached = await cache.get(SIMILAR_LIMIT_CACHE_KEY)
        if isinstance(cached, int):
            return cached
        try:
            row = (await self.session.execute(
                text("SELECT similar_questions_limit FROM site_settings WHERE id = :id"), {"id": SINGLETON_ID}
            )).first()
        except Exception as e:
            logger.warning(f"Could not read similar_questions_limit, using default: {e}")
            await self.session.rollback()
            row = None
        limit = int(row[0]) if row and row[0] else DEFAULT_SIMILAR_QUESTIONS_LIMIT
        limit = max(MIN_SIMILAR_QUESTIONS_LIMIT, min(MAX_SIMILAR_QUESTIONS_LIMIT, limit))
        await cache.set(SIMILAR_LIMIT_CACHE_KEY, limit, ttl_seconds=3600)
        return limit

    async def get_similar_questions_settings(self) -> dict:
        limit = await self.get_similar_questions_limit()
        try:
            row = (await self.session.execute(
                text("SELECT updated_at FROM site_settings WHERE id = :id"), {"id": SINGLETON_ID}
            )).first()
        except Exception:
            await self.session.rollback()
            row = None
        return {"similar_questions_limit": limit, "updated_at": row[0] if row else None}

    async def set_similar_questions_limit(self, limit: int, updated_by_id: int) -> dict:
        if not MIN_SIMILAR_QUESTIONS_LIMIT <= limit <= MAX_SIMILAR_QUESTIONS_LIMIT:
            raise ValueError(
                f"similar_questions_limit must be between {MIN_SIMILAR_QUESTIONS_LIMIT} and {MAX_SIMILAR_QUESTIONS_LIMIT}"
            )
        now = datetime.now(timezone.utc)
        await self.session.execute(
            text("""
                INSERT INTO site_settings (id, theme_name, similar_questions_limit, updated_at, updated_by_id)
                VALUES (:id, :theme_name, :limit, :updated_at, :updated_by_id)
                ON CONFLICT (id) DO UPDATE
                    SET similar_questions_limit = EXCLUDED.similar_questions_limit,
                        updated_at              = EXCLUDED.updated_at,
                        updated_by_id           = EXCLUDED.updated_by_id;
            """),
            {
                "id": SINGLETON_ID,
                "theme_name": DEFAULT_THEME,
                "limit": limit,
                "updated_at": now,
                "updated_by_id": updated_by_id,
            },
        )
        await self.session.commit()

        from app.cache.helpers import invalidate_site_settings_cache
        await invalidate_site_settings_cache()
        # Cached similar-question responses were built with the old count
        from app.cache import get_cache
        await get_cache().delete_pattern("*questions:similar*")
        return {"similar_questions_limit": limit, "updated_at": now}

    async def set_theme(self, theme_name: str, updated_by_id: int) -> SiteSettings:
        if theme_name not in ALLOWED_THEMES:
            raise ValueError(f"theme_name must be one of {sorted(ALLOWED_THEMES)}")

        now = datetime.now(timezone.utc)

        # Use a raw UPSERT so we never hit the unique-key violation on the
        # singleton row (id=1). This handles both "first write ever" and
        # "updating an existing row" in one atomic statement.
        await self.session.execute(
            text("""
                INSERT INTO site_settings (id, theme_name, updated_at, updated_by_id)
                VALUES (:id, :theme_name, :updated_at, :updated_by_id)
                ON CONFLICT (id) DO UPDATE
                    SET theme_name     = EXCLUDED.theme_name,
                        updated_at     = EXCLUDED.updated_at,
                        updated_by_id  = EXCLUDED.updated_by_id;
            """),
            {
                "id": SINGLETON_ID,
                "theme_name": theme_name,
                "updated_at": now,
                "updated_by_id": updated_by_id,
            },
        )
        await self.session.commit()

        from app.cache.helpers import invalidate_site_settings_cache
        await invalidate_site_settings_cache()

        # Return a fresh object reflecting the new state
        settings = await self.session.get(SiteSettings, SINGLETON_ID)
        if settings is None:
            settings = SiteSettings(id=SINGLETON_ID, theme_name=theme_name,
                                    updated_at=now, updated_by_id=updated_by_id)
        return settings
