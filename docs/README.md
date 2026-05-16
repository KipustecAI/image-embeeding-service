# Docs — LLM-Maintained Wiki

This directory follows the [LLM Wiki pattern](llmwiki.md) — a structured, interlinked collection of markdown files that an LLM agent maintains as a persistent, compounding knowledge base. The agent owns the wiki content; humans read it, ask questions of it, and direct what to capture.

**If you're a human looking for something, start at [index.md](index.md).** The index is a categorized catalog of every page.

**If you're an LLM working on this codebase, read this file in full.** It is the schema — the operating manual for how to use, update, and keep this directory healthy.

---

## The three layers

Per the wiki pattern, each markdown file in `docs/` belongs to one of three layers. The layer determines who writes it and how it changes:

### 1. Schema (immutable convention)

The meta-documents that define how the wiki itself works. These are co-evolved by humans and the LLM but change rarely.

| File | Purpose |
|---|---|
| [README.md](README.md) | This file — the schema. Conventions, workflows, layer definitions. |
| [llmwiki.md](llmwiki.md) | The seed pattern, copied in from external source. Don't edit — quote it instead. |
| [index.md](index.md) | Content-oriented catalog of every wiki page, grouped by category. Updated on every page added/removed. |
| [log.md](log.md) | Chronological event log. Appended on every ingest, decision, or lint pass. |

### 2. Wiki (LLM-owned, current state)

Pages that describe **what's true right now** about the system — the current API surface, the current architecture, the current contracts with other teams. Each page is an entity or concept page; the LLM updates it whenever a change to that entity ships in code.

Wiki pages are the *single source of truth for the current state*. If the wiki and the code disagree, fix the wiki (or the code, whichever is wrong) and log the discrepancy.

Wiki pages are interlinked liberally — every mentioned entity should be a link to its own page, every contract should link to its current shape, every feature should link to the related code paths.

### 3. Raw sources (immutable history)

Documents that captured something at a point in time — a phase plan when we started a feature, an inter-team requirements doc we sent or received, an older architecture proposal. These don't change after they're written. Updates go to wiki pages, not back into the raw source.

Examples in this repo today:
- `image-blacklist/00_CONTEXT.md` through `07_*.md` — the phase plans authored before/during implementation. Useful as a record of *why we did what we did*. Pull current state into wiki pages, not back into these.
- `weapons/00_CONTEXT.md` through `05_*.md` — same pattern.
- `requirements/IMAGE_COMPUTE_STREAMS.md` and `REPORT_GENERATION_STREAMS.md` — inter-team contracts. Living docs but only updated through formal negotiation cycles, not casual edits.
- `legacy/`, `new_arq/` — older designs superseded by current architecture. Kept for historical reference only.

---

## Today's layout — where things live

This repo predates the wiki pattern. Many existing files are already wiki-shaped (current-state entity pages); a few are raw historical. The index is the authoritative classification — use it to look up which layer a file belongs to.

```
docs/
├── README.md              # this file (schema)
├── llmwiki.md             # seed pattern
├── index.md               # content catalog
├── log.md                 # chronological event log
│
├── API_REFERENCE.md       # wiki: REST API surface (canonical)
├── BLACKLIST_API.md       # wiki: blacklist feature, consumer contract
├── CURL_EXAMPLES.md       # wiki: curl cookbook
│
├── new_arq_v2/            # wiki: current architecture (overview, services, streams)
│   └── fixes/             # wiki: incident/decision write-ups
│
├── requirements/          # wiki: inter-team contracts (compute, report-gen)
│
├── weapons/               # mostly raw: phase plans 00..05 + producer CONTRACT (wiki)
│
├── image-blacklist/       # mostly raw: phase plans 00..07 + README (wiki: tracks current state with deviations)
│
├── new_arq/               # raw: superseded by new_arq_v2/
└── legacy/                # raw: very old; reference only
```

We don't move existing files mechanically. We migrate by:

