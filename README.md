# Silicon 1C handover

A package for the person who owns the 1C database behind the Silicon sales bot: the HTTP contract, a Docker test stand, and the instructions for creating a counterparty and an invoice.

## Problem

The bot can quote and collect company details, but the accounting document has to appear in 1C as a real “invoice to customer”, with the company’s own print form. That print form and the chart of accounts live in 1C, not in the bot. The integrator needed a written contract and a stand that does not touch the live database.

## What was built

- `handover_1c/` describes the HTTP API the bot calls: find or create a counterparty by EDRPOU, create an invoice, download the PDF.
- A Docker compose file stands up the bot against a mock of those endpoints so the 1C side can be developed without Telegram traffic.
- The bot code in this tree is the same integrator the public Silicon case describes, trimmed for the handover.

The live base used by the shop is BAS. The published service is `hs/bot-api/v1` with Basic auth. The bot posts the payer, the line items, and the amount. 1C returns the document id and number. The PDF the customer receives is whatever that service prints. Stamp, signature, and the supplier bank block are part of the 1C print template, not of this repository.

## How it works

Search is `GET /counterparty?code=`. Create is `POST /counterparty`. The invoice is `POST /invoice` with `counterparty_id`, `items`, and `amount`. The file is `GET /invoice/{id}/pdf`. If the code already exists, create returns 409 and the existing id is reused. Unknown product names are rejected, so the line title has to match an item in the 1C catalog.

## Stack

Python, FastAPI, Docker, the 1C HTTP service.

## Run locally

```bash
docker compose -f docker-compose.1c-test.yml up -d --build
```

Use the example env. Do not copy passwords from a working server into this tree.

## What this public copy leaves out

`ACCESS.txt`, test databases, and every `.env`. The private working bot keeps those.
