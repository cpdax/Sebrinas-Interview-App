# Sebrina's Interview App

A mobile-friendly Streamlit app for capturing customer conversation notes. Saves to SharePoint — one list per product line (Procare / ChildPlus).

Built by Dax Collins, Procare Product Operations. Related Aha! item: [PRODOPS-22](https://procaresoftware.aha.io/features/PRODOPS-22)

---

## What it does

- Simple form: contact name, org, role, destination (Procare or ChildPlus), event source, notes
- In-browser audio recording with Web Speech API transcription — tap to record, copy to notes
- Saves each entry as a row in a SharePoint list, routing by destination
- Falls back to a CSV download if SharePoint isn't configured (useful for demos)

---

## Running locally

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# Fill in your Azure AD credentials in secrets.toml
streamlit run app.py
```

---

## Deploying to Streamlit Community Cloud

1. Push this repo to GitHub (private repo is fine)
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app → select repo + `app.py`
3. In **Advanced settings → Secrets**, paste the contents of your filled-in `secrets.toml`
4. Deploy

---

## SharePoint setup — what you need

The app auto-creates the SharePoint lists on first save. You just need an Azure AD app registration with the right permissions.

### Step 1 — Register an Azure AD app

1. Go to [portal.azure.com](https://portal.azure.com) → **Azure Active Directory → App registrations → New registration**
2. Name: `Sebrina Interview App` (or anything)
3. Supported account types: **Single tenant**
4. No redirect URI needed
5. Click **Register**

### Step 2 — Add API permissions

In the new app registration:
1. **API permissions → Add a permission → Microsoft Graph → Application permissions**
2. Add: `Sites.ReadWrite.All`
3. Click **Grant admin consent** (requires Global Admin or SharePoint Admin)

### Step 3 — Create a client secret

1. **Certificates & secrets → New client secret**
2. Copy the **Value** immediately (you can't see it again)

### Step 4 — Fill in secrets.toml

```toml
TENANT_ID = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"   # Azure AD → Overview → Tenant ID
CLIENT_ID = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"   # App registration → Overview → Application (client) ID
CLIENT_SECRET = "your-client-secret-value"
```

### What the app creates on first save

| Destination | SharePoint Site | List Name |
|-------------|----------------|-----------|
| Procare | `procaresoftwarellc.sharepoint.com/sites/ProductManagement` | Customer Interview Notes - Sebrina |
| ChildPlus | `procaresoftwarellc.sharepoint.com/sites/ProductManagement-childplus` | Customer Interview Notes - Sebrina |

List columns: Contact Name, Organization, Role, Destination, Event Source, Notes, Submitted At

---

## Audio recording notes

- Uses the browser's built-in Web Speech API — no API key or external service needed
- Works on iPhone Safari (requires microphone permission on first use)
- Transcription is editable before saving
- **In noisy environments**: speak clearly and close to the phone mic. Upgrade to Whisper API (v2) for better accuracy.

---

## Roadmap

See `requirements.md` for full backlog. Near-term v2 items:
- Zoom transcript auto-import
- Auth / login for multi-user
- Whisper transcription for noisy environments

---

## Repo structure

```
app.py                    Main Streamlit app
requirements.txt          Python dependencies
README.md
.streamlit/
  secrets.toml.example    Template — copy to secrets.toml and fill in
requirements.md           Full requirements & backlog (not deployed)
```
