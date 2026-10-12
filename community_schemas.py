"""Typed contracts for Shopify Community (Discourse) conversation capture."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

CaptureFormat = Literal["discourse_json", "rendered_text"]
BodyFormat = Literal["raw", "html", "text"]


class ParsedPost(BaseModel):
    """One post exactly as captured. ``None`` always means unknown, never empty."""

    model_config = ConfigDict(extra="forbid")

    post_number: int = Field(gt=0)
    username: str = Field(min_length=1)
    post_id: Optional[int] = None
    body: str
    body_format: BodyFormat
    created_at: Optional[str] = None  # UTC ISO-8601
    updated_at: Optional[str] = None  # UTC ISO-8601, as supplied by the source
    reply_to_post_number: Optional[int] = None


class PostError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_number: Optional[int] = None
    code: str
    message: str


class ParsedTopic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forum_host: str
    topic_id: int = Field(gt=0)
    topic_url: str
    slug: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None  # numeric category id (JSON) or name (text capture)
    author_username: Optional[str] = None
    created_at: Optional[str] = None
    posts_count_declared: Optional[int] = None


class ParsedThread(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_format: CaptureFormat
    topic: ParsedTopic
    posts: list[ParsedPost] = Field(default_factory=list)
    errors: list[PostError] = Field(default_factory=list)


class IngestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_row_id: Optional[int] = None
    forum_host: str
    topic_id: int
    source_format: CaptureFormat
    posts_processed: int = 0
    new_posts: int = 0
    edited_posts: int = 0
    unchanged_posts: int = 0
    new_post_numbers: list[int] = Field(default_factory=list)
    edited_post_numbers: list[int] = Field(default_factory=list)
    parse_errors: list[PostError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    duplicate_capture: bool = False
    incomplete_capture: bool = False
    capture_id: Optional[int] = None
    command_id: Optional[str] = None
