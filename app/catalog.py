"""
Catalog layer.

Loads artwork records from a CSV and resolves free-text user queries
("do you have anything by Ritu?", "tell me about the monsoon painting")
to specific artworks.

Design notes:
  - CSV in, not a database. The gallery will hand us a spreadsheet. Meeting
    them where they are costs nothing and removes an onboarding step.
  - Fuzzy matching via rapidfuzz, not embeddings. At 15-400 works, fuzzy
    matching on title + artist is faster, free, deterministic, and debuggable.
    If a client ever shows up with 5000+ works, swap resolve() for a vector
    search. The interface stays the same.
  - Price is NOT exposed unless price_visible == 'yes'. Indian galleries
    withhold prices deliberately to preserve negotiating room. Flipping this
    is a per-artwork config decision, not a code change.
  - MISS IS THE SAFE DEFAULT. An earlier version matched "do you have a
    picasso" to an unrelated Bhardwaj oil, because WRatio scores partial
    character overlap generously. A bot that invents inventory in front of a
    collector is worse than a bot that says "I'm not sure." Hence the
    token-overlap gate: a query must share a real word with a title or artist
    before fuzzy scoring is trusted at all.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz, process

MATCH_THRESHOLD = 80      # raised from 62 after the "picasso" false positive
AMBIGUITY_MARGIN = 8      # top-2 within this gap => ask the user which one

STOPWORDS = {
    "a", "an", "the", "do", "you", "have", "any", "anything", "about",
    "tell", "me", "show", "is", "it", "of", "on", "in", "by", "for",
    "painting", "paintings", "art", "artwork", "work", "piece", "price",
    "available", "please", "hi", "hello", "want", "know", "more", "can",
    "i", "we", "your", "this", "that", "there", "some", "like", "would",
    "much", "how", "cost", "costs", "buy", "see", "look", "looking",
}


ROMAN_RE = re.compile(r"^(?:i{1,3}|iv|v|vi{1,3}|ix|x{1,3})$")


def _tokens(text: str) -> set[str]:
    """
    Meaningful words, lowercased, stopwords and short noise removed.

    Numerals and roman numerals are KEPT regardless of length. In art titles
    they are load-bearing: 'Ficus' and 'Ficus II' are different paintings, and
    'Blue Series III' and 'Study No. 4' hang on exactly those characters. An
    earlier version dropped anything under 3 chars, which made every work in a
    series collapse into its siblings.
    """
    raw = re.findall(r"[a-z0-9']+", (text or "").lower())
    out: set[str] = set()
    for w in raw:
        if w.isdigit():                       # "4" in 'Study No. 4'
            out.add(w)
        elif ROMAN_RE.match(w):               # "ii" in 'Ficus II'
            out.add(w)
        elif len(w) > 2 and w not in STOPWORDS:
            out.add(w)
    return out


@dataclass
class Artwork:
    id: str
    title: str
    artist: str
    medium: str
    dimensions_cm: str
    year: str
    status: str                 # available | sold | on_hold
    price_visible: bool
    price_inr: int | None
    description: str
    image_file: str

    @property
    def is_available(self) -> bool:
        return self.status == "available"

    @property
    def status_line(self) -> str:
        return {
            "available": "Available",
            "sold": "Sold",
            "on hold": "On hold",
            "on_hold": "On hold",
        }.get(self.status, self.status.title())

    def price_line(self) -> str:
        """The only thing the bot is ever allowed to say about money."""
        if self.price_visible and self.price_inr:
            return f"₹{self.price_inr:,}"
        return "Price on request"

    def caption(self) -> str:
        """Message body sent alongside the artwork image."""
        return "\n".join([
            f"*{self.title}*",
            f"{self.artist}, {self.year}",
            f"{self.medium} · {self.dimensions_cm} cm",
            "",
            self.description,
            "",
            f"{self.status_line} · {self.price_line()}",
        ])

    def short_label(self) -> str:
        """WhatsApp truncates button/list titles at 24 chars."""
        return self.title[:24]

    def search_tokens(self) -> set[str]:
        return _tokens(f"{self.title} {self.artist}")


@dataclass
class Catalog:
    works: list[Artwork] = field(default_factory=list)
    _index: dict[str, Artwork] = field(default_factory=dict, repr=False)
    _search_keys: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, csv_path: str | Path) -> "Catalog":
        works: list[Artwork] = []
        with open(csv_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                price_raw = (row.get("price_inr") or "").strip()
                works.append(Artwork(
                    id=row["id"].strip(),
                    title=row["title"].strip(),
                    artist=row["artist"].strip(),
                    medium=row["medium"].strip(),
                    dimensions_cm=row["dimensions_cm"].strip(),
                    year=row["year"].strip(),
                    status=row["status"].strip().lower(),
                    price_visible=row["price_visible"].strip().lower() == "yes",
                    price_inr=int(price_raw) if price_raw else None,
                    description=row["description"].strip(),
                    image_file=row["image_file"].strip(),
                ))
        cat = cls(works=works)
        cat._build_index()
        return cat

    def _build_index(self) -> None:
        self._index = {w.id: w for w in self.works}
        self._search_keys = {}
        for w in self.works:
            self._search_keys[w.title.lower()] = w.id
            self._search_keys[f"{w.title} {w.artist}".lower()] = w.id

    # ---------- lookup ----------

    def by_id(self, work_id: str) -> Artwork | None:
        return self._index.get(work_id)

    def artists(self) -> list[str]:
        return sorted({w.artist for w in self.works})

    def available(self) -> list[Artwork]:
        return [w for w in self.works if w.is_available]

    def by_artist(self, name: str) -> list[Artwork]:
        """
        Works by an artist. Requires genuine token overlap with the artist's
        name, so 'ficus' cannot match a painter just because characters rhyme.
        """
        q_tokens = _tokens(name)
        if not q_tokens:
            return []
        best: tuple[int, str] | None = None
        for artist in {w.artist for w in self.works}:
            if not (q_tokens & _tokens(artist)):
                continue
            score = fuzz.WRatio(name.lower(), artist.lower())
            if best is None or score > best[0]:
                best = (score, artist)
        if not best:
            return []
        return [w for w in self.works if w.artist == best[1]]

    def resolve(self, query: str) -> tuple[str, list[Artwork]]:
        """
        Map free text to artworks.

        Returns (outcome, works):
          'hit'       -> [the one artwork]
          'ambiguous' -> [2-3 candidates to disambiguate]
          'artist'    -> [all works by a matched artist]
          'miss'      -> []

        'miss' is the safe default. Better to ask the customer to rephrase
        than to serve a painting they never asked about.
        """
        q = (query or "").strip()
        q_tokens = _tokens(q)
        if not q_tokens:
            return "miss", []

        # 1. Artist query: "anything by Menon" should list, not guess one work.
        artist_works = self.by_artist(q)
        if len(artist_works) > 1:
            return "artist", artist_works
        if len(artist_works) == 1:
            return "hit", artist_works

        # 2. Gate: query must share a real word with some title/artist.
        candidates = [w for w in self.works if q_tokens & w.search_tokens()]
        if not candidates:
            return "miss", []

        cand_ids = {c.id for c in candidates}
        candidate_keys = {
            k: wid for k, wid in self._search_keys.items() if wid in cand_ids
        }

        matches = process.extract(
            q.lower(), candidate_keys.keys(), scorer=fuzz.WRatio, limit=5
        )
        good = [m for m in matches if m[1] >= MATCH_THRESHOLD]

        # 3. Fallback: token overlap is trustworthy even when the fuzzy score
        #    is dragged down by extra words ("is the kathakali one available").
        #    If exactly one work in the catalog shares a distinctive word with
        #    the query, that IS the answer — a single unique token is stronger
        #    evidence than a mediocre character-level score.
        if not good:
            if len(candidates) == 1:
                return "hit", candidates
            best_overlap = max(
                len(q_tokens & w.search_tokens()) for w in candidates
            )
            token_hits = [
                w for w in candidates
                if len(q_tokens & w.search_tokens()) == best_overlap
            ]
            if len(token_hits) == 1:
                return "hit", token_hits
            if 2 <= len(token_hits) <= 3:
                return "ambiguous", token_hits
            return "miss", []

        top = good[0][1]
        close = [m for m in good if top - m[1] <= AMBIGUITY_MARGIN]

        seen: list[str] = []
        for key, _score, _idx in close:
            wid = candidate_keys[key]
            if wid not in seen:
                seen.append(wid)

        if len(seen) == 1:
            return "hit", [self._index[seen[0]]]

        # Tie-break on token overlap before declaring ambiguity.
        #
        # WRatio scores 'ficus' and 'ficus ii' identically (90.0) against
        # "tell me about ficus ii", so the fuzzy path alone calls it a tie.
        # But the query contains 'ii', and only ONE work has that token. More
        # matched meaningful words = the better answer. This is what makes
        # series titles (Ficus II, Blue Series III, Study No. 4) resolvable.
        tied = [self._index[w] for w in seen]
        overlaps = [len(q_tokens & w.search_tokens()) for w in tied]
        best = max(overlaps)
        winners = [w for w, n in zip(tied, overlaps) if n == best]
        if len(winners) == 1:
            return "hit", winners

        return "ambiguous", tied[:3]
