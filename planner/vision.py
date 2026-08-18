"""Read a timetable picture with a vision model, which beats OCR on screenshots.

Tesseract loses small grid text, so the picture is shown to OpenAI instead and
asked for the rows in the same shape `timetable.classes_from_text` reads. The
key is optional: without it the bot falls back to OCR.
"""

from __future__ import annotations

import base64
import io
import json
import os

from PIL import Image

from .model import gemini_urls, sent, text_of

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
URL = "https://api.openai.com/v1/chat/completions"
WIDEST = 1600  # a whole-page screenshot is far bigger than the model needs

PROMPT = """You are reading a university class timetable, a grid whose columns \
are the days of the week.

Return JSON: {"classes": [{"day": "MON", "start": "0930", "end": "1120",
"code": "IE4727", "kind": "LEC", "venue": "S2-B3A_06", "weeks": "1-11"}]}

Rules:
- One entry per class block in the grid; a block spanning several rows is one entry.
- day is MON, TUE, WED, THU, FRI, SAT or SUN, taken from the column it sits in.
- start and end are 24-hour times as four digits, from the rows the block covers.
- kind is the class type shown, e.g. LEC, TUT, LAB, SEM (LEC/STU counts as LEC).
- venue is the room, "" if none is shown.
- weeks copies the week note, e.g. "1-11", "12,13" or "" when the block has none.
- Ignore any summary table, legend or heading outside the grid.
- Return only the JSON object."""


def classes_as_rows(image: bytes) -> str:
    """The timetable as rows `classes_from_text` reads, or "" without a key."""
    if os.environ.get("GEMINI_API_KEY"):
        return rows_from_answer(_ask_gemini(image))
    if os.environ.get("OPENAI_API_KEY"):
        return rows_from_answer(_ask_openai(image))
    return ""


def _shrunk(image: bytes) -> str:
    """The picture, narrowed if huge, as base64 PNG — smaller means a faster answer."""
    try:
        picture = Image.open(io.BytesIO(image))
    except OSError:
        return base64.b64encode(image).decode()
    if picture.width > WIDEST:
        height = round(picture.height * WIDEST / picture.width)
        picture = picture.resize((WIDEST, height), Image.LANCZOS)
    kept = io.BytesIO()
    picture.convert("RGB").save(kept, format="PNG")
    return base64.b64encode(kept.getvalue()).decode()


def _ask_openai(image: bytes) -> str:
    key = os.environ["OPENAI_API_KEY"]
    picture = _shrunk(image)
    response = sent(
        (URL,),
        {"Authorization": f"Bearer {key}"},
        {
            "model": MODEL,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{picture}"},
                        },
                    ],
                }
            ],
        },
    )
    return response.json()["choices"][0]["message"]["content"]


def _ask_gemini(image: bytes) -> str:
    """Google's model reads pictures just as well and has a free tier."""
    key = os.environ["GEMINI_API_KEY"]
    picture = _shrunk(image)
    response = sent(
        gemini_urls(),
        {"x-goog-api-key": key},
        {
            "contents": [
                {
                    "parts": [
                        {"text": PROMPT},
                        {"inline_data": {"mime_type": "image/png", "data": picture}},
                    ]
                }
            ],
            "generationConfig": {
                "response_mime_type": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},  # reading a grid needs no pondering
            },
        },
    )
    return text_of(response.json())


def rows_from_answer(answer: str) -> str:
    """Turn the model's JSON into timetable rows, dropping anything malformed."""
    try:
        found = json.loads(answer)
    except json.JSONDecodeError:
        return ""
    classes = found.get("classes") if isinstance(found, dict) else None
    if not isinstance(classes, list):
        return ""
    rows = []
    for lesson in classes:
        if not isinstance(lesson, dict):
            continue
        day = str(lesson.get("day", "")).upper()[:3]
        start = str(lesson.get("start", "")).replace(":", "")
        end = str(lesson.get("end", "")).replace(":", "")
        code = str(lesson.get("code", "")).upper()
        if not (day and start.isdigit() and end.isdigit() and code):
            continue
        weeks = str(lesson.get("weeks", "")).replace(" ", "")
        rows.append(
            " ".join(
                part
                for part in (
                    day,
                    f"{start}-{end}",
                    code,
                    str(lesson.get("kind", "")).upper(),
                    str(lesson.get("venue", "")),
                    f"Wk{weeks}" if weeks else "",
                )
                if part
            )
        )
    return "\n".join(rows)
