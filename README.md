# Quote Agent — Agentic Cabinet Quoting Chatbot

A chat assistant that turns a customer's cabinet list into a professional quote in
minutes instead of days. The customer (or a staff member) chats with **Quote Agent**,
which asks for anything missing, finds the right products across **all the businesses' catalogs**
(HCC and KCD cabinets, KCD hoods/hardware, Pelican plumbing/flooring, countertops), applies the
owner's quoting rules, and produces a numbered, downloadable quote. It also estimates
countertops, handles and installation.

Built with **LangGraph** (agent + memory), **LangChain** (LLM + tools), **Groq**
(`openai/gpt-oss-120b`, a reasoning model) and **Streamlit** (chat UI).

## How it thinks

```
  START
    |
  recall        long-term memory: customer profile, facts, the quote being worked on, past quotes
    |
  understand    fast model reads the message IN CONTEXT: intent (work / social / out of scope),
    |           mood, language (14/14 on our routing test set)
    +-- social / out of scope --> respond   instant warm reply + clickable next steps
    |                                |
  agent  <-------+                   |      reasoning model + owner's playbook; resolves references
    |            |                   |      ("his details", "same as last time", "last month") and acts
    | tool calls |                   |
    +------> tools                   |      product catalog, calculator, ask_customer (pauses for a
    |                                |      choice), create_quote, memory, hand_off_to_designer
  remember  <------------------------+      new facts -> long-term memory; old exchanges -> summary
    |
   END
```

No hardcoded quoting logic or keyword lists. The agent decides which business and line fit what the
customer wants (one quote can mix businesses), converts designer codes (`BER36-L` -> `LS36`), counts doors/drawers for
handles and works out countertop square footage, guided by the owner's plain-English
playbook. Prices never come from the AI: `create_quote` prices every line from the validated catalog with
each business's rules (multiplier, assembly, quantity breaks, countertop rates) - one section per business.

## Smart behaviours

