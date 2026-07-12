"""
Conversation flows.

The bot's actual behaviour. Everything else in this codebase is plumbing;
this file is the product.

Design rules:
  - The bot NEVER states a price unless that artwork is explicitly marked
    price_visible. Indian galleries withhold prices deliberately, to preserve
    negotiating room and differential pricing by buyer. A bot that blurts a
    number destroys the thing the gallery's business runs on.
  - The bot NEVER invents an artwork. Every artwork it mentions is verified
    against the catalog first.
  - The bot REMEMBERS. "How much is it?" after being shown a painting must
    resolve to that painting. This is the highest-intent message a customer
    sends, and a bot that fumbles it is worthless.
  - Every dead end captures a lead. If the bot cannot answer, that is not a
    failure — it is a qualified customer with a phone number, handed to a
    human. That is the whole ROI.
"""

from __future__ import annotations

import os
import re

from catalog import Artwork, Catalog
from leads import Lead, save_lead
from llm import LLM, Classification
from session import Session, SessionStore
from whatsapp import Button, ListRow, WhatsAppClient

GALLERY_NAME = os.getenv("GALLERY_NAME", "The Gallery")
GALLERY_HOURS = os.getenv("GALLERY_HOURS", "Tue–Sun, 11am–7pm")
GALLERY_ADDRESS = os.getenv("GALLERY_ADDRESS", "[gallery address]")
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "https://example.com/artworks")

BTN_BROWSE = Button("browse", "See artworks")
BTN_VISIT = Button("visit", "Plan a visit")
BTN_HUMAN = Button("human", "Talk to us")
BTN_CALLBACK = Button("callback", "Request callback")

# Messages that refer to something already on screen rather than naming it.
#
# CAREFUL. Every word here has a second life in ordinary English. "this" is a
# pronoun in "how much is this" and a determiner in "can I come THIS weekend".
# An earlier version matched the latter, decided the customer was still talking
# about the last painting, and re-sent it instead of routing to the visit flow.
# Same failure class as putting "one" in ORDINALS.
PRONOUN_RE = re.compile(
    r"\b(it|its|that|this|these|those|them|the one|that one|this one|same)\b",
    re.IGNORECASE,
)

# "this weekend", "this evening", "that day" — the pronoun is pointing at TIME,
# not at an artwork. If a pronoun is followed by one of these, it is not a
# reference to anything we showed.
TIME_AFTER_PRONOUN_RE = re.compile(
    r"\b(it|its|that|this|these|those)\s+"
    r"(weekend|week|month|morning|afternoon|evening|night|day|sunday|monday|"
    r"tuesday|wednesday|thursday|friday|saturday|today|tomorrow|time)\b",
    re.IGNORECASE,
)

# Explicit intent. These OUTRANK a bare pronoun — "can I come see the gallery"
# is a visit request even though it contains "see".
VISIT_RE = re.compile(
    r"\b(visit|come\s+(?:in|by|over|see)|drop\s+by|walk\s+in|appointment|"
    r"opening\s+hours|timings?|address|located|where\s+are\s+you|"
    r"see\s+the\s+gallery|come\s+to\s+the\s+gallery)\b",
    re.IGNORECASE,
)
HUMAN_RE = re.compile(
    r"\b(speak|talk)\s+to\s+(someone|somebody|a\s+person|you|the\s+owner)|"
    r"\b(call\s+me|callback|call\s+back|contact\s+me|human|representative)\b",
    re.IGNORECASE,
)

ORDINALS = {
    # NOTE: "one"/"two"/"three" are deliberately NOT here. In English "one" is
    # a filler noun ("that one", "the kathakali one", "which one"), not an
    # ordinal. Mapping it to index 0 made "the second one" resolve to the FIRST
    # item. Cardinals are ambiguous; only true ordinals belong in this map.
    "first": 0, "1st": 0,
    "second": 1, "2nd": 1,
    "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3,
    "fifth": 4, "5th": 4,
    "last": -1,
}
PRICE_RE = re.compile(
    r"\b(price|cost|how much|kitna|kitne|rate|worth|budget)\b", re.IGNORECASE
)


def image_url(work: Artwork) -> str:
    return f"{IMAGE_BASE_URL.rstrip('/')}/{work.image_file}"


