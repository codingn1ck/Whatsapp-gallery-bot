"""
Conversation memory.

The bug this fixes: the bot showed a customer 'Monsoon Over Kaziranga', she
replied "how much is it?", and the bot had no idea what "it" was. It fell
through to a generic dead-end — on the single highest-intent message a
customer can send.

Real people speak in pronouns. "How much is it." "Is that one available."
"Can I see it in person." "The second one." None of these contain an artwork
name. Without memory the bot fumbles every one of them.

Design:
  - Per-phone-number session, in memory, with a TTL.
  - TTL is 24 hours, which is not arbitrary: it is exactly Meta's service
    window. Outside that window we cannot send a free-form reply anyway, so
    a session that outlives the window is a session we can never act on.
  - In-process dict is fine for one gallery. If this ever runs multi-tenant
    or multi-worker, swap the backing store for Redis — the interface is
    get() / touch() / clear().
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Meta's service window. Also our session TTL — see module docstring.
SESSION_TTL_SECONDS = 24 * 60 * 60


@dataclass
class Session:
    number: str
    name: str | None = None

    # What the bot most recently put in front of this customer.
    last_artwork_id: str | None = None
    # Ids offered in the last list, in display order, so "the second one" works.
    last_offered_ids: list[str] = field(default_factory=list)
    last_intent: str | None = None

    updated_at: float = field(default_factory=time.time)
    first_seen: float = field(default_factory=time.time)
    message_count: int = 0

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.updated_at) > SESSION_TTL_SECONDS

    @property
    def is_returning(self) -> bool:
        """Has this person spoken to us before, in an earlier session?"""
        return self.message_count > 1

    def touch(self) -> None:
        self.updated_at = time.time()
        self.message_count += 1

    def remember_artwork(self, artwork_id: str) -> None:
        self.last_artwork_id = artwork_id

    def remember_offer(self, artwork_ids: list[str]) -> None:
        self.last_offered_ids = list(artwork_ids)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get(self, number: str, name: str | None = None) -> Session:
        s = self._sessions.get(number)
        if s is None or s.is_expired:
            s = Session(number=number, name=name)
            self._sessions[number] = s
        if name and not s.name:
            s.name = name
        s.touch()
        return s

    def clear(self, number: str) -> None:
        self._sessions.pop(number, None)

    def sweep(self) -> int:
        """Drop expired sessions. Call periodically if this runs long-lived."""
        dead = [n for n, s in self._sessions.items() if s.is_expired]
        for n in dead:
            del self._sessions[n]
        return len(dead)

    def __len__(self) -> int:
        return len(self._sessions)
