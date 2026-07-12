#!/usr/bin/env python3
"""
Local simulator.

Runs the entire bot in your terminal. No Meta account, no WhatsApp number,
no API key, no webhook, no ngrok. Just:

    python3 simulate.py            # interactive
    python3 simulate.py --script   # canned conversation, good for screenshots

This exists so the bot can be built, tested and demoed while Meta Business
Verification is still pending — which, per the research, is the slowest thing
in the whole project.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))

from catalog import Catalog          # noqa: E402
from flows import Bot                # noqa: E402
from leads import export_csv, init_db, open_leads  # noqa: E402
from llm import get_llm              # noqa: E402
from whatsapp import MockWhatsApp    # noqa: E402

CUSTOMER = "+919876543210"
CUSTOMER_NAME = "Ananya"

SCRIPT = [
    "hi",
    "what do you have?",
    "tell me about the monsoon painting",
    "how much is it?",
    "do you have anything by menon",
    "is the kathakali one available",
    "do you have a picasso",
    "can i come see the gallery this weekend",
    "i'd like to speak to someone",
]


def banner(text: str) -> None:
    print(f"\n{'═' * 66}\n  {text}\n{'═' * 66}")


def main() -> None:
    catalog = Catalog.load("data/catalog.csv")
    llm = get_llm()
    wa = MockWhatsApp()
    bot = Bot(catalog, llm, wa)
    init_db()

    provider = os.getenv("LLM_PROVIDER", "mock")
    banner(
        f"{os.getenv('GALLERY_NAME', 'The Gallery')} — WhatsApp bot simulator\n"
        f"  catalog: {len(catalog.works)} works · "
        f"llm: {provider} · whatsapp: mock"
    )

    scripted = "--script" in sys.argv

    if scripted:
        for msg in SCRIPT:
            print(f"\n  {CUSTOMER_NAME} → {msg}")
            bot.handle(CUSTOMER, CUSTOMER_NAME, msg)
    else:
        print("\n  Type a message as a customer. Ctrl-C or 'quit' to exit.")
        print("  Button taps: type the button id (browse / visit / human / art:A001)\n")
        while True:
            try:
                msg = input(f"  {CUSTOMER_NAME} → ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if msg.lower() in {"quit", "exit"}:
                break
            if msg:
                bot.handle(CUSTOMER, CUSTOMER_NAME, msg)

    leads = open_leads()
    banner(f"LEADS CAPTURED: {len(leads)}")
    for r in leads:
        art = f" · {r['artwork_title']}" if r["artwork_title"] else ""
        print(f"  #{r['id']}  {r['reason']:9} {r['wa_number']}{art}")
        print(f"          \"{(r['message'] or '')[:60]}\"")
    if leads:
        path = export_csv()
        print(f"\n  Exported to {path}")
    print()


if __name__ == "__main__":
    main()
