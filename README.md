# Customer Interview Notes App

A mobile + desktop Streamlit app for capturing customer conversation notes. Routes data by product line (Procare / ChildPlus) to separate SharePoint lists. Supports solo and group conversations, tagging with similarity-check, and in-browser audio transcription.

---

## What it does

- **Two modes** — Solo (one person) or Group (multiple attendees)
- **HubSpot lookup** — Solo searches by name/agency; Group searches all contacts at an agency and lets you check who's present
- **Manual contact entry** — add people not in HubSpot by hand
- **Multi-note capture** — add as many notes as you want per session; each saves as its own row
- **Audio transcription** — tap the mic, speak, copy to a note (Web Speech API; works in Chrome/Edge/Safari)
- **Smart tagging** — pick from existing tags or add new ones; the app checks for similar tags and offers to use them instead
- **Separate SharePoint lists per product line** — Procare data and ChildPlus data are fully segregated
- **Read-only HubSpot context panel** at the bottom showing contact info + recent support tickets

---

## Running locally

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# Fill in your values in secrets.toml
streamlit run app.py
```

---

## Deploying to Streamlit Community Cloud

1. Push this repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app
3. Set **Main file path** to `app.py`
4. In **Advanced settings → Secrets**, paste the contents of your filled-in `secrets.toml`
5. Deploy

---

## Data model

### Notes lists (one per destination)

Each note gets its own row. Notes from the same session share a SessionID.

| Column | Type | Description |
|--------|------|-------------|
| SessionID | Text | UUID shared by all notes in one session |
| NoteIndex | Number | Position of this note in the session (1, 2, 3…) |
| NoteCount | Number | Total notes in the session |
| SessionType | Choice | Solo \| Group |
| Contacts | Text | Full attendee list, formatted |
| PrimaryContact | Text | First contact's name (for easy sort) |
| PrimaryAgency | Text | First contact's agency (for easy sort) |
| Destination | Choice | Procare \| ChildPlus |
| EventSource | Choice | Conference \| Site Visit \| Zoom \| Other |
| Tags | Text | Comma-separated tags |
| NoteText | Text (multi-line) | The note itself |
| NoteTimestamp | DateTime | When the note was captured |
| SubmittedAt | DateTime | When the session was saved |

### Tags lists (one per destination)

Source of truth for tag names. Supports fuzzy similarity check ("did you mean…?") when adding new tags.

| Column | Type | Description |
|--------|------|-------------|
| TagName | Text | The tag string |
| FirstUsed | DateTime | When the tag was first created |
| UseCount | Number | Reserved for future tag-usage tracking |

All four lists auto-create on first save — no manual SharePoint setup needed.

---

## Configuration — secrets.toml

```toml
# Azure AD
TENANT_ID = "..."
CLIENT_ID = "..."
CLIENT_SECRET = "..."

# SharePoint
SHAREPOINT_HOSTNAME = "yourcompany.sharepoint.com"
PROCARE_SITE_PATH = "/sites/your-procare-site"
CHILDPLUS_SITE_PATH = "/sites/your-childplus-site"

# SharePoint list names
PROCARE_NOTES_LIST = "Customer Interview Notes - Procare"
CHILDPLUS_NOTES_LIST = "Customer Interview Notes - ChildPlus"
PROCARE_TAGS_LIST = "Interview Tags - Procare"
CHILDPLUS_TAGS_LIST = "Interview Tags - ChildPlus"

# HubSpot (optional)
HUBSPOT_TOKEN = "pat-na1-..."
```

---

## Azure AD setup

1. [portal.azure.com](https://portal.azure.com) → **Azure Active Directory → App registrations → New registration**
2. Single tenant, no redirect URI → **Register**
3. **API permissions → Add → Microsoft Graph → Application permissions → `Sites.ReadWrite.All`**
4. **Grant admin consent** (requires Global Admin or SharePoint Admin)
5. **Certificates & secrets → New client secret** → copy the value immediately

---

## Audio recording

- Web Speech API — browser-native, no API key
- Works: Chrome/Edge on Windows, Safari/Chrome on Mac, Safari on iPhone
- Does not work reliably in Firefox
- Mic captures the device's microphone only — **not Zoom call audio**. For Zoom conversations, use Zoom's own transcription and paste quotes in
- For noisy environments: hold device close, speak clearly

---

## Repo structure

```
app.py                          Main Streamlit app
requirements.txt                Python dependencies
README.md
.streamlit/
  secrets.toml.example          Template — copy to secrets.toml and fill in
```
