"""Search over plant documents: SOPs, FMEAs, past investigations.

A document is markdown with YAML front matter (``id``, ``title``, ``version``, ``plant``,
``review_date``); each heading starts a passage. :class:`KeywordIndex` ranks passages by keyword
overlap weighted by how rare each word is, and marks a passage stale when its document is past
its review date. Where documents come from, and who may read which, is the caller's business:
:meth:`KeywordIndex.search` takes a filter.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import Counter
from collections.abc import Callable

import yaml

WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if len(w) > 1]


def front_matter(text: str) -> tuple[dict, str]:
    """``(meta, body)`` of a markdown document; ``meta`` is empty without front matter."""
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        return yaml.safe_load(front) or {}, body
    return {}, text


def sections(body: str, title: str) -> list[tuple[str, str]]:
    """``(heading, text)`` for each section with text; text before the first heading is under
    ``title``."""
    out: list[tuple[str, str]] = []
    heading, buf = title, []

    def flush() -> None:
        content = "\n".join(buf).strip()
        if content:
            out.append((heading, content))

    for line in body.splitlines():
        if line.startswith("#"):
            flush()
            heading, buf = line.lstrip("#").strip(), []
        else:
            buf.append(line)
    flush()
    return out


def document(text: str, default_id: str) -> tuple[dict, list[tuple[str, str]]]:
    """A document's fields (doc, title, version, plant, review_date) and its sections."""
    meta, body = front_matter(text)
    doc = {
        "doc": str(meta.get("id", default_id)),
        "title": str(meta.get("title", default_id)),
        "version": str(meta.get("version", "1")),
        "plant": str(meta.get("plant", "global")),
        "review_date": str(meta.get("review_date", "")),
    }
    return {**meta, **doc}, sections(body, doc["title"])


class KeywordIndex:
    """Passages are dicts with at least ``doc``, ``version``, ``title``, ``heading``, ``text``
    and ``review_date``; other keys (``plant``, ``pack``) are kept for the caller's filter."""

    def __init__(self, passages: list[dict]):
        self.passages = passages
        self._words = [words(p["heading"] + " " + p["text"]) for p in passages]
        df: Counter = Counter()
        for ws in self._words:
            df.update(set(ws))
        n = max(len(passages), 1)
        self.idf = {w: math.log(1 + n / c) for w, c in df.items()}

    def search(
        self,
        query: str,
        *,
        keep: Callable[[dict], bool] | None = None,
        limit: int = 5,
        today: dt.date | None = None,
    ) -> list[dict]:
        """The best ``limit`` passages for ``query`` among those ``keep`` accepts."""
        today = today or dt.date.today()
        q = set(words(query))
        scored = []
        for p, ws in zip(self.passages, self._words, strict=True):
            if keep is not None and not keep(p):
                continue
            tf = Counter(ws)
            s = sum(self.idf.get(w, 0) * (1 + math.log(tf[w])) for w in q if tf[w])
            if s > 0:
                scored.append((s, p))
        scored.sort(key=lambda x: -x[0])
        out = []
        for _, p in scored[:limit]:
            stale = bool(p["review_date"]) and p["review_date"] < today.isoformat()
            out.append(
                {
                    "doc": p["doc"],
                    "version": p["version"],
                    "title": p["title"],
                    "section": p["heading"],
                    "text": p["text"][:1200],
                    "review_date": p["review_date"],
                    "stale": stale,
                }
            )
        return out
