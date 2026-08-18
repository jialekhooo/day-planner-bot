"""Read a university timetable — a screenshot or its text — into weekly classes.

The grid puts each class in the column of its day, so the picture is read with
OCR and every word is placed back under the day heading it sits below. Optical
character recognition confuses a few characters (`l` for `1`, `O` for `0`), so
codes, times and week numbers are cleaned up before they are trusted.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, time, timedelta

DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
KINDS = ("LEC/STU", "LEC", "STU", "TUT", "LAB", "SEM", "DES", "PRJ")

_CODE = re.compile(r"^[A-Z]{2}\d{4}$")
_DIGITS = re.compile(r"\d{4}$")
_TIMES = re.compile(r"(\d{4})\s*(?:T[O0]|[-–—~])\s*(\d{4})")
_WEEKS = re.compile(r"WK([\d,\-]+)")


class TimetableError(Exception):
    """Raised when a timetable cannot be read."""


@dataclass(frozen=True)
class Class:
    """One class in the grid, repeating on its weekday in the weeks listed."""

    weekday: int  # Monday is 0, as in date.weekday()
    start: time
    end: time
    code: str
    kind: str
    venue: str
    weeks: tuple[int, ...]

    @property
    def title(self) -> str:
        head = f"{self.code} {self.kind}" if self.kind else self.code
        return f"{head} @ {self.venue}" if self.venue else head


def _digits(word: str) -> str:
    """OCR reads 1 as l or I and 0 as O — put the digits back."""
    return word.upper().replace("I", "1").replace("L", "1").replace("|", "1")


def _letters(word: str) -> str:
    """The reverse trick, for the two letters that open a course code."""
    return word.upper().replace("1", "I").replace("0", "O").replace("5", "S")


def _as_code(word: str) -> str | None:
    """`IE4727`, or `1E4727` as OCR sometimes leaves it — two letters, four digits."""
    bare = re.sub(r"[^\w]", "", word).upper()
    if len(bare) != 6 or bare.startswith("WK"):
        return None  # Wk12,13 reads as six characters too
    code = _letters(bare[:2]) + bare[2:]
    return code if _CODE.match(code) else None


def _times_in(words: list[str]) -> tuple[time, time] | None:
    for word in words:
        found = _TIMES.search(_digits(re.sub(r"[^\w\-–—~]", "", word)))
        if found:
            try:
                return _clock(found.group(1)), _clock(found.group(2))
            except ValueError:
                return None
    return None


def _clock(digits: str) -> time:
    return time(int(digits[:2]), int(digits[2:]))


def _weeks_in(words: list[str]) -> tuple[int, ...]:
    """`Wk1-11`, `Wk12,13` and `Wk2-13` all become the weeks they cover."""
    weeks: set[int] = set()
    for word in words:
        found = _WEEKS.search(_digits(re.sub(r"[^\w,\-]", "", word)))
        if not found:
            continue
        for part in found.group(1).split(","):
            span = part.split("-")
            try:
                if len(span) == 2 and span[0] and span[1]:
                    weeks.update(range(int(span[0]), int(span[1]) + 1))
                elif span[0]:
                    weeks.add(int(span[0]))
            except ValueError:
                continue
    return tuple(sorted(weeks))


def _code_in(words: list[str], known: set[str]) -> str | None:
    """A course code, matched back to a cleanly read one when it is garbled."""
    for word in words:
        candidate = _as_code(word)
        if candidate:
            return candidate
        tail = _DIGITS.search(re.sub(r"[^\d]", "", word))
        if tail:
            for code in known:
                if code.endswith(tail.group()):
                    return code
    return None


def _kind_in(words: list[str]) -> str:
    for word in words:
        cleaned = word.upper().strip(".,;:")
        for kind in KINDS:
            if cleaned == kind:
                return kind.split("/")[0]
    return ""


def _venue_in(words: list[str], code: str, kind: str) -> str:
    """Whatever is left once the code, class type, times and weeks are removed."""
    skip = {code, kind, "LEC/STU"}
    leftovers = []
    for word in words:
        cleaned = word.strip(".,;:")
        upper = _digits(re.sub(r"[^\w\-–—~]", "", cleaned))
        if not cleaned or cleaned.upper() in skip or _TIMES.search(upper):
            continue
        if _WEEKS.search(upper) or _looks_like_code(cleaned):
            continue
        if re.fullmatch(r"[A-Z]\d{2}", cleaned.upper()) or len(cleaned) < 3:
            continue  # the class group, e.g. F31, and OCR specks
        leftovers.append(cleaned)
    return " ".join(leftovers[:2])


def _class_from(words: list[str], weekday: int, known: set[str]) -> Class | None:
    times = _times_in(words)
    code = _code_in(words, known)
    if times is None or code is None:
        return None
    kind = _kind_in(words)
    return Class(
        weekday=weekday,
        start=times[0],
        end=times[1],
        code=code,
        kind=kind,
        venue=_venue_in(words, code, kind),
        weeks=_weeks_in(words),
    )


def classes_from_cells(cells: list[tuple[int, list[str]]]) -> list[Class]:
    """Turn each cell's words into a class, ignoring cells that hold neither."""
    known = {code for _, words in cells for word in words if (code := _as_code(word))}
    classes: list[Class] = []
    for weekday, words in cells:
        found = _class_from(words, weekday, known)
        if found is None:
            continue
        if any(  # the grid repeats a class in every row it spans
            (other.weekday, other.start, other.end, other.code, other.weeks)
            == (found.weekday, found.start, found.end, found.code, found.weeks)
            for other in classes
        ):
            continue
        classes.append(found)
    return classes


