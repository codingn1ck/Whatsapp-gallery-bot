"""
WhatsApp Cloud API client.

Two implementations behind one interface:

    MockWhatsApp  - prints messages to the console. No Meta account, no key,
                    no phone number. This is how the entire bot gets built
                    and tested before Business Verification ever completes.
    CloudWhatsApp - the real Meta Graph API.

Platform facts baked into this file (they constrain the design, so they are
worth stating in code rather than in a forgotten doc):

  - Free-form messages (text, images, interactive buttons) may ONLY be sent
    inside the 24-hour service window, i.e. after the customer messages first.
    Outside it, you may send ONLY a pre-approved template, and Meta charges
    for it. This bot is inbound-first by design, so it lives inside the window
    and costs approximately nothing.
  - Interactive reply buttons: max 3, title max 20 chars.
  - Interactive list rows: max 10 per section, title max 24 chars.
  - Images can be sent by public URL or by uploaded media id. URL is simpler;
    the gallery's own website or an S3/Cloudinary bucket works fine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

GRAPH_VERSION = "v21.0"

BUTTON_TITLE_MAX = 20
LIST_TITLE_MAX = 24
LIST_ROW_MAX = 10


@dataclass
class Button:
    id: str
    title: str

    def payload(self) -> dict:
        return {
            "type": "reply",
            "reply": {"id": self.id, "title": self.title[:BUTTON_TITLE_MAX]},
        }


@dataclass
class ListRow:
    id: str
    title: str
    description: str = ""

    def payload(self) -> dict:
        row = {"id": self.id, "title": self.title[:LIST_TITLE_MAX]}
        if self.description:
            row["description"] = self.description[:72]
        return row


class WhatsAppClient(Protocol):
    def send_text(self, to: str, body: str) -> None: ...
    def send_image(self, to: str, image_url: str, caption: str) -> None: ...
    def send_buttons(self, to: str, body: str, buttons: list[Button]) -> None: ...
    def send_list(
        self, to: str, body: str, button_label: str, rows: list[ListRow]
    ) -> None: ...


# ---------------------------------------------------------------- mock

@dataclass
class MockWhatsApp:
    """
    Prints instead of sending. Also records everything, so tests can assert
    on what the bot *would* have sent.
    """

    sent: list[dict] = field(default_factory=list)
    verbose: bool = True

    def _record(self, kind: str, to: str, **kw) -> None:
        entry = {"kind": kind, "to": to, **kw}
        self.sent.append(entry)
        if not self.verbose:
            return
        print(f"\n  ┌─ BOT → {to}  [{kind}]")
        if kw.get("image_url"):
            print(f"  │ 🖼  {kw['image_url']}")
        body = kw.get("body") or kw.get("caption") or ""
        for line in body.split("\n"):
            print(f"  │ {line}")
        if kw.get("buttons"):
            btns = " ".join(f"[ {b.title} ]" for b in kw["buttons"])
            print(f"  │ {btns}")
        if kw.get("rows"):
            print(f"  │ ▾ {kw.get('button_label', 'Choose')}")
            for r in kw["rows"]:
                print(f"  │    • {r.title}")
        print("  └─")

    def send_text(self, to: str, body: str) -> None:
        self._record("text", to, body=body)

    def send_image(self, to: str, image_url: str, caption: str) -> None:
        self._record("image", to, image_url=image_url, caption=caption)

    def send_buttons(self, to: str, body: str, buttons: list[Button]) -> None:
        self._record("buttons", to, body=body, buttons=buttons[:3])

    def send_list(
        self, to: str, body: str, button_label: str, rows: list[ListRow]
    ) -> None:
        self._record(
            "list", to, body=body, button_label=button_label, rows=rows[:LIST_ROW_MAX]
        )


# ---------------------------------------------------------------- real

@dataclass
class CloudWhatsApp:
    """Meta WhatsApp Cloud API. Direct — no BSP, no per-message middleman."""

    access_token: str
    phone_number_id: str

    @property
    def _url(self) -> str:
        return f"https://graph.facebook.com/{GRAPH_VERSION}/{self.phone_number_id}/messages"

    def _post(self, payload: dict) -> None:
        import requests

        try:
            resp = requests.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=12,
            )
            if resp.status_code >= 400:
                print(f"[whatsapp] send failed {resp.status_code}: {resp.text[:300]}")
        except Exception as exc:  # noqa: BLE001 - never crash on a send
            print(f"[whatsapp] send error: {exc}")

    def _base(self, to: str) -> dict:
        return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}

    def send_text(self, to: str, body: str) -> None:
        self._post({**self._base(to), "type": "text", "text": {"body": body}})

    def send_image(self, to: str, image_url: str, caption: str) -> None:
        self._post({
            **self._base(to),
            "type": "image",
            "image": {"link": image_url, "caption": caption},
        })

    def send_buttons(self, to: str, body: str, buttons: list[Button]) -> None:
        self._post({
            **self._base(to),
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {"buttons": [b.payload() for b in buttons[:3]]},
            },
        })

    def send_list(
        self, to: str, body: str, button_label: str, rows: list[ListRow]
    ) -> None:
        self._post({
            **self._base(to),
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body},
                "action": {
                    "button": button_label[:BUTTON_TITLE_MAX],
                    "sections": [
                        {
                            "title": "Artworks",
                            "rows": [r.payload() for r in rows[:LIST_ROW_MAX]],
                        }
                    ],
                },
            },
        })


# ---------------------------------------------------------------- factory

def get_whatsapp() -> WhatsAppClient:
    """
        WHATSAPP_MODE=mock|cloud
        WHATSAPP_TOKEN=...
        WHATSAPP_PHONE_NUMBER_ID=...
    """
    mode = os.getenv("WHATSAPP_MODE", "mock").strip().lower()
    if mode == "cloud":
        token = os.getenv("WHATSAPP_TOKEN", "").strip()
        pnid = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        if not (token and pnid):
            print("[whatsapp] cloud mode requested but credentials missing — using mock")
            return MockWhatsApp()
        return CloudWhatsApp(token, pnid)
    return MockWhatsApp()
