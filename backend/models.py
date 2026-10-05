"""Pydantic models shared across modules."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ApplicationStatus(str, Enum):
    """Status enum from spec 3.4."""

    APPLIED = "applied"
    OA = "oa"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    GHOSTED = "ghosted"
    OTHER = "other"


#: Statuses that end the application lifecycle — never flagged as stale (spec 3.6).
TERMINAL_STATUSES = {ApplicationStatus.REJECTED, ApplicationStatus.OFFER}


class EmailRecord(BaseModel):
    """One Gmail thread reduced to the text we hand the LLM."""

    thread_id: str
    message_ids: list[str] = Field(default_factory=list)
    subject: str = ""
    sender: str = ""
    received_at: datetime
    body: str = ""


class ExtractedApplication(BaseModel):
    """Strict JSON contract we require back from the LLM (spec 3.4)."""

    is_job_related: bool
    company: str = ""
    role: str = ""
    status: ApplicationStatus = ApplicationStatus.OTHER
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class UserTokens(BaseModel):
    """Persisted OAuth credentials for one Google account."""

    email: str
    token: str | None = None
    refresh_token: str | None = None
    token_uri: str
    client_id: str
    client_secret: str
    scopes: list[str] = Field(default_factory=list)
    expiry: datetime | None = None
    sheet_id: str | None = None
    sheet_url: str | None = None
