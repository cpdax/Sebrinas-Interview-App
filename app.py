import streamlit as st
import streamlit.components.v1 as components
import requests
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

DESTINATION_OPTIONS = ["Procare", "ChildPlus"]
SOURCE_OPTIONS      = ["Conference", "Site Visit", "Zoom", "Other"]
HS_BASE             = "https://api.hubapi.com"
TICKET_DAYS         = 90
TICKET_MAX          = 5

# ─────────────────────────────────────────────
# HUBSPOT HELPERS
# ─────────────────────────────────────────────

def get_hubspot_token() -> str | None:
    try:
        return st.secrets["HUBSPOT_TOKEN"]
    except (KeyError, FileNotFoundError):
        return None


def search_hubspot_contacts(name: str, agency: str, token: str) -> list:
    """Search contacts by name and/or company. All provided terms are ANDed."""
    filters = []
    name   = name.strip()
    agency = agency.strip()

    if name:
        parts = name.split(" ", 1)
        filters.append({"propertyName": "firstname", "operator": "CONTAINS_TOKEN", "value": parts[0]})
        if len(parts) > 1:
            filters.append({"propertyName": "lastname", "operator": "CONTAINS_TOKEN", "value": parts[1]})

    if agency:
        filters.append({"propertyName": "company", "operator": "CONTAINS_TOKEN", "value": agency})

    if not filters:
        return []

    resp = requests.post(
        f"{HS_BASE}/crm/v3/objects/contacts/search",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "filterGroups": [{"filters": filters}],
            "properties": ["firstname", "lastname", "email", "company", "phone", "jobtitle"],
            "limit": 10,
        },
        timeout=10,
    )
    return resp.json().get("results", []) if resp.ok else []


def get_contact_tickets(contact_id: str, token: str) -> list:
    """Return up to TICKET_MAX tickets from the last TICKET_DAYS days for a contact."""
    headers = {"Authorization": f"Bearer {token}"}

    assoc = requests.get(
        f"{HS_BASE}/crm/v3/objects/contacts/{contact_id}/associations/tickets",
        headers=headers, timeout=10,
    )
    if not assoc.ok:
        return []

    ticket_ids = [r["id"] for r in assoc.json().get("results", [])]
    if not ticket_ids:
        return []

    batch = requests.post(
        f"{HS_BASE}/crm/v3/objects/tickets/batch/read",
        headers={**headers, "Content-Type": "application/json"},
        json={
            "inputs": [{"id": tid} for tid in ticket_ids],
            "properties": ["subject", "createdate", "hs_ticket_priority", "content"],
        },
        timeout=15,
    )
    if not batch.ok:
        return []

    cutoff  = datetime.now(timezone.utc) - timedelta(days=TICKET_DAYS)
    tickets = []
    for t in batch.json().get("results", []):
        created = datetime.fromisoformat(t["properties"]["createdate"].replace("Z", "+00:00"))
        if created >= cutoff:
            tickets.append(t)

    tickets.sort(key=lambda t: t["properties"]["createdate"], reverse=True)
    return tickets[:TICKET_MAX]


# ─────────────────────────────────────────────
# SHAREPOINT / GRAPH API HELPERS
# ─────────────────────────────────────────────

def get_sharepoint_config() -> dict | None:
    try:
        return {
            "tenant_id":           st.secrets["TENANT_ID"],
            "client_id":           st.secrets["CLIENT_ID"],
            "client_secret":       st.secrets["CLIENT_SECRET"],
            "hostname":            st.secrets["SHAREPOINT_HOSTNAME"],
            "procare_site_path":   st.secrets["PROCARE_SITE_PATH"],
            "childplus_site_path": st.secrets["CHILDPLUS_SITE_PATH"],
            "list_name":           st.secrets["LIST_NAME"],
        }
    except (KeyError, FileNotFoundError):
        return None


def get_graph_token(cfg: dict) -> str | None:
    resp = requests.post(
        f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "scope":         "https://graph.microsoft.com/.default",
        },
        timeout=10,
    )
    return resp.json().get("access_token") if resp.ok else None


