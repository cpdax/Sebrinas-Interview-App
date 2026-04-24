# Customer Interview Notes App

A mobile-friendly Streamlit app for capturing customer conversation notes. Saves to SharePoint — routing determined by a destination field in the form.

---

## What it does

- Simple form: contact name, org, role, destination, event source, notes
- In-browser audio recording with Web Speech API transcription — tap to record, copy to notes
- Saves each entry as a row in a SharePoint list, routing by destination
- Falls back to a CSV download if SharePoint isn't configured (useful for demos/prototyping)

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
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app → select repo + `app.py`
3. In **Advanced settings → Secrets**, paste the contents of your filled-in `secrets.toml`
4. Deploy

---

## Configuration — secrets.toml

All configuration lives in Streamlit secrets. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill in:

```toml
# Azure AD app registration (for SharePoint write access)
TENANT_ID = "your-azure-tenant-id"
CLIENT_ID = "your-azure-app-client-id"
CLIENT_SECRET = "your-azure-app-client-secret"

# SharePoint routing
SHAREPOINT_HOSTNAME = "yourcompany.sharepoint.com"
PROCARE_SITE_PATH = "/sites/your-procare-site"
CHILDPLUS_SITE_PATH = "/sites/your-childplus-site"
LIST_NAME = "your-list-name"
```

The app auto-creates the SharePoint list on first save — no manual list setup needed.

---

## SharePoint setup — Azure AD app registration

1. Go to [portal.azure.com](https://portal.azure.com) → **Azure Active Directory → App registrations → New registration**
2. Name it anything, single tenant, no redirect URI → **Register**
3. **API permissions → Add → Microsoft Graph → Application permissions → `Sites.ReadWrite.All`**
4. Click **Grant admin consent** (requires Global Admin or SharePoint Admin)
5. **Certificates & secrets → New client secret** — copy the value immediately

---

## Audio recording

- Uses the browser's built-in Web Speech API — no API key needed
- Works on iPhone Safari (prompts for microphone permission on first use)
- Transcription appears in the recording component; tap "Copy to notes" to paste it into the notes field
- For noisy environments (conference floors, booths): hold phone close to speaker, speak clearly

---

## Repo structure

```
app.py                        Main Streamlit app
requirements.txt              Python dependencies
README.md
.streamlit/
  secrets.toml.example        Template — copy to secrets.toml and fill in
```
