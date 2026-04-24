# Sebrina's Interview App — Requirements & Backlog

_Last updated: April 24, 2026_

---

## Background

Sebrina Carroll (Director of Content and Learning, Procare/ChildPlus) needs a way to capture notes from customer conversations at conferences, in-person site visits, and Zoom calls. The goal is a persistent repository she can pull quotes and trends from over time. She is not running formal surveys — she is the one entering all data.

Origin: [Meeting transcript — April 17, 2026](../../../Meetings/Zoom/2026-04-17%2011.19.16%20Dax%20and%20Sebrina%20Customer%20Interview%20Form%20Discussion/meeting_saved_closed_caption.txt)

Aha! item: [PRODOPS-22](https://procaresoftware.aha.io/features/PRODOPS-22)

---

## MVP (v1) — Final Spec

### Platform
- Streamlit web app, deployed to Streamlit Community Cloud
- New GitHub repo (separate from `cpdax/release-note-generator`)
- Accessible via phone browser (iPhone Safari primary target)
- No auth for v1 — unlisted URL shared directly with Sebrina

### Form Fields
| Field | Type | Notes |
|-------|------|-------|
| Contact name | Text | Required |
| Organization / agency | Text | Required |
| Title / role | Text | |
| Destination | Dropdown | Procare · ChildPlus — routes save to correct SharePoint list |
| Event / source | Dropdown | Conference · Site Visit · Zoom · Other |
| Notes | Text area | Free-form |
| Audio | Record button | Web Speech API → transcription appended to Notes field automatically; user can edit before saving |

### Backend / Save Target
- **SharePoint Lists** — one list per destination (Procare, ChildPlus)
- Each submission appends a row to the appropriate list
- Lists are filterable/sortable in SharePoint and Excel — no extra tooling needed for Sebrina to analyze trends
- Routing: Destination field determines which SharePoint list receives the record
- **SharePoint write requires an Azure AD app registration** — see setup steps in README.md. The app falls back to a CSV download if credentials are not yet configured (supports demo/prototype use before IT setup is complete).

### SharePoint Destinations
| Destination | Site | List (auto-created on first save) |
|-------------|------|----------------------------------|
| Procare | `procaresoftwarellc.sharepoint.com/sites/ProductManagement` | Customer Interview Notes - Sebrina |
| ChildPlus | `procaresoftwarellc.sharepoint.com/sites/ProductManagement-childplus` | Customer Interview Notes - Sebrina |

### Audio — Web Speech API
- Browser-native, no API key required, works on iPhone Safari
- Transcription result is displayed in the recording component; user copies to Notes field
- Sebrina can edit the transcription text before saving
- Whisper (better accuracy, noisy environments) deferred to v2

### GQ — Deferred to v2+
- GQ API (v1) does not support the required workflow: email required for all records, custom field writes don't persist, no notes endpoint
- GQ becomes a future destination: when Sebrina wants to formally recruit a known contact into a study, she can push from SharePoint → GQ manually or via automation
- GQ study "Customer Conversations — Sebrina Carroll" to be created when v2 integration is built

### Auth
- None for v1 — unlisted URL
- Add Streamlit-native auth or login when/if app is shared beyond Sebrina

---

## Deployment Prerequisites (before going live)

1. **GitHub repo** — new private repo under `cpdax/` (separate from release-note-generator). See README.md for file list.
2. **Streamlit Community Cloud** — connect repo, set main file to `app.py`, add secrets.
3. **Azure AD app registration** — `Sites.ReadWrite.All` permission, admin consent granted. See README.md for step-by-step. Without this, app works but saves to CSV download only.

---

## Future Versions / Backlog

### Near-term (v2 candidates)
- [ ] **Zoom integration** — auto-ingest Zoom meeting transcripts into SharePoint list; match participant by name if possible, otherwise queue for manual review. Also investigate GQ's native Zoom integration.
- [ ] **GQ integration** — push entries with known emails from SharePoint into GQ as candidate records. Requires resolving field-write limitations discovered in v1 API exploration.
- [ ] **Auth / login** — Streamlit-native auth or simple password gate if app is shared beyond Sebrina. Full multi-user login if it expands team-wide.
- [ ] **Whisper transcription** — replace Web Speech API with OpenAI Whisper for better accuracy in noisy environments (conference floors, booths). Requires OpenAI API key.

### Medium-term (v3 candidates)
- [ ] **Custom question builder** — let Sebrina define her own interview questions per study or session type; answers stored as structured fields, not just free-form notes.
- [ ] **Tagging & sorting** — tag interviews by topic, person, agency, program type. Filter and search across the repository.
- [ ] **Trend / quote surfacing** — AI-assisted summary: "What are customers saying about professional development?" pulls relevant quotes and patterns from SharePoint data.

### Longer-term / Investigate
- [ ] **Badge scanning** — use iPhone camera to scan Cvent QR codes at conferences; auto-populate name and agency fields. Requires Cvent API access or QR code decode + field mapping. Note: Cvent lead scanning costs extra per event — evaluate cost vs. build.
- [ ] **SharePoint page integration** — push selected quotes to Sebrina's existing SharePoint page so she can surface them in meetings without manually copying.
- [ ] **Multi-user / team version** — if other PMs or researchers want to capture interviews in the same repository, add user attribution per record.

---

## Open Questions / Decisions Log

| Date | Question | Decision |
|------|----------|----------|
| 2026-04-24 | Where does data live? | SharePoint Lists (one per destination: Procare, ChildPlus). GQ deferred to v2. |
| 2026-04-24 | Why not GQ for v1? | API requires email for all records; custom field writes don't persist; no notes endpoint. |
| 2026-04-24 | Form platform? | Streamlit + GitHub (new repo, separate from release-note-generator) |
| 2026-04-24 | Audio approach for MVP? | Web Speech API → transcription to text. Whisper deferred to v2. |
| 2026-04-24 | Auth for v1? | None — unlisted URL shared with Sebrina directly |
| 2026-04-24 | GQ study name? | "Customer Conversations — Sebrina Carroll" — create when v2 integration is built |
| 2026-04-24 | Destination routing? | Destination dropdown (Procare / ChildPlus) routes save to correct SharePoint list |
| 2026-04-24 | Hard deadline? | No hard deadline; prototype goal = week of April 28 for Sebrina review |

---

## Technical Notes
- Sebrina has Claude access (company license) — keep in mind for future AI-assisted features
- Sebrina's existing SharePoint page (built by Dax) is a related asset
- Conferences primarily use Cvent for badge/lead management
- Great Question API token lives in `_Config/api_tokens.json`
- SharePoint writes use Microsoft Graph API with client credentials flow (Azure AD app registration required)
- Web Speech API: `webkitSpeechRecognition` on Safari, `SpeechRecognition` on others — both prefixes handled in the audio component
- App auto-creates SharePoint lists on first successful save — no manual list setup needed