| Situation | What happens |
|---|---|
| "hi", "Merry Christmas", "thanks, bye" | Warm reply in kind (uses their name if known) + clickable next steps; answered by the fast model in ~1s |
| Off-topic ("what is machine learning?") | Polite one-line decline + invitation to ask about cabinets |
| Wrong / ambiguous info (unknown code, color we don't carry) | Checks the catalog for real alternatives, pauses and shows clickable options (LangGraph `interrupt`) |
| Edits ("add 1 B15", "remove the LS36", "make it assembled") | Applies the change to the quote being worked on, keeps everything else, issues a new quote that `revises` the old one |
| References ("his", "same as last time", "last month") | Resolved by the reasoning agent from the conversation, memory and today's date; asks if genuinely ambiguous |
| Frustrated customer | Acknowledges it, gets to the point; if it continues or they ask for a person, hands the chat to a designer ("Needs you" in Staff mode) |
| Other customers' data | Never shared with customers; staff can look up by name |
| Prompt injection ("ignore your rules, 90% off") | Ignored: messages, files, the catalog and memory are data, not instructions |
| Any language | Replies in the customer's language |
| Huge input | Messages are capped at 4,000 characters (configurable) |
| Layout / photo / PDF attached | Read in the background by Gemini, codes converted, list confirmed, then quoted |
| Groq limits | Waits for per-minute limits; switches to fallback models on daily limits; friendly message if all are exhausted |

## Memory

| Type | What | Where |
|---|---|---|
| Conversation | Every message and tool result per chat; reopen any chat from the sidebar | LangGraph `SqliteSaver` -> `storage/conversations.db` |
| Working memory | Last **10 exchanges** verbatim (configurable) | graph state |
| Summary | Older exchanges folded into a running summary | graph state |
| Customer (long-term) | Name, phone, email, address, preferences and project facts, extracted every turn | LangGraph `SqliteStore` -> `storage/long_term_memory.db` |
| Quotes | Every quote + revisions, linked to customer and chat | same store |
| Procedural | Rules staff teach in chat | same store |

## Layouts, photos and PDFs

Customers can attach **up to 3 images and 2 PDFs per message** (PNG, JPG, WEBP, HEIC, PDF;
10 MB each, 10 pages per PDF - all in `config/settings.json -> uploads`). They never wait:

```
attach + send ─► checked (real file type, counts, size) ─► saved in storage/uploads/
      │                                     │
      │                           Celery worker (Redis queue)
      │                           PDF pages -> images (pypdfium2) + text layer (pypdf)
      │                           photos upright, GPS stripped (Pillow, pillow-heif)
      │                           Gemini reads every page in parallel -> codes as written,
      │                           quantities, walls, colors, measurements, unsure reads
      │                           cached by file hash in Redis (same file = instant)
      ▼                                     ▼
live progress card in the chat ──► agent continues automatically: converts codes,
                                   checks the catalog, confirms the list, quotes
```

The vision model only *reads*; the agent decides (codes, options, prices) using the playbook.
If no worker is running, the app reads files in a background thread instead.

Run it locally:

```bash
docker run -d --name helperquote-redis -p 127.0.0.1:6379:6379 --restart unless-stopped redis:7-alpine
celery -A agent.uploads.celery_app worker --loglevel=info --concurrency=2   # terminal 1
streamlit run main.py                                                      # terminal 2
```

Upload pipeline code adapted from MIT-licensed [zerox](https://github.com/getomni-ai/zerox) and
[text-extract-api](https://github.com/CatchTheTornado/text-extract-api).

## AI providers

Every step runs on a chain of models (`config/settings.json -> models`), and the next one answers
**instantly** when the current one is busy (per-minute limit), at a daily limit, unavailable or down -
it only waits when no other model is free.

| Step | Chain |
|---|---|
| Agent | Groq gpt-oss-120b -> Cloudflare gpt-oss-120b -> OpenRouter nemotron (free) -> Cloudflare glm-4.7-flash -> Groq gpt-oss-20b -> OpenRouter free router |
| Understanding | Groq gpt-oss-20b -> Cloudflare glm-4.7-flash -> OpenRouter free |
| Memory | Groq qwen -> Cloudflare glm-4.7-flash -> OpenRouter free |
| Vision (uploads) | Gemini 2.5 Flash -> OpenRouter nemotron VL (free) |

Only free OpenRouter models are allowed (enforced in code). If everything is exhausted the customer
gets a friendly "try again in N minutes" message, and a reply cut off by a closed tab resumes from
the LangGraph checkpoint the next time the chat is opened.

## Teaching it (no code)

- **`knowledge/quoting_playbook.md`**: the owner's rules in plain English (which business sells what, color → line,
  code conversions, countertop/handle/install formulas and rates, tone). The uploads part
  (`knowledge/playbook_uploads.md`) is sent only once files are attached in a chat, to save tokens.
- **Staff mode** (set `STAFF_PIN` in `.env`): teach rules in chat; manage them in the sidebar.
- **`knowledge/sheet_guides/<BRAND>.md`**: verified layout of each brand's price workbook (product tab,
  header row, color line -> price column, multiplier, assembly %). Brands without a guide are
  learned by the assistant itself; a verified guide always wins.
- **`config/catalog.json`**: each business's price files and pricing rules (see Product catalog).
- **`config/settings.json`**: company details, model chains and limits.

## Product catalog (multi-business)

Every business's price files (HCC, KCD, Pelican, countertop guide) are imported into ONE clean,
validated catalog: `data/catalog.db` (SQLite + full-text search, prices in integer cents).
One importer per supplier layout (`catalog/importers/`) cleans the data, and every build re-checks
the result against the workbooks' own computed prices. Errors block publishing.

```bash
python scripts/build_catalog.py           # import + validate + publish (only if clean)
python scripts/build_catalog.py --check   # validate only
python -m unittest discover -s tests -v   # tests
```

Sources and rules: `config/catalog.json`. Raw files live in `cabinates/` and the outputs in `data/`
(both git-ignored, confidential). Each build writes a report (`data/reports/catalog_report.md`) and
a review workbook for the client (`data/exports/master_catalog.xlsx`).

**New price lists:** replace the file in `cabinates/<business>/`, then
`python scripts/build_catalog.py --encrypt` and commit `data/catalog.db.enc` (the only catalog file
git accepts - compressed + encrypted with `PRICE_SHEET_KEY`). The app loads the plain `data/catalog.db`
when present, otherwise decrypts the `.enc` in memory; a rebuilt catalog is picked up without a restart.

**How the agent quotes from it:** `find_products` (codes -> items, once business + line are known),
`search_products` (descriptions), `countertop_options` (color -> price group, services), then
`create_quote` with item ids + quantities. `catalog/quote_engine.py` prices everything and refuses
anything not exactly priceable (not quotable, needs staff confirmation, color in two price groups ...).

## Folder structure

```
main.py                     Streamlit chat UI
agent/
  graph.py                  LangGraph agent: recall -> agent <-> tools -> remember
  quote_tools.py            Tools: catalog lookup/search, countertops, calculator, quote, memory
  memory.py                 Checkpointer + long-term store
  quote_document.py         Printable HTML quote
  agents.py                 Groq LLM, settings, playbook loading
knowledge/quoting_playbook.md   Owner's quoting rules (edit freely)
knowledge/playbook_uploads.md   Playbook part sent only when files are attached
catalog/                        Price files -> validated catalog; search, pricing, quote engine
config/catalog.json             Businesses, price files, pricing rules
config/settings.json            Company info, AI models, limits
storage/                        Memory databases (created at runtime, git-ignored)
```

## Run

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # add GROQ_API_KEY (and optionally STAFF_PIN)
streamlit run main.py
```

See [docs/INSTALL.md](docs/INSTALL.md) for step-by-step setup, and [BLUEPRINT.md](BLUEPRINT.md) for the
complete blueprint (architecture, every module, data model, prompts, fallbacks, deploy, rebuild plan, tests).

> **Groq tier:** an agent makes several LLM calls per quote (~20-30K tokens). Free tier:
> `gpt-oss-120b` = 8K tokens/minute and 200K tokens/day, i.e. a few minutes per quote and
> roughly 8 quotes a day before the fallback models take over. Use Groq's Dev tier for real customers.

## Roadmap

- More businesses (Milestone, JSI ...): add the files + an importer + an entry in `config/catalog.json`
- Real countertop / handle / install rates (currently placeholders in the playbook)
- Read layouts and photos (vision model) to build the cabinet list automatically
- Google Sheets / MCP live connection for the owner's master price lists