class Bot:
    def __init__(
        self,
        catalog: Catalog,
        llm: LLM,
        wa: WhatsAppClient,
        sessions: SessionStore | None = None,
    ) -> None:
        self.catalog = catalog
        self.llm = llm
        self.wa = wa
        self.sessions = sessions or SessionStore()

    # ---------------------------------------------------------------- entry

    def handle(self, number: str, name: str | None, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return

        s = self.sessions.get(number, name)

        if self._handle_tap(s, text):
            return

        # ORDER MATTERS.
        #
        # Explicit intent comes FIRST. "Can I come see the gallery this weekend"
        # is a visit request, full stop — even though it contains "this" and
        # "see". An earlier version let the pronoun gate grab it and re-sent the
        # last painting instead, and silently dropped the visit lead. A walk-in
        # request is the most valuable thing a gallery customer can send.
        if VISIT_RE.search(text):
            s.last_intent = "visit_request"
            return self._visit(s, text)

        if HUMAN_RE.search(text):
            s.last_intent = "human_request"
            return self._human(s, text)

        # Then resolve references against what the customer was last shown,
        # before asking the model anything. "How much is it" is a price query
        # about a known artwork, not a mystery.
        ctx_work, ctx_kind = self._resolve_context(s, text)

        if ctx_work and PRICE_RE.search(text):
            s.last_intent = "price_query"
            return self._price(s, ctx_work, text)

        if ctx_work and ctx_kind in ("ordinal", "pronoun"):
            s.last_intent = "artwork_query"
            return self._send_work(s, ctx_work)

        c: Classification = self.llm.classify(text, self.catalog)
        s.last_intent = c.intent

        if c.intent == "greeting":
            return self._greet(s)
        if c.intent == "catalog_browse":
            return self._browse(s)
        if c.intent == "visit_request":
            return self._visit(s, text)
        if c.intent == "human_request":
            return self._human(s, text)
        if c.intent in ("artwork_query", "price_query"):
            return self._artwork(s, text, c)

        return self._fallback(s, text)

    # ------------------------------------------------------------- context

    def _resolve_context(self, s: Session, text: str) -> tuple[Artwork | None, str]:
        """
        What does 'it' / 'the second one' / 'the ficus' refer to?

        Returns (artwork, kind) where kind is one of:
            'named'    - the message names a real artwork outright
            'ordinal'  - "the second one", against the last list we offered
            'pronoun'  - "it", "that one", against the last work we showed
            'carry'    - no explicit reference, but a work is still in context
            'none'     - nothing to refer to
        """
        low = text.lower()

        # 1. A named artwork always wins. "Show me the ficus" is not a pronoun.
        outcome, works = self.catalog.resolve(text)
        if outcome == "hit":
            return works[0], "named"

        # 2. "The second one" — only meaningful if we just offered a list.
        if s.last_offered_ids:
            for word, idx in ORDINALS.items():
                if re.search(rf"\b{re.escape(word)}\b", low):
                    try:
                        w = self.catalog.by_id(s.last_offered_ids[idx])
                    except IndexError:
                        continue
                    if w:
                        return w, "ordinal"

        # 3. A pronoun, against whatever we last put on screen — BUT not if the
        #    pronoun is pointing at a time ("this weekend", "that evening").
        if (
            s.last_artwork_id
            and PRONOUN_RE.search(text)
            and not TIME_AFTER_PRONOUN_RE.search(text)
        ):
            w = self.catalog.by_id(s.last_artwork_id)
            if w:
                return w, "pronoun"

        # 4. Nothing explicit, but something is still in context. Used only for
        #    price queries ("how much" with no pronoun at all).
        if s.last_artwork_id:
            w = self.catalog.by_id(s.last_artwork_id)
            if w:
                return w, "carry"

        return None, "none"

    def _handle_tap(self, s: Session, text: str) -> bool:
        tap = text.strip().lower()
        if tap == "browse":
            self._browse(s)
            return True
        if tap == "visit":
            self._visit(s, "Tapped: plan a visit")
            return True
        if tap in ("human", "callback"):
            self._human(s, "Tapped: talk to us")
            return True
        if tap.startswith("art:"):
            work = self.catalog.by_id(text.split(":", 1)[1].strip())
            if work:
                self._send_work(s, work)
                return True
        return False

    # --------------------------------------------------------------- flows

    def _greet(self, s: Session) -> None:
        who = f" {s.name}" if s.name else ""
        if s.is_returning and s.last_artwork_id:
            work = self.catalog.by_id(s.last_artwork_id)
            if work:
                self.wa.send_buttons(
                    s.number,
                    f"Welcome back{who}. You were looking at *{work.title}*.",
                    [BTN_VISIT, BTN_CALLBACK, BTN_BROWSE],
                )
                return
        self.wa.send_buttons(
            s.number,
            f"Hello{who} — welcome to {GALLERY_NAME}.\n\n"
            "I can tell you about the works we have, help you plan a visit, "
            "or put you in touch with someone here.",
            [BTN_BROWSE, BTN_VISIT, BTN_HUMAN],
        )

    def _browse(self, s: Session) -> None:
        works = self.catalog.available()
        if not works:
            self.wa.send_text(s.number, "Nothing is listed as available right now.")
            return
        shown = works[:10]
        s.remember_offer([w.id for w in shown])
        more = ""
        if len(works) > 10:
            more = f"\n\n(Showing 10 of {len(works)}. Ask me about an artist to see more.)"
        self.wa.send_list(
            s.number,
            f"Here's what's currently available at {GALLERY_NAME}.{more}",
            "View artworks",
            [ListRow(f"art:{w.id}", w.short_label(), f"{w.artist} · {w.year}") for w in shown],
        )

    def _artwork(self, s: Session, text: str, c: Classification) -> None:
        if c.artwork_id:
            work = self.catalog.by_id(c.artwork_id)
            if work:
                if c.intent == "price_query":
                    return self._price(s, work, text)
                return self._send_work(s, work)

        if c.artist:
            works = self.catalog.by_artist(c.artist)
            if len(works) > 1:
                return self._offer(s, works, f"We have {len(works)} works by {works[0].artist}.")
            if works:
                return self._send_work(s, works[0])

        outcome, works = self.catalog.resolve(text)
        if outcome == "hit":
            if c.intent == "price_query":
                return self._price(s, works[0], text)
            return self._send_work(s, works[0])
        if outcome == "artist":
            return self._offer(s, works, f"We have {len(works)} works by {works[0].artist}.")
        if outcome == "ambiguous":
            return self._offer(s, works, "Which one did you mean?")

        return self._miss(s, text)

    def _offer(self, s: Session, works: list[Artwork], body: str) -> None:
        shown = works[:10]
        s.remember_offer([w.id for w in shown])
        self.wa.send_list(
            s.number,
            body,
            "View works",
            [ListRow(f"art:{w.id}", w.short_label(), f"{w.status_line} · {w.year}") for w in shown],
        )

    def _send_work(self, s: Session, work: Artwork) -> None:
        s.remember_artwork(work.id)
        self.wa.send_image(s.number, image_url(work), work.caption())

        if not work.is_available:
            self.wa.send_buttons(
                s.number,
                f"This piece is {work.status_line.lower()}. We may have similar works "
                f"by {work.artist} — would you like someone to get in touch?",
                [BTN_CALLBACK, BTN_BROWSE],
            )
            return

        self.wa.send_buttons(
            s.number,
            "Would you like to see it in person, or have someone contact you?",
            [BTN_VISIT, BTN_CALLBACK],
        )

    def _price(self, s: Session, work: Artwork, text: str) -> None:
        """
        The highest-intent message a customer sends. Never waste it.
        Either quote the (rare) published price, or capture the lead.
        """
        s.remember_artwork(work.id)

        if work.price_visible and work.price_inr:
            self.wa.send_buttons(
                s.number,
                f"*{work.title}* is {work.price_line()}.\n\n"
                "Framing and delivery are quoted separately. "
                "Shall I have someone confirm the details with you?",
                [BTN_CALLBACK, BTN_VISIT],
            )
            return

        save_lead(Lead(
            wa_number=s.number, wa_name=s.name, reason="price",
            artwork_id=work.id, artwork_title=work.title, message=text,
        ))
        self.wa.send_buttons(
            s.number,
            f"Pricing for *{work.title}* is shared directly by the gallery — "
            "it depends on framing, availability and terms.\n\n"
            "I've passed your enquiry on. Someone will get back to you within "
            "24 hours, or you're welcome to visit us.",
            [BTN_VISIT, BTN_HUMAN],
        )

    def _visit(self, s: Session, text: str) -> None:
        work = self.catalog.by_id(s.last_artwork_id) if s.last_artwork_id else None
        save_lead(Lead(
            wa_number=s.number, wa_name=s.name, reason="visit",
            artwork_id=work.id if work else None,
            artwork_title=work.title if work else None,
            message=text,
        ))
        extra = f"\n\nI've noted your interest in *{work.title}*." if work else ""
        self.wa.send_text(
            s.number,
            f"*{GALLERY_NAME}*\n{GALLERY_ADDRESS}\n{GALLERY_HOURS}\n\n"
            "You're welcome to walk in during opening hours. Someone from the "
            f"gallery will confirm a time with you shortly.{extra}",
        )

    def _human(self, s: Session, text: str) -> None:
        work = self.catalog.by_id(s.last_artwork_id) if s.last_artwork_id else None
        save_lead(Lead(
            wa_number=s.number, wa_name=s.name, reason="callback",
            artwork_id=work.id if work else None,
            artwork_title=work.title if work else None,
            message=text,
        ))
        ref = f" about *{work.title}*" if work else ""
        self.wa.send_text(
            s.number,
            f"I've passed this{ref} to the gallery. Someone will get back to you "
            "within 24 hours on this number.\n\n"
            "If it's urgent, you're welcome to visit us during opening hours.",
        )

    def _miss(self, s: Session, text: str) -> None:
        save_lead(Lead(
            wa_number=s.number, wa_name=s.name, reason="artwork", message=text
        ))
        self.wa.send_buttons(
            s.number,
            "I couldn't find that in our current collection — I may simply not "
            "have it listed. I've noted your enquiry and someone from the gallery "
            "will follow up.\n\nIn the meantime:",
            [BTN_BROWSE, BTN_HUMAN],
        )

    def _fallback(self, s: Session, text: str) -> None:
        save_lead(Lead(
            wa_number=s.number, wa_name=s.name, reason="artwork", message=text
        ))
        self.wa.send_buttons(
            s.number,
            "I've noted that and passed it to the gallery — someone will follow "
            "up with you.\n\nI can also help with:",
            [BTN_BROWSE, BTN_VISIT, BTN_HUMAN],
        )