@st.cache_data(ttl=3600, show_spinner=False)
def get_site_id(hostname: str, site_path: str, token: str) -> str | None:
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{hostname}:{site_path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    return resp.json().get("id") if resp.ok else None


@st.cache_data(ttl=3600, show_spinner=False)
def get_or_create_list(site_id: str, list_name: str, token: str) -> str | None:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists",
        headers=headers, timeout=10,
    )
    if resp.ok:
        for lst in resp.json().get("value", []):
            if lst.get("displayName") == list_name:
                return lst["id"]

    create = requests.post(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists",
        headers=headers,
        json={
            "displayName": list_name,
            "columns": [
                {"name": "ContactName",  "text": {}},
                {"name": "Organization", "text": {}},
                {"name": "Role",         "text": {}},
                {"name": "Destination",  "choice": {"choices": DESTINATION_OPTIONS}},
                {"name": "EventSource",  "choice": {"choices": SOURCE_OPTIONS}},
                {"name": "Notes",        "text": {"allowMultipleLines": True}},
                {"name": "SubmittedAt",  "dateTime": {}},
            ],
            "list": {"template": "genericList"},
        },
        timeout=15,
    )
    return create.json().get("id") if create.ok else None


def save_to_sharepoint(form_data: dict) -> tuple[bool, str]:
    cfg = get_sharepoint_config()
    if not cfg:
        return False, "sharepoint_not_configured"

    token = get_graph_token(cfg)
    if not token:
        return False, "Could not acquire Graph token — check Azure AD credentials"

    site_path = cfg["procare_site_path"] if form_data["destination"] == "Procare" else cfg["childplus_site_path"]
    site_id   = get_site_id(cfg["hostname"], site_path, token)
    if not site_id:
        return False, "Could not resolve SharePoint site"

    list_id = get_or_create_list(site_id, cfg["list_name"], token)
    if not list_id:
        return False, "Could not get or create SharePoint list"

    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists/{list_id}/items",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"fields": {
            "Title":        form_data["contact_name"] or "(no name)",
            "ContactName":  form_data["contact_name"],
            "Organization": form_data["organization"],
            "Role":         form_data["role"],
            "Destination":  form_data["destination"],
            "EventSource":  form_data["event_source"],
            "Notes":        form_data["notes"],
            "SubmittedAt":  form_data["submitted_at"],
        }},
        timeout=15,
    )
    return (True, "Saved") if resp.ok else (False, f"Graph API error {resp.status_code}: {resp.text[:200]}")


# ─────────────────────────────────────────────
# AUDIO COMPONENT
# ─────────────────────────────────────────────

