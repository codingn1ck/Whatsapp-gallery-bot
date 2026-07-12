# Gallery WhatsApp Bot

An inbound WhatsApp assistant for an art gallery. Answers artwork enquiries with
image + details, holds a conversation, and — the part that actually makes money —
captures every enquiry as a lead with a phone number attached.

Built direct on Meta's WhatsApp Cloud API. **No BSP, no middleman, no ₹1,500–5,999
per month subscription.** The only unavoidable cost is Meta's per-message fee,
and for an inbound-first bot like this one, that is close to zero.

---

## Run it right now (no accounts, no keys, no money)

```bash
pip install flask requests rapidfuzz
python3 simulate.py --script      # canned conversation, good for screenshots
python3 simulate.py               # interactive — type as a customer
python3 tests/test_bot.py         # 24 tests
```

Everything runs in mock mode by default. You can build, test, demo and screenshot
this entire bot before Meta Business Verification finishes — which, being the
slowest step in the project, is exactly the point.

---

## What it does

| Customer says | Bot does |
|---|---|
| "hi" | Greets, offers three buttons |
| "what do you have?" | Sends a tappable list of available works |
| "tell me about the monsoon painting" | Sends the image + artist, medium, size, year, description, status |
| **"how much is it?"** | Resolves *it* to the last work shown → **"price on request"** → **captures a price lead** |
| "the second one" | Resolves against the last list it offered |
| "anything by Menon" | Lists that artist's works |
| "do you have a Picasso?" | **Says no.** Captures the lead. Never invents inventory. |
| "can I visit this weekend?" | Sends address + hours, captures a visit lead |
| "I want to speak to someone" | Promises a callback within 24 hours, captures the lead |

---

## Three rules baked into the code

**1. Never state a price unless explicitly allowed.**
Indian galleries withhold prices deliberately — it preserves negotiating room and
differential pricing by buyer. A bot that blurts a number destroys the thing the
business runs on. Prices are hidden per-artwork via `price_visible` in the CSV.
A price question is not a failure; it is the **highest-intent lead the bot can
produce**, and it goes straight to a human.

**2. Never invent an artwork.**
Every artwork id the language model returns is verified against the real catalog
before use. Hallucinated ids are silently discarded. A bot that offers a collector
a Picasso the gallery does not own is worse than no bot.

**3. Never waste an enquiry.**
Every dead end captures a lead. "I couldn't find that" is followed by a name, a
number, and a row in the database. That is the entire ROI.

---

## Files

```
app/
  catalog.py    Artwork records + free-text lookup (fuzzy, with a token gate)
  llm.py        Provider-agnostic intent classifier: mock | gemini | claude
  whatsapp.py   Cloud API client + a mock that prints to console
  session.py    Conversation memory — what "it" and "the second one" refer to
  flows.py      The bot's actual behaviour. This file is the product.
  leads.py      SQLite lead capture + CSV export for the owner
server.py       Flask webhook (Meta handshake, HMAC verification, dedupe)
simulate.py     Run the whole bot in your terminal, no accounts needed
tests/          24 tests, including the bugs that actually bit
data/catalog.csv   15 fake paintings — swap for the real ones
```

---

## Going live: the Meta path

**Do this in the gallery's name, not yours.** The WhatsApp Business Account must be
registered to the gallery's business, verified with the gallery's GST/registration
documents, on the gallery's Meta Business Manager. You get added as a partner with
developer access. If you register it under yourself, you become an unremovable
single point of failure — and you destroy the "you own it" pitch that is your only
real differentiator against the SaaS competition.

1. **Meta Business Manager** (business.facebook.com) — the gallery creates one.
   No verification needed to start.
2. **Meta App Dashboard** (developers.facebook.com) → create an app → add the
   **WhatsApp** product. You immediately get a free test number and 1,000 free
   service conversations a month. **This is enough for the entire proof of concept.**
3. Copy `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` from **API Setup**.
4. Copy the **App Secret** from App Settings → Basic → `WHATSAPP_APP_SECRET`.
5. Invent any string for `WHATSAPP_VERIFY_TOKEN`.
6. Expose the webhook publicly (`ngrok http 8080` for testing) and register
   `https://your-url/webhook` in Meta's webhook config with that verify token.
   Subscribe to the **messages** field.
7. Set `WHATSAPP_MODE=cloud`, restart, message the test number.

**Business Verification** (needed for a real number and higher limits) takes days
to weeks for an Indian entity and gets rejected for mismatched names/addresses.
Start it early; it is the critical path, not the code.

**A number used for the Cloud API can never be used in the normal WhatsApp or
WhatsApp Business app again.** Use a fresh number.

---

## What this costs to run

| | |
|---|---|
| Meta — customer-initiated replies (24h window) | **Free.** First 1,000/month free. |
| Meta — free-form text, images, buttons in-window | **Free** |
| Meta — marketing template (outbound, India) | ₹0.8631 each |
| Meta — utility template | ₹0.1150 each |
| LLM classification | ~₹0.02–0.10 per message (or ₹0 in mock mode) |
| Hosting | ₹400–1,500/month VPS |

This bot is **inbound-first by design**, so it lives inside the free service window.
Monthly platform spend for a gallery is realistically a few hundred rupees.

Compare: **AiSensy ₹1,500/mo · Wati ₹2,199–5,999/mo · Interakt ~₹2,566/mo** —
plus, on some, a markup on Meta's own per-message rates.

That subscription is the margin this build captures.

---

## The honest sales position

On price alone, a one-time build only beats a ₹1,500/mo SaaS somewhere around
**year 3–4**. Do not pretend otherwise; the client can do arithmetic.

Sell it on what SaaS cannot give:
- **Ownership** — source code, their Meta account, their data
- **Customisation** — the price-on-request rule above is a gallery-specific
  business rule no generic BSP will build for you
- **No per-message markup**
- **No forced pricing or feature changes**

And sell it as **one-time build + annual maintenance (15–20% of build)**, never a
bare one-time fee. Platform APIs change, things break, and "what if you disappear"
is a legitimate question. Hand over the source, register the accounts in the
client's name, and charge for the AMC honestly.

---

## Swapping in the real gallery

1. Replace `data/catalog.csv` — same columns.
2. Upload artwork photos somewhere public; set `IMAGE_BASE_URL`.
3. Set `GALLERY_NAME`, `GALLERY_ADDRESS`, `GALLERY_HOURS`.
4. Mark which works may show a price (`price_visible=yes`).
5. Run the tests. Run the simulator. Then go live.
