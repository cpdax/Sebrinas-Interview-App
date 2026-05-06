# Questionnaire Builder — Design Notes (Paused May 6, 2026)

## Status
**Paused.** Decided to ship cookie-based persistent auth + GitHub Actions keep-warm pinger first because Sebrina was at a conference using the current app and complained about (a) repeated logins and (b) cold-start slowness. Resume questionnaire work after that ships and stabilizes.

## Resume command
"Read `Projects/Sebrinas Interview App/questionnaire_design.md` and let's pick up the questionnaire feature where we left off."

---

## Feature scope

Sebrina (and eventually her team) wants to build her own questionnaires inside the app. Each questionnaire:
- Has a name and a description
- Is assigned to ChildPlus, Procare, or both
- Contains an ordered list of typed questions
- Is versioned — old captures stay linked to the version of the questionnaire they used at capture time

When starting a session, after picking destination + mode, user gets a **questionnaire picker** showing only questionnaires assigned to that destination, plus a "No questionnaire — freeform notes only" option.

Selected questionnaire's questions render *above* the existing freeform Notes section. Freeform Notes section stays — it's an "anything else" catch-all per Dax's instruction.

---

## Design decisions locked in

### Architecture: in-app editor (NOT Confluence-page-as-format)

Rejected: Sebrina edits Confluence pages with strict format → app parses. Too fragile, format errors break things, would require Dax to be helpdesk every time a question gets added.

Chosen: **In-app admin section** with a friendly editor (+ New Questionnaire, dropdown for question types, etc.). Storage is invisible to Sebrina; she manipulates questionnaires through the UI only.

### Storage: one Confluence page, JSON code block

All questionnaires stored as a single JSON document inside a code block on one Confluence page. The page lives in a structured location, e.g. `Sebrina's Customer Notes / Questionnaires`.

App reads the page on load, parses the JSON, presents the list. When admin creates/edits in the app, app rewrites the JSON and PUTs the page back via the same Confluence REST API used for note saves.

Sebrina can see the page in Confluence (audit trail, version history built-in) but doesn't edit it directly.

### Auth: two-password approach

- `APP_PASSWORD` — existing, lets users into the capture form
- `ADMIN_PASSWORD` — new, additionally unlocks the "Manage Questionnaires" admin section

Both passwords gate access; only admin password unlocks the editor. **No real user accounts in v1.**

**Flagged for future:** if the user count grows past ~5 or there's ever a need for per-user audit ("who edited which questionnaire," "who ran which session"), we revisit and build real auth (separate database, real accounts). Don't pre-build for that — wait until the need is real.

### Versioning

Each questionnaire has an auto-incrementing `version` integer. Any edit bumps the version.

Each saved capture session stamps:
1. `questionnaire_id` — stable across versions
2. `questionnaire_version` — what version was used at capture time
3. The full text of the questionnaire as JSON inside the answer blob — so old captures are readable without resolving the version reference

### Question types for v1 (5 starters)

Confirmed minimum viable set:
1. **Short text** — single-line input
2. **Long text** — multi-line textarea (like the existing freeform notes)
3. **Yes/No** — radio buttons
4. **Single choice from list** — dropdown; admin defines the options when creating the question
5. **Rating 1–5** — slider or radio buttons (decide at build time)

**Skipped for v1, easy to add later:**
- Multi-select (checkbox list)
- Date picker
- File upload
- Conditional logic ("if Q1 = yes, show Q3")
- Number input (use short text for now)
- Time input

### Required vs optional

Each question has a `required: true/false` toggle (default `false`). Required questions block save until answered. Validation on submit.

### Capture-flow buttons after a successful save

Two buttons replace the current single "+ Capture another":
- **"Same setup (new contact)"** — preserves destination + mode + questionnaire choice, resets contacts/notes/tags/answers
- **"Start over"** — fully resets except auth (current behavior)

When using "Same setup," the questionnaire picked is the **latest version** of that questionnaire, not whatever version was used last time. This means if Sebrina edited the questionnaire between captures, the new capture uses the new version. Versioning preserves old data; new captures use current.

### Storage of captured answers in the Confluence notes table

Three new columns added to the **Full Detail table** (Summary table stays as-is):
- `QuestionnaireID` — stable identifier
- `QuestionnaireVersion` — version at capture time
- `QuestionnaireAnswers` — formatted Q&A text (one Q+A pair per line, human-readable in Confluence, machine-parseable for export)

Bootstrap logic must gracefully handle existing pages — adapt the table structure rather than requiring manual cleanup.

### Editor UX details

- **Reorder questions**: up/down arrow buttons (drag-and-drop is more code, save for later)
- **Delete a questionnaire**: BLOCKED if it's been used in any capture; "Archive" instead, which hides from the picker but preserves history. Hard delete only allowed for never-used questionnaires.
- **Picker UI**: big buttons (like destination/mode) for v1. Switch to dropdown when she has 6+ questionnaires (probably never explicitly switches — just a future call).
- **Default questionnaire?**: No. Sebrina creates her first one before she can pick one (or she picks "freeform only").

### What stays unchanged

- HubSpot lookup (solo + group modes)
- Audio recording + review
- Tags (still simplified stopgap version)
- Confluence two-table save layout
- Password gate (will need to also check ADMIN_PASSWORD)

---

## Build sequence (3 phases)

**Phase 1 — Storage + admin editor (no end-user changes yet)**
- Add `ADMIN_PASSWORD` to secrets
- Add admin-mode detection (if entered password = admin password, set admin flag)
- Build "Manage Questionnaires" section behind admin gate
  - List view (table of all questionnaires, name + destinations + version)
  - Create/edit form (name, description, destinations, ordered questions with types and required flags)
  - Delete/archive logic
- Storage helpers: read JSON from Confluence questionnaires page, write JSON back
- Test: Sebrina (with admin password) can create, edit, delete a questionnaire. Non-admin password skips the section entirely.

**Phase 2 — End-user picker + question rendering**
- New step after mode selection: questionnaire picker (filtered by destination)
- "No questionnaire — freeform notes only" option always present
- Render selected questionnaire's questions above the freeform Notes section
- Validate required questions on save
- Test: end-to-end capture with a questionnaire works; freeform-only path still works.

**Phase 3 — Storage of answers in the Confluence notes table**
- Add 3 new columns to Full Detail table
- Modify bootstrap and append logic to handle the new column count
- Format Q&A blob per the agreed structure
- Test: saving a questionnaire-driven capture writes answers to the right place; export is human-readable.

Each phase merges to main only after Sebrina or Dax tests on the preview URL.

---

## Open items / things to revisit

- **Multi-user / real auth** — flagged when user count exceeds 5 or per-user audit becomes a need.
- **Tag autocomplete restoration** — separate stopgap deferred item, not blocked by questionnaires.
- **Tag persistence source** — once we resume, evaluate parsing the Tags column from the Confluence notes table as a way to populate tag autocomplete. Solves the deferred TODO without needing SharePoint.
- **Questionnaire deletion of used items** — current design blocks; if Sebrina pushes back, revisit.
- **Reorder via drag-and-drop** — possible v2 polish if up/down buttons feel clunky.

---

## Branch where this work will happen
When we resume: create branch `questionnaires` off main in GitHub Desktop. Set up a second Streamlit Cloud preview app pointing at that branch so Sebrina can test before merge.