1. When a wiki page becomes the single source of truth for an entity, links from raw sources are *one-directional* — the wiki links *from* the raw plan, never the other way (raw sources don't get retroactive edits).
2. New synthesis pages live alongside their topic (e.g. a "category feature current state" page would join `image-blacklist/` or `wiki/features/` once we have a `wiki/` directory).
3. When raw plans get superseded entirely, they move to `legacy/` and the wiki page subsumes them.

---

## Workflows

### Ingest (add new information)

Triggers: a feature ships, an inter-team contract changes, a new decision is made, ops runs a verification pass that surfaces something new.

Steps:

1. **Identify the touched entities.** What pages need updating? (Find them in [index.md](index.md).)
2. **Update each touched page.** Keep the page as current-state truth. If the change supersedes a claim on another page, update both.
3. **Update [index.md](index.md)** if pages were added/removed or significantly changed scope.
4. **Append to [log.md](log.md)** with the date prefix `## [YYYY-MM-DD] <type> | <one-line summary>`. Types: `ingest`, `decision`, `lint`, `verification`, `incident`.
5. **Cross-check code paths** the change touched — read them, confirm the wiki claim matches reality, fix whichever is wrong.

### Query (find an answer)

1. Read [index.md](index.md) — find the relevant pages by topic.
2. Read those pages directly (don't re-derive from raw sources unless needed).
3. If the answer required synthesis across multiple pages, **file the synthesis as a new page** so the next query benefits. Append a log entry recording the question and the answer's location.

### Lint (periodic health check)

Run when something feels out of sync. Look for:

- **Wiki ↔ code drift.** Pick a wiki claim, check `src/`. If they disagree, fix one.
- **Stale references.** Page X says "see Y for details" but Y no longer covers that — fix the link or move the content.
- **Orphan pages.** No inbound links from index or other pages — either link them in or move to raw/legacy.
- **Duplicate truth.** Two pages claim authority on the same entity — merge or designate one canonical, point the other to it.
- **Missing entities.** A concept is referenced repeatedly but has no page of its own — create one.

Capture lint findings in the log with `type=lint` so the trail is visible.

---

## Conventions

### Naming

- **kebab-case** for new wiki page filenames (`upstream-compute-contract.md`).
- **UPPER_SNAKE** is fine for top-level legacy / contract files that are widely linked (`API_REFERENCE.md`, `BLACKLIST_API.md`); don't rename them just for consistency.
- Phase plans keep their `NN_TOPIC.md` numbering — they're sequential by design.

### Front matter and prefixes

Pages don't require YAML frontmatter, but if it helps a particular page (e.g. for an Obsidian Dataview query), add it. Be sparing.

A page's first line should make its layer clear:
- Wiki page: opens with a short description of what it covers (current-state tone).
- Raw plan: opens with "Phase NN" or "Status: shipped/draft" or similar timestamp marker.

### Linking

- **Inter-doc links use relative paths** (`[../requirements/IMAGE_COMPUTE_STREAMS.md](...)`). Resilient to moves.
- **Code links should include a line number when pointing at a specific function** (`[src/main.py:202](../src/main.py)`). Line numbers go stale fast; only use them when the precision actually matters.
- Cross-link liberally. A wiki page that names an entity without linking to its page is a missed opportunity.

### Tone

Wiki pages: terse, factual, current-state. Avoid "we recently shipped" or "as of last week" — write timeless statements about what is true now. Date stamps are for the log, not the wiki.

Raw plans: keep the tone they were authored in. Don't retroactively rewrite them when reality diverges — add a "Deviations" section to the wiki or to a follow-up page.

---

## When code and docs drift

The biggest failure mode is wiki claims that no longer match what's in `src/`. Default to the code as ground truth — it's what actually runs. Update the wiki page (and log the drift) when you notice it. If the code is wrong instead, fix the code and add a note in the log.

A useful sanity routine: pick a wiki page at random when you sit down to work. Read it. Compare to `src/`. Fix anything stale.

---

## Cross-references to the codebase

- [../CLAUDE.md](../CLAUDE.md) — project-level instructions for Claude Code, separate from the wiki. Read it for environment setup, commands, and high-level architecture facts that aren't going to change.
- [../README.md](../README.md) — top-level service README. Pinned summary; the wiki is the deep version.
- [../src/](../src/) — the codebase. The wiki describes it; this is what actually runs.
- [../alembic/versions/](../alembic/versions/) — migration history. The wiki should reflect the current schema state, not the migration log.
