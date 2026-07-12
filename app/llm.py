"""
LLM layer — deliberately thin.

The bot needs exactly one thing from a language model: read a customer's
message and tell us (a) what they want, and (b) which artwork they mean.
That's a classification task, not a reasoning task. Any competent small model
does it well.

So the model sits behind ONE function, `classify()`, with three
implementations:

    MockLLM    - no API key, deterministic, falls back to fuzzy matching.
                 Used for tests and for building before any key exists.
    GeminiLLM  - Google AI Studio. Free tier is fine for demos.
    ClaudeLLM  - Anthropic. Same interface, different vendor.

Swap providers by changing LLM_PROVIDER in config. Nothing else in the
codebase knows or cares which model is running.

CRITICAL CONSTRAINT: the model is NEVER allowed to invent an artwork. It may
only return an artwork_id that exists in the catalog we hand it, or null.
Every returned id is verified against the catalog before use. This is what
stops the bot cheerfully offering a Picasso the gallery does not own.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Protocol

from catalog import Catalog

# ---------------------------------------------------------------- intents

INTENTS = {
    "greeting",        # hi, hello, namaste
    "artwork_query",   # tell me about X / do you have anything by Y
    "price_query",     # how much is X
    "visit_request",   # can I come see the gallery / book an appointment
    "human_request",   # I want to talk to someone
    "catalog_browse",  # what do you have / show me your collection
    "other",           # anything else — hand to a human
}


@dataclass
class Classification:
    intent: str
    artwork_id: str | None = None
    artist: str | None = None
    confidence: float = 0.0
    raw: str = ""

    def __post_init__(self) -> None:
        if self.intent not in INTENTS:
            self.intent = "other"


class LLM(Protocol):
    def classify(self, message: str, catalog: Catalog) -> Classification: ...


# ---------------------------------------------------------------- prompt

SYSTEM_PROMPT = """You are the message router for an art gallery's WhatsApp assistant.

Read the customer's message and return ONLY a JSON object, no prose, no markdown fences:

{"intent": "...", "artwork_id": "..." or null, "artist": "..." or null, "confidence": 0.0-1.0}

intent must be exactly one of:
  greeting        - a greeting with no other request
  artwork_query   - asking about a specific artwork or an artist's works
  price_query     - asking what something costs
  visit_request   - wants to visit the gallery or book an appointment
  human_request   - wants to speak to a person
  catalog_browse  - wants to see what's available generally
  other           - anything else

artwork_id: ONLY an id from the catalog below, or null. NEVER invent an id.
If the customer names an artist or artwork the gallery does not have, return null.
Do not guess. A wrong artwork is worse than no artwork.

artist: the artist's name IF the customer is asking about an artist's body of
work rather than one piece. Otherwise null.

CATALOG:
{catalog_block}
"""


def _catalog_block(catalog: Catalog) -> str:
    return "\n".join(
        f"{w.id} | {w.title} | {w.artist} | {w.year}" for w in catalog.works
    )


def _parse(raw: str, catalog: Catalog) -> Classification:
    """Parse model output, then VERIFY every id against the real catalog."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return Classification(intent="other", raw=raw)
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return Classification(intent="other", raw=raw)

    art_id = data.get("artwork_id")
    # The guardrail. A hallucinated id is discarded, silently and always.
    if art_id and not catalog.by_id(str(art_id).strip()):
        art_id = None

    return Classification(
        intent=str(data.get("intent", "other")).strip(),
        artwork_id=str(art_id).strip() if art_id else None,
        artist=(str(data["artist"]).strip() if data.get("artist") else None),
        confidence=float(data.get("confidence") or 0.0),
        raw=raw,
    )


# ---------------------------------------------------------------- mock

GREETING_WORDS = {"hi", "hello", "hey", "namaste", "namaskar", "hola", "good"}
VISIT_WORDS = {"visit", "come", "appointment", "see", "gallery", "open", "timing", "hours", "address"}
HUMAN_WORDS = {"talk", "speak", "someone", "person", "human", "call", "owner", "manager"}
PRICE_WORDS = {"price", "cost", "how much", "rate", "worth"}
BROWSE_WORDS = {"catalog", "catalogue", "collection", "what do you have", "show me", "everything", "list"}


