#!/usr/bin/env python3
"""
Webhook server — the production entry point.

Meta POSTs every inbound WhatsApp message here. Two endpoints:

    GET  /webhook   Meta's one-time verification handshake
    POST /webhook   inbound messages

Security notes that matter:
  - Meta signs every POST with HMAC-SHA256 using your App Secret, in the
    X-Hub-Signature-256 header. We verify it. Without this check, anyone who
    learns your URL can inject fake customer messages and poison the lead
    database. This is not optional.
  - Meta RETRIES on non-200. So we always return 200 fast and never let an
    exception escape — a 500 here means Meta redelivers the same message and
    the customer gets the reply twice.
  - Meta redelivers on timeout too. Real deployments should dedupe on the
    message id; a simple seen-set is included.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))

from flask import Flask, request  # noqa: E402

from catalog import Catalog       # noqa: E402
from flows import Bot             # noqa: E402
from leads import init_db         # noqa: E402
from llm import get_llm           # noqa: E402
from session import SessionStore  # noqa: E402
from whatsapp import get_whatsapp # noqa: E402

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "change-me")
APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
CATALOG_PATH = os.getenv("CATALOG_PATH", "data/catalog.csv")

app = Flask(__name__)

catalog = Catalog.load(CATALOG_PATH)
bot = Bot(catalog, get_llm(), get_whatsapp(), SessionStore())
init_db()

# Meta redelivers messages. Remember what we've already handled.
_seen: deque[str] = deque(maxlen=1000)
_seen_set: set[str] = set()


def already_handled(msg_id: str) -> bool:
    if msg_id in _seen_set:
        return True
    if len(_seen) == _seen.maxlen:
        _seen_set.discard(_seen[0])
    _seen.append(msg_id)
    _seen_set.add(msg_id)
    return False


def signature_ok(raw_body: bytes) -> bool:
    """Verify Meta's HMAC. Fail closed if a secret is configured."""
    if not APP_SECRET:
        # No secret set = local/dev. Warn loudly but allow.
        print("[webhook] WARNING: WHATSAPP_APP_SECRET unset — signature not verified")
        return True
    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(
        APP_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@app.get("/webhook")
def verify():
    """Meta's subscription handshake. Echo the challenge if the token matches."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("[webhook] verified by Meta")
        return challenge, 200
    print("[webhook] verification FAILED — check WHATSAPP_VERIFY_TOKEN")
    return "forbidden", 403


@app.post("/webhook")
def inbound():
    raw = request.get_data()

    if not signature_ok(raw):
        print("[webhook] bad signature — rejected")
        return "forbidden", 403

    try:
        _process(request.get_json(silent=True) or {})
    except Exception as exc:  # noqa: BLE001
        # NEVER 500. Meta would redeliver and the customer would be replied
        # to twice. Log it, swallow it, move on.
        print(f"[webhook] handler error (swallowed): {exc}")

    return "ok", 200


def _process(payload: dict) -> None:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Delivery/read receipts arrive here too. Ignore them.
            messages = value.get("messages")
            if not messages:
                continue

            contacts = {c["wa_id"]: c.get("profile", {}).get("name")
                        for c in value.get("contacts", [])}

            for msg in messages:
                msg_id = msg.get("id", "")
                if msg_id and already_handled(msg_id):
                    print(f"[webhook] duplicate {msg_id} — skipped")
                    continue

                number = msg.get("from", "")
                name = contacts.get(number)
                text = _extract_text(msg)

                if not (number and text):
                    continue

                print(f"[webhook] {name or number}: {text}")
                bot.handle(number, name, text)


def _extract_text(msg: dict) -> str:
    """Pull usable text out of whichever message type arrived."""
    mtype = msg.get("type")

    if mtype == "text":
        return msg.get("text", {}).get("body", "")

    if mtype == "interactive":
        inter = msg.get("interactive", {})
        # A button tap or list selection comes back as the id we sent.
        if "button_reply" in inter:
            return inter["button_reply"].get("id", "")
        if "list_reply" in inter:
            return inter["list_reply"].get("id", "")

    if mtype == "button":
        return msg.get("button", {}).get("payload", "")

    # Images, audio, documents: acknowledge rather than ignore.
    if mtype in ("image", "audio", "document", "video", "sticker"):
        return "__media__"

    return ""


@app.get("/health")
def health():
    return {"ok": True, "works": len(catalog.works)}, 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"[webhook] listening on :{port} · {len(catalog.works)} works loaded")
    app.run(host="0.0.0.0", port=port)
