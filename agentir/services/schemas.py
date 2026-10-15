from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class StartRequest(BaseModel):
    caption: str
    session_id: Optional[str] = None


class StartResponse(BaseModel):
    session_id: str
    query: str


class AskResponse(BaseModel):
    question: str
    candidates: list[str]
    scores: Optional[list[float]] = None
    debug: dict[str, Any] = {}


class ObserveRequest(BaseModel):
    question: str
    answer: str


class ObserveResponse(BaseModel):
    query: str
    cost: dict[str, int] = {}


class AnswerRequest(BaseModel):
    target: str  # path relative to the user-sim's corpus_dir
    question: str
    context: Optional[str] = None  # current retrieval query, if the user-sim is configured to see it


class AnswerResponse(BaseModel):
    answer: str