class MockLLM:
    """
    No API key required. Rule-based intent detection + the catalog's own
    fuzzy matcher. Good enough to build and test the entire system against,
    and it keeps the test suite free and deterministic forever.
    """

    def classify(self, message: str, catalog: Catalog) -> Classification:
        msg = (message or "").strip().lower()
        if not msg:
            return Classification(intent="other")

        words = set(re.findall(r"[a-z]+", msg))

        outcome, works = catalog.resolve(message)

        if any(p in msg for p in PRICE_WORDS) and outcome in ("hit", "ambiguous"):
            return Classification(
                intent="price_query",
                artwork_id=works[0].id if outcome == "hit" else None,
                confidence=0.8,
            )
        if outcome == "hit":
            return Classification(intent="artwork_query", artwork_id=works[0].id, confidence=0.85)
        if outcome == "artist":
            return Classification(intent="artwork_query", artist=works[0].artist, confidence=0.8)
        if outcome == "ambiguous":
            return Classification(intent="artwork_query", confidence=0.5)

        if any(p in msg for p in BROWSE_WORDS):
            return Classification(intent="catalog_browse", confidence=0.8)
        if words & HUMAN_WORDS:
            return Classification(intent="human_request", confidence=0.7)
        if words & VISIT_WORDS:
            return Classification(intent="visit_request", confidence=0.7)
        if words & GREETING_WORDS and len(words) <= 4:
            return Classification(intent="greeting", confidence=0.9)

        return Classification(intent="other", confidence=0.3)


# ---------------------------------------------------------------- gemini

class GeminiLLM:
    """Google AI Studio. Free tier is sufficient for demos."""

    ENDPOINT = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "{model}:generateContent"
    )

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash") -> None:
        self.api_key = api_key
        self.model = model

    def classify(self, message: str, catalog: Catalog) -> Classification:
        import requests

        prompt = SYSTEM_PROMPT.replace("{catalog_block}", _catalog_block(catalog))
        try:
            resp = requests.post(
                self.ENDPOINT.format(model=self.model),
                headers={"Content-Type": "application/json"},
                params={"key": self.api_key},
                json={
                    "system_instruction": {"parts": [{"text": prompt}]},
                    "contents": [{"parts": [{"text": message}]}],
                    "generationConfig": {
                        "temperature": 0,
                        "maxOutputTokens": 200,
                        "responseMimeType": "application/json",
                    },
                },
                timeout=12,
            )
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return _parse(text, catalog)
        except Exception as exc:  # noqa: BLE001 - never let the bot 500 on a customer
            print(f"[llm] gemini failed, falling back to mock: {exc}")
            return MockLLM().classify(message, catalog)


# ---------------------------------------------------------------- claude

class ClaudeLLM:
    """Anthropic. Same interface, different vendor."""

    ENDPOINT = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001") -> None:
        self.api_key = api_key
        self.model = model

    def classify(self, message: str, catalog: Catalog) -> Classification:
        import requests

        prompt = SYSTEM_PROMPT.replace("{catalog_block}", _catalog_block(catalog))
        try:
            resp = requests.post(
                self.ENDPOINT,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 200,
                    "temperature": 0,
                    "system": prompt,
                    "messages": [{"role": "user", "content": message}],
                },
                timeout=12,
            )
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"]
            return _parse(text, catalog)
        except Exception as exc:  # noqa: BLE001
            print(f"[llm] claude failed, falling back to mock: {exc}")
            return MockLLM().classify(message, catalog)


# ---------------------------------------------------------------- factory

def get_llm() -> LLM:
    """
    Provider chosen by env var. Defaults to mock so the system always runs,
    even with no keys configured at all.

        LLM_PROVIDER=mock|gemini|claude
        GEMINI_API_KEY=...
        ANTHROPIC_API_KEY=...
    """
    provider = os.getenv("LLM_PROVIDER", "mock").strip().lower()

    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if not key:
            print("[llm] LLM_PROVIDER=gemini but GEMINI_API_KEY is unset — using mock")
            return MockLLM()
        return GeminiLLM(key, os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))

    if provider == "claude":
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            print("[llm] LLM_PROVIDER=claude but ANTHROPIC_API_KEY is unset — using mock")
            return MockLLM()
        return ClaudeLLM(key, os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"))

    return MockLLM()