def classes_from_text(text: str) -> list[Class]:
    """Read a pasted timetable: one class a line, each naming its day."""
    cells: list[tuple[int, list[str]]] = []
    for line in text.splitlines():
        words = line.split()
        weekday = next(
            (
                DAYS.index(word.upper()[:3])
                for word in words
                if word.upper()[:3] in DAYS and len(word) <= 9
            ),
            None,
        )
        if weekday is None:
            continue
        kept = [word for word in words if not (word.upper()[:3] in DAYS and len(word) <= 9)]
        cells.append((weekday, kept))
    return classes_from_cells(cells)


def classes_from_image(image: bytes) -> list[Class]:
    """Read the grid in a screenshot, column by column."""
    import pytesseract  # noqa: PLC0415 - optional, only needed for pictures
    from PIL import Image

    picture = Image.open(io.BytesIO(image)).convert("L")
    picture = picture.resize((picture.width * 3, picture.height * 3), Image.LANCZOS)
    found = pytesseract.image_to_data(
        picture, config="--psm 6", output_type=pytesseract.Output.DICT
    )
    words = [
        (
            found["left"][index] + found["width"][index] / 2,
            found["top"][index],
            found["text"][index].strip(),
        )
        for index in range(len(found["text"]))
        if found["text"][index].strip()
    ]
    columns = _day_columns(words)
    if not columns:
        raise TimetableError("I couldn't find the day headings (MON, TUE, …) in that picture.")
    return classes_from_cells(_cells(words, columns))


def _day_columns(words: list[tuple[float, int, str]]) -> list[tuple[int, float, float]]:
    """Each day heading and the band of the picture that belongs to it."""
    headings = [
        (DAYS.index(text.upper()), centre, top)
        for centre, top, text in words
        if text.upper() in DAYS
    ]
    if len(headings) < 2:
        return []
    headings.sort(key=lambda heading: heading[1])
    centres = [centre for _, centre, _ in headings]
    width = (centres[-1] - centres[0]) / (len(centres) - 1)
    bands = []
    for index, (weekday, centre, _) in enumerate(headings):
        left = centres[index - 1] if index else centre - width
        right = centres[index + 1] if index + 1 < len(centres) else centre + width
        bands.append((weekday, (left + centre) / 2, (centre + right) / 2))
    return bands


def _cells(
    words: list[tuple[float, int, str]],
    columns: list[tuple[int, float, float]],
) -> list[tuple[int, list[str]]]:
    """Group each column's words into blocks, one per class in that column."""
    header_top = min(top for centre, top, text in words if text.upper() in DAYS)
    footer_top = min(
        (top for _, top, text in words if text.lower().startswith("academic")),
        default=None,
    )
    cells: list[tuple[int, list[str]]] = []
    for weekday, left, right in columns:
        inside = sorted(
            (top, text)
            for centre, top, text in words
            if left <= centre < right
            and top > header_top + 20
            and (footer_top is None or top < footer_top)
        )
        block: list[str] = []
        previous: int | None = None
        for top, line in _lines(inside):
            starts_class = any(_looks_like_code(word) for word in line)
            far_below = previous is not None and top - previous > 100
            if block and (starts_class or far_below):
                cells.append((weekday, block))
                block = []
            block.extend(line)
            previous = top
        if block:
            cells.append((weekday, block))
    return cells


def _lines(words: list[tuple[int, str]]) -> list[tuple[int, list[str]]]:
    """Words sitting at much the same height belong to the same line."""
    lines: list[tuple[int, list[str]]] = []
    for top, text in words:
        if lines and top - lines[-1][0] <= 20:
            lines[-1][1].append(text)
            continue
        lines.append((top, [text]))
    return lines


def _looks_like_code(word: str) -> bool:
    bare = re.sub(r"[^\w]", "", word)
    return bool(_as_code(word) or re.fullmatch(r"\d{5,6}", bare))


def week_dates(classes: list[Class], week_one: date, weeks: int) -> list[tuple[date, Class]]:
    """Every date a class falls on, from week one for the given number of weeks."""
    monday = week_one - timedelta(days=week_one.weekday())
    dated = []
    for number in range(1, weeks + 1):
        for lesson in classes:
            if lesson.weeks and number not in lesson.weeks:
                continue
            dated.append((monday + timedelta(days=(number - 1) * 7 + lesson.weekday), lesson))
    return sorted(dated, key=lambda pair: (pair[0], pair[1].start))
