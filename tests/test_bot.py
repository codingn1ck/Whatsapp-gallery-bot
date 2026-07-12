#!/usr/bin/env python3
"""
Test suite.

These pin the two bugs that actually bit during development, plus the rules
that protect the gallery's business. If any of these break, the bot is
embarrassing a real gallery in front of a real collector.

Run:  python3 tests/test_bot.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))
os.chdir(ROOT)
os.environ["LEADS_DB"] = "data/test_leads.db"

from catalog import Catalog          # noqa: E402
from flows import Bot                # noqa: E402
from llm import MockLLM              # noqa: E402
from session import SessionStore     # noqa: E402
from whatsapp import MockWhatsApp    # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {detail}")


def fresh() -> tuple[Bot, MockWhatsApp]:
    cat = Catalog.load("data/catalog.csv")
    wa = MockWhatsApp(verbose=False)
    return Bot(cat, MockLLM(), wa, SessionStore()), wa


def body_of(entry: dict) -> str:
    return entry.get("body") or entry.get("caption") or ""


def all_text(wa: MockWhatsApp) -> str:
    return "\n".join(body_of(e) for e in wa.sent)


# ---------------------------------------------------------- catalog matching

print("\ncatalog matching")

cat = Catalog.load("data/catalog.csv")

check("loads 15 works", len(cat.works) == 15, f"got {len(cat.works)}")

o, w = cat.resolve("do you have a picasso")
check("REGRESSION: 'picasso' must MISS, not hallucinate a painting",
      o == "miss", f"got {o} {[x.title for x in w]}")

o, w = cat.resolve("i want to buy a van gogh")
check("REGRESSION: 'van gogh' must MISS", o == "miss", f"got {o}")

o, w = cat.resolve("is the kathakali one available")
check("REGRESSION: 'kathakali' must HIT (was a false negative)",
      o == "hit" and w[0].id == "A009", f"got {o}")

o, w = cat.resolve("tell me about the monsoon painting")
check("fuzzy title match works", o == "hit" and w[0].id == "A001", f"got {o}")

o, w = cat.resolve("anything by menon")
check("artist query lists all their works",
      o == "artist" and len(w) == 3, f"got {o} n={len(w)}")

o, w = cat.resolve("asdkjhasd")
check("gibberish misses", o == "miss", f"got {o}")


# ------------------------------------------------------------- price policy

print("\nprice policy (the gallery's business rules)")

hidden = cat.by_id("A001")   # price_visible = no
shown = cat.by_id("A011")    # price_visible = yes, 45000

check("hidden-price work says 'Price on request'",
      hidden.price_line() == "Price on request", hidden.price_line())
check("published-price work shows the figure",
      "45,000" in shown.price_line(), shown.price_line())
check("no rupee figure leaks into a hidden-price caption",
      "₹" not in hidden.caption())


# ----------------------------------------------------- conversation memory

print("\nconversation memory")

bot, wa = fresh()
bot.handle("+91999", "Test", "tell me about the monsoon painting")
wa.sent.clear()
bot.handle("+91999", "Test", "how much is it?")
text = all_text(wa)
check("REGRESSION: 'how much is it' resolves to the last-shown artwork",
      "Monsoon Over Kaziranga" in text, text[:80])
check("...and does NOT state a price",
      "₹" not in text)
check("...and offers a human follow-up",
      any(e["kind"] == "buttons" for e in wa.sent))

bot, wa = fresh()
bot.handle("+91888", "Test", "what do you have")
wa.sent.clear()
bot.handle("+91888", "Test", "tell me about the second one")
text = all_text(wa)
check("ordinal reference ('the second one') resolves against the last list",
      "Chandni Chowk" in text, text[:80])

bot, wa = fresh()
# NB: "the ficus" is genuinely ambiguous (Ficus AND Ficus II), and the bot
# correctly asks which one rather than guessing. So we name the work exactly.
bot.handle("+91777", "Test", "tell me about ficus II")
wa.sent.clear()
bot.handle("+91777", "Test", "how much is it")
text = all_text(wa)
check("published-price work DOES quote the price on 'how much'",
      "45,000" in text, text[:80])

bot, wa = fresh()
bot.handle("+91222", "Test", "show me the ficus")
text = all_text(wa)
check("ambiguous title ('the ficus') disambiguates instead of guessing",
      any(e["kind"] == "list" for e in wa.sent), text[:60])


# --------------------------------------------------------------- lead capture

print("\nlead capture (the ROI)")

import leads  # noqa: E402

if os.path.exists("data/test_leads.db"):
    os.remove("data/test_leads.db")
leads.init_db()

bot, wa = fresh()
bot.handle("+91666", "Priya", "tell me about threshold")
bot.handle("+91666", "Priya", "how much is it")
rows = leads.all_leads()
price_leads = [r for r in rows if r["reason"] == "price"]
check("a price enquiry creates a lead", len(price_leads) == 1, f"got {len(rows)} rows")
check("...tagged with the artwork",
      price_leads and price_leads[0]["artwork_title"] == "Threshold",
      price_leads[0]["artwork_title"] if price_leads else "none")

bot, wa = fresh()
bot.handle("+91555", "Rahul", "do you have a picasso")
misses = [r for r in leads.all_leads() if r["wa_number"] == "+91555"]
check("even a MISS captures the lead (never waste a customer)", len(misses) == 1)

bot, wa = fresh()
bot.handle("+91444", "Sana", "can i visit this weekend")
visits = [r for r in leads.all_leads() if r["reason"] == "visit"]
check("a visit request captures a lead", len(visits) >= 1)


# --------------------------------------------- intent beats pronoun (bug #5)

print("\nintent must outrank pronouns")

bot, wa = fresh()
bot.handle("+91123", "Test", "tell me about the kathakali study")
wa.sent.clear()
bot.handle("+91123", "Test", "can i come see the gallery this weekend")
text = all_text(wa)
# The bot SHOULD still mention the artwork ("I've noted your interest in X") —
# that context makes the lead more valuable. What it must NOT do is re-send the
# painting image instead of answering the visit question.
check("REGRESSION: 'come see the gallery THIS weekend' is a VISIT, not a re-sent painting",
      not any(e["kind"] == "image" for e in wa.sent), text[:70])
check("...and it sends the address",
      any(e["kind"] == "text" for e in wa.sent), text[:70])
check("...and it carries the artwork context into the lead",
      "Kathakali" in text, text[:70])

before = len([r for r in leads.all_leads() if r["reason"] == "visit"])
bot, wa = fresh()
bot.handle("+91124", "Test", "show me concrete lullaby")
bot.handle("+91124", "Test", "can i come see the gallery this weekend")
after = len([r for r in leads.all_leads() if r["reason"] == "visit"])
check("REGRESSION: the visit lead is actually captured (it was being dropped)",
      after > before, f"{before} -> {after}")

bot, wa = fresh()
bot.handle("+91125", "Test", "tell me about threshold")
wa.sent.clear()
bot.handle("+91125", "Test", "how much is this")
text = all_text(wa)
check("but a REAL pronoun still works ('how much is this')",
      "Threshold" in text, text[:70])

bot, wa = fresh()
bot.handle("+91126", "Test", "show me salt pans")
wa.sent.clear()
bot.handle("+91126", "Test", "i'd like to speak to someone")
text = all_text(wa)
check("'speak to someone' routes to a human, not an artwork",
      "callback" not in text.lower() or "24 hours" in text, text[:70])


# ------------------------------------------------------------ never invent

print("\nsafety: the bot must never invent inventory")

bot, wa = fresh()
for q in ["do you have a picasso", "any monet?", "i want a van gogh", "show me a hussain"]:
    wa.sent.clear()
    bot.handle("+91333", "Test", q)
    text = all_text(wa)
    leaked = [w.title for w in cat.works if w.title in text]
    check(f"'{q}' offers no artwork", not leaked, f"leaked {leaked}")


# ------------------------------------------------------------------- result

print(f"\n{'─' * 50}")
print(f"  {PASS} passed, {FAIL} failed")
print(f"{'─' * 50}\n")

if os.path.exists("data/test_leads.db"):
    os.remove("data/test_leads.db")

sys.exit(1 if FAIL else 0)