AUDIO_HTML = """
<style>
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
  #recorder {
    display:flex; flex-direction:column; align-items:center;
    gap:12px; padding:16px;
    background:#f8f9fa; border-radius:12px; border:1px solid #dee2e6;
  }
  #recordBtn {
    width:64px; height:64px; border-radius:50%; border:none; cursor:pointer;
    font-size:28px; background:#dc3545; color:white;
    box-shadow:0 4px 12px rgba(220,53,69,0.3); transition:all 0.2s;
  }
  #recordBtn.listening { background:#198754; animation:pulse 1s infinite; }
  #recordBtn:disabled  { background:#adb5bd; cursor:default; }
  @keyframes pulse { 0%,100%{transform:scale(1)} 50%{transform:scale(1.1)} }
  #status { font-size:13px; color:#6c757d; }
  #transcriptBox {
    width:100%; box-sizing:border-box; padding:10px; border-radius:8px;
    border:1px solid #ced4da; font-size:14px; min-height:60px; resize:vertical; display:none;
  }
  #copyBtn {
    padding:6px 16px; background:#0d6efd; color:white;
    border:none; border-radius:6px; cursor:pointer; font-size:13px; display:none;
  }
  #copyBtn:hover { background:#0b5ed7; }
  #copied { font-size:12px; color:#198754; display:none; }
</style>
<div id="recorder">
  <button id="recordBtn" title="Tap to record">🎙</button>
  <div id="status">Tap to start recording</div>
  <textarea id="transcriptBox" placeholder="Transcription will appear here..."></textarea>
  <div style="display:flex;gap:10px;align-items:center;">
    <button id="copyBtn" onclick="copyText()">Copy to notes ↓</button>
    <span id="copied">Copied!</span>
  </div>
</div>
<script>
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn=document.getElementById('recordBtn'), status=document.getElementById('status');
  const tbox=document.getElementById('transcriptBox'), copyBtn=document.getElementById('copyBtn');
  const copied=document.getElementById('copied');
  if (!SpeechRec) {
    status.textContent='⚠️ Voice recording not supported in this browser.'; btn.disabled=true;
  } else {
    const rec=new SpeechRec(); rec.continuous=true; rec.interimResults=true; rec.lang='en-US';
    let running=false, finalText='';
    btn.addEventListener('click',()=>{
      if (!running) {
        finalText=''; tbox.value=''; rec.start(); running=true;
        btn.textContent='⏹'; btn.classList.add('listening'); status.textContent='Recording… tap to stop';
      } else { rec.stop(); }
    });
    rec.onresult=(e)=>{
      let interim='';
      for(let i=e.resultIndex;i<e.results.length;i++){
        const t=e.results[i][0].transcript;
        if(e.results[i].isFinal){finalText+=t+' ';}else{interim+=t;}
      }
      tbox.value=finalText+interim;
    };
    rec.onend=()=>{
      running=false; btn.textContent='🎙'; btn.classList.remove('listening');
      if(finalText.trim()){
        status.textContent='✅ Done — copy text and paste into notes below';
        tbox.style.display='block'; copyBtn.style.display='inline-block';
      } else { status.textContent='No speech detected. Tap to try again.'; }
    };
    rec.onerror=(e)=>{
      status.textContent='Error: '+e.error+'. Tap to retry.';
      running=false; btn.textContent='🎙'; btn.classList.remove('listening');
    };
  }
  function copyText(){
    navigator.clipboard.writeText(tbox.value).then(()=>{
      copied.style.display='inline'; setTimeout(()=>{copied.style.display='none';},2000);
    });
  }
</script>
"""


# ─────────────────────────────────────────────
# PAGE SETUP
# ─────────────────────────────────────────────

st.set_page_config(page_title="Customer Notes", page_icon="🎤", layout="centered")

st.markdown("""
<style>
  .main > div { max-width:640px; margin:auto; }
  label { font-weight:600; }
  .stTextArea textarea { font-size:15px; }
  .stButton > button { width:100%; padding:14px; font-size:16px; border-radius:10px; font-weight:600; }
  .hs-panel { background:#f0f4ff; border:1px solid #c7d7ff; border-radius:10px; padding:16px; margin-top:8px; }
  .ticket-meta { font-size:12px; color:#6c757d; }
  .success-banner { background:#d1e7dd; color:#0f5132; padding:16px; border-radius:10px; text-align:center; font-weight:600; }
  .fallback-banner { background:#fff3cd; color:#664d03; padding:12px; border-radius:8px; font-size:13px; }
</style>
""", unsafe_allow_html=True)

st.title("🎤 Customer Notes")
st.caption("Capture a conversation — it takes 30 seconds.")

# ─────────────────────────────────────────────
# SESSION STATE INIT
# ─────────────────────────────────────────────

_state_defaults = {
    "field_name":        "",
    "field_agency":      "",
    "field_role":        "",
    "field_notes":       "",
    "hs_results":        None,   # None = not searched yet; [] = searched, no results
    "hs_selected_id":    None,
    "hs_selected_data":  None,
    "hs_tickets":        None,
    "submitted":         False,
    "last_entry":        None,
}
for k, v in _state_defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ─────────────────────────────────────────────
# SUCCESS STATE
# ─────────────────────────────────────────────

