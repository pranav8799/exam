from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models.site_settings import MAX_SIMILAR_QUESTIONS_LIMIT, MIN_SIMILAR_QUESTIONS_LIMIT

ALLOWED_THEMES = {"crimson", "ocean", "forest", "royal"}


class SimilarQuestionsSettingsOut(BaseModel):
    """How many similar questions students see per question (admin dashboard setting)."""

    similar_questions_limit: int
    min_limit: int = MIN_SIMILAR_QUESTIONS_LIMIT
    max_limit: int = MAX_SIMILAR_QUESTIONS_LIMIT
    updated_at: Optional[datetime] = None


class SimilarQuestionsSettingsUpdate(BaseModel):
    similar_questions_limit: int = Field(ge=MIN_SIMILAR_QUESTIONS_LIMIT, le=MAX_SIMILAR_QUESTIONS_LIMIT)


class ThemeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    theme_name: str
    updated_at: datetime


class ThemeUpdatePayload(BaseModel):
    theme_name: str

    @field_validator("theme_name")
    @classmethod
    def validate_theme_name(cls, value: str) -> str:
        if value not in ALLOWED_THEMES:
            raise ValueError(
                f"theme_name must be one of {sorted(ALLOWED_THEMES)}"
            )
        return value
