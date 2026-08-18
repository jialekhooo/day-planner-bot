"""One place to talk to the language model, shared by the timetable reader and the assistant."""

from __future__ import annotations

import os
import time

import httpx

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
# whichever answers: the free tier turns one model away as overloaded fairly often
GEMINI_MODELS = tuple(
    os.environ.get("GEMINI_MODEL", "gemini-flash-latest,gemini-3.5-flash").split(",")
)
TIMEOUT = 150.0
TRIES = 4
BUSY = (429, 500, 503)


def available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def sent(urls: tuple[str, ...], headers: dict[str, str], body: dict[str, object]) -> httpx.Response:
    """Post, waiting out the busy answers these free tiers hand back now and then.

    Each try moves on to the next address given, so a model that is overloaded
    hands the work to its stand-in rather than failing the whole request.
    """
    for attempt in range(TRIES):
        url = urls[attempt % len(urls)]
        response = httpx.post(url, headers=headers, timeout=TIMEOUT, json=body)
        if response.status_code not in BUSY or attempt == TRIES - 1:
            response.raise_for_status()
            return response
        time.sleep(2 * (attempt + 1))
    raise httpx.HTTPError("unreachable")


def gemini_urls() -> tuple[str, ...]:
    return tuple(f"{GEMINI_URL}/{model}:generateContent" for model in GEMINI_MODELS)


def text_of(answer: dict[str, object]) -> str:
    candidates = answer.get("candidates", [])
    if not isinstance(candidates, list) or not candidates:
        return ""
    content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
    parts = content.get("parts", []) if isinstance(content, dict) else []
    return "".join(
        str(part.get("text", "")) for part in parts if isinstance(part, dict)
    )


def ask_json(prompt: str, question: str) -> str:
    """Ask the model for a JSON answer, as the assistant and the reader both do."""
    response = sent(
        gemini_urls(),
        {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        {
            "contents": [{"parts": [{"text": prompt}, {"text": question}]}],
            "generationConfig": {"response_mime_type": "application/json"},
        },
    )
    return text_of(response.json())