if st.session_state.submitted:
    st.markdown('<div class="success-banner">✅ Saved! Ready for the next one.</div>', unsafe_allow_html=True)
    st.write("")

    if st.session_state.last_entry and st.session_state.last_entry.get("fallback_csv"):
        st.markdown('<div class="fallback-banner">⚠️ SharePoint not connected yet — download below to save locally.</div>', unsafe_allow_html=True)
        e = st.session_state.last_entry
        csv_row    = f'"{e["contact_name"]}","{e["organization"]}","{e["role"]}","{e["destination"]}","{e["event_source"]}","{e["notes"].replace(chr(34),chr(39))}","{e["submitted_at"]}"\n'
        csv_header = '"Contact Name","Organization","Role","Destination","Event Source","Notes","Submitted At"\n'
        st.download_button("⬇️ Download as CSV", data=csv_header + csv_row,
                           file_name=f"interview_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", mime="text/csv")

    if st.button("Add another"):
        for k, v in _state_defaults.items():
            st.session_state[k] = v
        st.rerun()

    st.stop()


# ─────────────────────────────────────────────
# SECTION 1 — CONTACT INFO
# ─────────────────────────────────────────────

st.subheader("Who did you talk to?")

name   = st.text_input("Name *",                 key="field_name",   placeholder="First and last name")
agency = st.text_input("Organization / Agency *", key="field_agency", placeholder="e.g. Bright Horizons, Rockford Head Start")
role   = st.text_input("Title / Role",            key="field_role",   placeholder="e.g. Executive Director, Program Coordinator")


# ─────────────────────────────────────────────
# SECTION 2 — HUBSPOT SEARCH (optional)
# ─────────────────────────────────────────────

st.divider()

hs_token        = get_hubspot_token()
has_hs          = hs_token is not None
search_enabled  = has_hs and bool(name.strip() or agency.strip())

col1, col2 = st.columns([4, 1])
with col1:
    st.markdown("**🔍 HubSpot Lookup** *(optional)*")
    if not has_hs:
        st.caption("HubSpot not configured — add HUBSPOT_TOKEN to secrets to enable.")
    else:
        st.caption("Search for an existing contact to pre-fill fields and load support history.")
with col2:
    search_clicked = st.button("Search", disabled=not search_enabled, use_container_width=True)

if search_clicked and hs_token:
    with st.spinner("Searching HubSpot…"):
        results = search_hubspot_contacts(name, agency, hs_token)
    st.session_state.hs_results      = results
    st.session_state.hs_selected_id  = None
    st.session_state.hs_selected_data = None
    st.session_state.hs_tickets      = None

# ── Results picker ──
if st.session_state.hs_results is not None:
    if len(st.session_state.hs_results) == 0:
        st.info("No match identified")
    else:
        options = []
        for c in st.session_state.hs_results:
            p = c["properties"]
            label = f"{p.get('firstname', '')} {p.get('lastname', '')}".strip() or "(no name)"
            if p.get("company"):  label += f"  ·  {p['company']}"
            if p.get("jobtitle"): label += f"  ·  {p['jobtitle']}"
            options.append(label)
        options.append("None of these")

        choice = st.radio("Select the right person:", options, key="hs_radio")

        if choice and choice != "None of these":
            chosen_idx     = options.index(choice)
            chosen_contact = st.session_state.hs_results[chosen_idx]

            # Auto-fill fields and fetch tickets when selection changes
            if st.session_state.hs_selected_id != chosen_contact["id"]:
                p = chosen_contact["properties"]
                if p.get("company"):  st.session_state["field_agency"] = p["company"]
                if p.get("jobtitle"): st.session_state["field_role"]   = p["jobtitle"]
                st.session_state.hs_selected_id   = chosen_contact["id"]
                st.session_state.hs_selected_data = chosen_contact

                if hs_token:
                    with st.spinner("Loading support history…"):
                        st.session_state.hs_tickets = get_contact_tickets(chosen_contact["id"], hs_token)

                st.rerun()  # Reflect auto-filled field values above

        elif choice == "None of these":
            st.session_state.hs_selected_id   = None
            st.session_state.hs_selected_data = None
            st.session_state.hs_tickets        = None


# ─────────────────────────────────────────────
# SECTION 3 — CONTEXT
# ─────────────────────────────────────────────

st.divider()
st.subheader("Context")

destination  = st.selectbox("Destination *", DESTINATION_OPTIONS, key="field_destination")
event_source = st.selectbox("Where did you meet?", SOURCE_OPTIONS, key="field_source")


# ─────────────────────────────────────────────
# SECTION 4 — NOTES
# ─────────────────────────────────────────────

st.divider()
st.subheader("Notes")
st.markdown("**Record a quote or key point**")
st.caption("Tap the mic, speak, then copy the transcription into the notes below.")
components.html(AUDIO_HTML, height=220)

notes = st.text_area(
    "Notes", key="field_notes",
    placeholder="Paste transcription here, or type directly.\n\nWhat challenges did they mention? Any good quotes? What stood out?",
    height=180, label_visibility="collapsed",
)


# ─────────────────────────────────────────────
# SAVE
# ─────────────────────────────────────────────

st.divider()
save_clicked = st.button("💾  Save", use_container_width=True)

if save_clicked:
    if not name.strip() or not agency.strip():
        st.error("Name and Organization are required.")
    else:
        form_data = {
            "contact_name": name.strip(),
            "organization": agency.strip(),
            "role":         role.strip(),
            "destination":  destination,
            "event_source": event_source,
            "notes":        notes.strip(),
            "submitted_at": datetime.now().isoformat(),
        }
        with st.spinner("Saving…"):
            success, message = save_to_sharepoint(form_data)

        if success:
            st.session_state.last_entry = form_data
            st.session_state.submitted  = True
            st.rerun()
        elif message == "sharepoint_not_configured":
            form_data["fallback_csv"]   = True
            st.session_state.last_entry = form_data
            st.session_state.submitted  = True
            st.rerun()
        else:
            st.error(f"Save failed: {message}")


# ─────────────────────────────────────────────
# SECTION 5 — HUBSPOT CONTEXT (read-only, bottom)
# ─────────────────────────────────────────────

contact = st.session_state.get("hs_selected_data")
if contact:
    p = contact["properties"]
    st.divider()
    st.subheader("📋 HubSpot Context")
    st.caption("Read-only — for reference only. Not saved with your notes.")

    st.markdown('<div class="hs-panel">', unsafe_allow_html=True)

    col1, col2 = st.columns(2)
    with col1:
        full_name = f"{p.get('firstname','').strip()} {p.get('lastname','').strip()}".strip()
        st.markdown(f"**{full_name or '(no name)'}**")
        if p.get("email"): st.markdown(f"✉️ {p['email']}")
        if p.get("phone"): st.markdown(f"📞 {p['phone']}")
    with col2:
        if p.get("company"):  st.markdown(f"🏢 {p['company']}")
        if p.get("jobtitle"): st.markdown(f"💼 {p['jobtitle']}")
        st.markdown(f"[Open in HubSpot ↗]({contact.get('url', '#')})")

    st.markdown("</div>", unsafe_allow_html=True)

    # Tickets
    tickets = st.session_state.get("hs_tickets") or []
    st.markdown(f"**Recent Support Tickets** — last {TICKET_DAYS} days")

    if not tickets:
        st.caption("No support tickets in this period.")
    else:
        for t in tickets:
            tp       = t["properties"]
            raw_subj = tp.get("subject", "") or ""
            subject  = raw_subj.split(" - ", 1)[1] if " - " in raw_subj else raw_subj
            date_str = datetime.fromisoformat(
                tp["createdate"].replace("Z", "+00:00")
            ).strftime("%b %d, %Y")
            priority = tp.get("hs_ticket_priority", "") or ""
            content  = tp.get("content", "") or ""
            snippet  = (content[:200] + "…") if len(content) > 200 else content
            p_emoji  = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(priority.upper(), "⚪")

            with st.expander(f"{p_emoji} {subject or '(no subject)'} — {date_str}"):
                if priority:
                    st.markdown(f'<span class="ticket-meta">Priority: {priority.title()}</span>', unsafe_allow_html=True)
                if snippet:
                    st.markdown(snippet)
                st.markdown(f"[View in HubSpot ↗]({t.get('url', '#')})")
