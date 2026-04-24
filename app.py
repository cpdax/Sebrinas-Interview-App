import streamlit as st
import streamlit.components.v1 as components
import requests
import uuid
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

DESTINATION_OPTIONS = ["Procare", "ChildPlus"]
SOURCE_OPTIONS      = ["Conference", "Site Visit", "Zoom", "Other"]
HS_BASE             = "https://api.hubapi.com"
TICKET_DAYS         = 90
TICKET_MAX          = 5
TAG_SIMILARITY      = 0.75  # 75% similarity threshold for "did you mean?"

NOTES_LIST_NAME = {"Procare": "PROCARE_NOTES_LIST", "ChildPlus": "CHILDPLUS_NOTES_LIST"}
TAGS_LIST_NAME  = {"Procare": "PROCARE_TAGS_LIST",  "ChildPlus": "CHILDPLUS_TAGS_LIST"}

# ─────────────────────────────────────────────
# HUBSPOT HELPERS
# ─────────────────────────────────────────────

def get_hubspot_token() -> str | None:
    try:
        return st.secrets["HUBSPOT_TOKEN"]
    except (KeyError, FileNotFoundError):
        return None


def _search_contacts(filters: list, token: str) -> list:
    if not filters:
        return []
    resp = requests.post(
        f"{HS_BASE}/crm/v3/objects/contacts/search",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "filterGroups": [{"filters": filters}],
            "properties": ["firstname", "lastname", "email", "company", "phone", "jobtitle"],
            "limit": 25,
        },
        timeout=10,
    )
    return resp.json().get("results", []) if resp.ok else []


def search_hubspot_contacts(name: str, agency: str, token: str) -> list:
    """Search by name, agency, or both. All provided terms are ANDed."""
    filters = []
    name, agency = name.strip(), agency.strip()
    if name:
        parts = name.split(" ", 1)
        filters.append({"propertyName": "firstname", "operator": "CONTAINS_TOKEN", "value": parts[0]})
        if len(parts) > 1:
            filters.append({"propertyName": "lastname", "operator": "CONTAINS_TOKEN", "value": parts[1]})
    if agency:
        filters.append({"propertyName": "company", "operator": "CONTAINS_TOKEN", "value": agency})
    return _search_contacts(filters, token)


def search_contacts_by_agency(agency: str, token: str) -> list:
    """Group mode — return all contacts at an agency."""
    if not agency.strip():
        return []
    return _search_contacts(
        [{"propertyName": "company", "operator": "CONTAINS_TOKEN", "value": agency.strip()}],
        token,
    )


def get_contact_tickets(contact_id: str, token: str) -> list:
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
        try:
            created = datetime.fromisoformat(t["properties"]["createdate"].replace("Z", "+00:00"))
            if created >= cutoff:
                tickets.append(t)
        except (KeyError, ValueError):
            continue
    tickets.sort(key=lambda t: t["properties"]["createdate"], reverse=True)
    return tickets[:TICKET_MAX]


# ─────────────────────────────────────────────
# SHAREPOINT / GRAPH HELPERS
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
            "procare_notes_list":   st.secrets["PROCARE_NOTES_LIST"],
            "childplus_notes_list": st.secrets["CHILDPLUS_NOTES_LIST"],
            "procare_tags_list":    st.secrets["PROCARE_TAGS_LIST"],
            "childplus_tags_list":  st.secrets["CHILDPLUS_TAGS_LIST"],
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
        }, timeout=10,
    )
    return resp.json().get("access_token") if resp.ok else None


@st.cache_data(ttl=3600, show_spinner=False)
def get_site_id(hostname: str, site_path: str, token: str) -> str | None:
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{hostname}:{site_path}",
        headers={"Authorization": f"Bearer {token}"}, timeout=10,
    )
    return resp.json().get("id") if resp.ok else None


@st.cache_data(ttl=3600, show_spinner=False)
def ensure_notes_list(site_id: str, list_name: str, token: str) -> str | None:
    """Get or create the one-row-per-note list."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    existing = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists",
        headers=headers, timeout=10,
    )
    if existing.ok:
        for lst in existing.json().get("value", []):
            if lst.get("displayName") == list_name:
                return lst["id"]

    create = requests.post(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists",
        headers=headers,
        json={
            "displayName": list_name,
            "columns": [
                {"name": "SessionID",       "text": {}},
                {"name": "NoteIndex",       "number": {}},
                {"name": "NoteCount",       "number": {}},
                {"name": "SessionType",     "choice": {"choices": ["Solo", "Group"]}},
                {"name": "Contacts",        "text": {"allowMultipleLines": True}},
                {"name": "PrimaryContact",  "text": {}},
                {"name": "PrimaryAgency",   "text": {}},
                {"name": "Destination",     "choice": {"choices": DESTINATION_OPTIONS}},
                {"name": "EventSource",     "choice": {"choices": SOURCE_OPTIONS}},
                {"name": "Tags",            "text": {}},
                {"name": "NoteText",        "text": {"allowMultipleLines": True}},
                {"name": "NoteTimestamp",   "dateTime": {}},
                {"name": "SubmittedAt",     "dateTime": {}},
            ],
            "list": {"template": "genericList"},
        }, timeout=15,
    )
    return create.json().get("id") if create.ok else None


@st.cache_data(ttl=3600, show_spinner=False)
def ensure_tags_list(site_id: str, list_name: str, token: str) -> str | None:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    existing = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists",
        headers=headers, timeout=10,
    )
    if existing.ok:
        for lst in existing.json().get("value", []):
            if lst.get("displayName") == list_name:
                return lst["id"]

    create = requests.post(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists",
        headers=headers,
        json={
            "displayName": list_name,
            "columns": [
                {"name": "TagName",   "text": {}},
                {"name": "FirstUsed", "dateTime": {}},
                {"name": "UseCount",  "number": {}},
            ],
            "list": {"template": "genericList"},
        }, timeout=15,
    )
    return create.json().get("id") if create.ok else None


def fetch_tags(destination: str) -> list[str]:
    """Return list of tag names from the destination's Tags list. [] if SharePoint not configured."""
    cfg = get_sharepoint_config()
    if not cfg:
        return st.session_state.get("local_tags", [])

    token = get_graph_token(cfg)
    if not token:
        return []

    site_path = cfg["procare_site_path"] if destination == "Procare" else cfg["childplus_site_path"]
    list_name = cfg["procare_tags_list"] if destination == "Procare" else cfg["childplus_tags_list"]
    site_id = get_site_id(cfg["hostname"], site_path, token)
    if not site_id:
        return []
    list_id = ensure_tags_list(site_id, list_name, token)
    if not list_id:
        return []

    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists/{list_id}/items?expand=fields",
        headers={"Authorization": f"Bearer {token}"}, timeout=10,
    )
    if not resp.ok:
        return []
    return sorted({
        item["fields"].get("TagName", "").strip()
        for item in resp.json().get("value", [])
        if item["fields"].get("TagName")
    })


def save_new_tag(tag_name: str, destination: str):
    """Append a new tag to the destination's Tags list."""
    cfg = get_sharepoint_config()
    if not cfg:
        st.session_state.setdefault("local_tags", [])
        if tag_name not in st.session_state.local_tags:
            st.session_state.local_tags.append(tag_name)
        return

    token = get_graph_token(cfg)
    if not token:
        return

    site_path = cfg["procare_site_path"] if destination == "Procare" else cfg["childplus_site_path"]
    list_name = cfg["procare_tags_list"] if destination == "Procare" else cfg["childplus_tags_list"]
    site_id = get_site_id(cfg["hostname"], site_path, token)
    if not site_id:
        return
    list_id = ensure_tags_list(site_id, list_name, token)
    if not list_id:
        return

    requests.post(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists/{list_id}/items",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"fields": {
            "Title":     tag_name,
            "TagName":   tag_name,
            "FirstUsed": datetime.now(timezone.utc).isoformat(),
            "UseCount":  1,
        }}, timeout=10,
    )


def save_session_notes(session_data: dict) -> tuple[bool, str]:
    """Save all notes from a session as individual SharePoint rows."""
    cfg = get_sharepoint_config()
    if not cfg:
        return False, "sharepoint_not_configured"

    token = get_graph_token(cfg)
    if not token:
        return False, "Could not acquire Graph token"

    dest = session_data["destination"]
    site_path = cfg["procare_site_path"] if dest == "Procare" else cfg["childplus_site_path"]
    list_name = cfg["procare_notes_list"] if dest == "Procare" else cfg["childplus_notes_list"]
    site_id = get_site_id(cfg["hostname"], site_path, token)
    if not site_id:
        return False, "Could not resolve SharePoint site"
    list_id = ensure_notes_list(site_id, list_name, token)
    if not list_id:
        return False, "Could not get or create notes list"

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    notes   = session_data["notes"]
    contacts_blob = format_contacts_blob(session_data["contacts"])
    primary       = session_data["contacts"][0] if session_data["contacts"] else {"name": "", "agency": ""}

    for idx, note in enumerate(notes, start=1):
        payload = {"fields": {
            "Title":          f"{primary['name']} — note {idx}/{len(notes)}",
            "SessionID":      session_data["session_id"],
            "NoteIndex":      idx,
            "NoteCount":      len(notes),
            "SessionType":    session_data["session_type"],
            "Contacts":       contacts_blob,
            "PrimaryContact": primary["name"],
            "PrimaryAgency":  primary["agency"],
            "Destination":    dest,
            "EventSource":    session_data["event_source"],
            "Tags":           ", ".join(session_data["tags"]),
            "NoteText":       note["text"],
            "NoteTimestamp":  note["timestamp"],
            "SubmittedAt":    session_data["submitted_at"],
        }}
        resp = requests.post(
            f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists/{list_id}/items",
            headers=headers, json=payload, timeout=15,
        )
        if not resp.ok:
            return False, f"Failed on note {idx}: {resp.status_code} {resp.text[:200]}"

    return True, f"Saved {len(notes)} notes"


def format_contacts_blob(contacts: list[dict]) -> str:
    parts = []
    for c in contacts:
        bits = [c.get("name", "").strip()]
        if c.get("role"):
            bits[0] += f" ({c['role']})"
        if c.get("agency"):
            bits.append(f"@ {c['agency']}")
        parts.append(" ".join(bits))
    return "; ".join(parts)


# ─────────────────────────────────────────────
# TAG SIMILARITY
# ─────────────────────────────────────────────

def find_similar_tag(new_tag: str, existing_tags: list[str]) -> str | None:
    new_norm = new_tag.lower().strip()
    if not new_norm:
        return None
    best_match, best_score = None, 0.0
    for existing in existing_tags:
        if existing.lower() == new_norm:
            return existing  # exact (case-insensitive) match
        score = SequenceMatcher(None, new_norm, existing.lower()).ratio()
        if score > best_score:
            best_score, best_match = score, existing
    return best_match if best_score >= TAG_SIMILARITY else None


# ─────────────────────────────────────────────
# AUDIO COMPONENT
# ─────────────────────────────────────────────

def audio_recorder_html(component_id: str = "rec") -> str:
    return f"""
<style>
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
  .rec-wrap {{ display:flex;flex-direction:column;align-items:center;gap:10px;padding:12px;
    background:#f8f9fa;border-radius:10px;border:1px solid #dee2e6; }}
  .rec-btn {{ width:52px;height:52px;border-radius:50%;border:none;cursor:pointer;
    font-size:22px;background:#dc3545;color:white;
    box-shadow:0 3px 10px rgba(220,53,69,0.3);transition:all 0.2s; }}
  .rec-btn.listening {{ background:#198754;animation:pulse 1s infinite; }}
  .rec-btn:disabled {{ background:#adb5bd;cursor:default; }}
  @keyframes pulse {{ 0%,100%{{transform:scale(1)}} 50%{{transform:scale(1.08)}} }}
  .rec-status {{ font-size:12px;color:#6c757d; }}
  .rec-box {{ width:100%;box-sizing:border-box;padding:8px;border-radius:6px;
    border:1px solid #ced4da;font-size:13px;min-height:50px;resize:vertical;display:none; }}
  .rec-copy {{ padding:5px 14px;background:#0d6efd;color:white;border:none;
    border-radius:6px;cursor:pointer;font-size:12px;display:none; }}
  .rec-copy:hover {{ background:#0b5ed7; }}
  .rec-copied {{ font-size:11px;color:#198754;display:none; }}
</style>
<div class="rec-wrap">
  <button class="rec-btn" id="b_{component_id}">🎙</button>
  <div class="rec-status" id="s_{component_id}">Tap to record</div>
  <textarea class="rec-box" id="t_{component_id}" placeholder="Transcription appears here…"></textarea>
  <div style="display:flex;gap:8px;align-items:center;">
    <button class="rec-copy" id="c_{component_id}" onclick="copy_{component_id}()">Copy transcript ↓</button>
    <span class="rec-copied" id="k_{component_id}">Copied!</span>
  </div>
</div>
<script>
  (function(){{
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const btn=document.getElementById('b_{component_id}');
    const status=document.getElementById('s_{component_id}');
    const tbox=document.getElementById('t_{component_id}');
    const copyBtn=document.getElementById('c_{component_id}');
    const copied=document.getElementById('k_{component_id}');
    if (!SR) {{
      status.textContent='⚠️ Not supported in this browser — use Chrome, Edge, or Safari.';
      btn.disabled=true; return;
    }}
    const rec=new SR();
    rec.continuous=true; rec.interimResults=true; rec.lang='en-US';
    let running=false, finalText='';
    btn.addEventListener('click',()=>{{
      if(!running){{
        finalText=''; tbox.value='';
        rec.start(); running=true;
        btn.textContent='⏹'; btn.classList.add('listening');
        status.textContent='Recording… tap to stop';
      }} else {{ rec.stop(); }}
    }});
    rec.onresult=(e)=>{{
      let interim='';
      for(let i=e.resultIndex;i<e.results.length;i++){{
        const t=e.results[i][0].transcript;
        if(e.results[i].isFinal){{ finalText+=t+' '; }} else {{ interim+=t; }}
      }}
      tbox.value=finalText+interim;
    }};
    rec.onend=()=>{{
      running=false; btn.textContent='🎙'; btn.classList.remove('listening');
      if(finalText.trim()){{
        status.textContent='✅ Done — copy and paste into note below';
        tbox.style.display='block'; copyBtn.style.display='inline-block';
      }} else {{ status.textContent='No speech detected. Tap to try again.'; }}
    }};
    rec.onerror=(e)=>{{
      status.textContent='Error: '+e.error+'. Tap to retry.';
      running=false; btn.textContent='🎙'; btn.classList.remove('listening');
    }};
    window['copy_{component_id}']=function(){{
      navigator.clipboard.writeText(tbox.value).then(()=>{{
        copied.style.display='inline';
        setTimeout(()=>{{ copied.style.display='none'; }},2000);
      }});
    }};
  }})();
</script>
"""


# ─────────────────────────────────────────────
# PAGE SETUP
# ─────────────────────────────────────────────

st.set_page_config(page_title="Customer Notes", page_icon="🎤", layout="centered")

st.markdown("""
<style>
  .main > div { max-width:900px; margin:auto; }
  label { font-weight:600; }
  .stTextArea textarea { font-size:15px; }
  .stButton > button {
    padding:10px 14px; font-size:15px;
    border-radius:10px; font-weight:500;
  }
  .primary-save button {
    width:100%; padding:14px; font-size:16px; font-weight:600;
    background:#198754; color:white; border:none;
  }
  .primary-save button:hover { background:#157347; color:white; }
  .mode-card button {
    width:100%; padding:20px 14px; font-size:16px; font-weight:500; text-align:left;
  }
  .tag-pill {
    display:inline-block; background:#e7f1ff; color:#0a58ca;
    padding:4px 10px; border-radius:12px; font-size:13px; margin:2px 4px 2px 0;
  }
  .note-box {
    background:#f8f9fa; border:1px solid #dee2e6;
    border-radius:10px; padding:14px; margin-bottom:10px;
  }
  .contact-box {
    background:#ffffff; border:1px solid #dee2e6;
    border-radius:10px; padding:14px; margin-bottom:10px;
  }
  .hs-panel { background:#f0f4ff; border:1px solid #c7d7ff;
    border-radius:10px; padding:14px; margin-top:8px; }
  .ticket-meta { font-size:12px; color:#6c757d; }
  .success-banner { background:#d1e7dd; color:#0f5132;
    padding:16px; border-radius:10px; text-align:center; font-weight:600; }
  .fallback-banner { background:#fff3cd; color:#664d03;
    padding:12px; border-radius:8px; font-size:13px; }
  .mode-label { font-size:12px; color:#6c757d; text-transform:uppercase;
    letter-spacing:0.05em; font-weight:600; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────

def init_state():
    defaults = {
        "mode":            None,       # None | "solo" | "group"
        "session_id":      str(uuid.uuid4()),
        "contacts":        [],          # list of dicts: {name, agency, role, hs_id, hs_data}
        "solo_search_run": False,
        "solo_results":    None,
        "group_search_run": False,
        "group_results":   None,
        "group_agency":    "",
        "group_manual_name":   "",
        "group_manual_role":   "",
        "destination":     DESTINATION_OPTIONS[0],
        "event_source":    SOURCE_OPTIONS[0],
        "notes":           [{"text": "", "timestamp": datetime.now().isoformat()}],
        "tags":            [],
        "pending_tag_input": "",
        "pending_similar_tag": None,  # {new, similar}
        "hs_context_view": None,       # contact index (int) for group mode picker
        "submitted":       False,
        "last_entry":      None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()


def reset_all():
    keys = list(st.session_state.keys())
    for k in keys:
        del st.session_state[k]
    init_state()


# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────

st.title("🎤 Customer Notes")

# Success banner
if st.session_state.submitted:
    entry = st.session_state.last_entry or {}
    note_count = len(entry.get("notes", [])) if entry else 0
    contact_count = len(entry.get("contacts", [])) if entry else 0
    st.markdown(
        f'<div class="success-banner">✅ Saved — {note_count} note(s) for {contact_count} contact(s)</div>',
        unsafe_allow_html=True
    )

    if entry.get("fallback_csv"):
        st.markdown('<div class="fallback-banner">⚠️ SharePoint not connected — download below.</div>', unsafe_allow_html=True)
        csv_header = '"SessionID","NoteIndex","SessionType","PrimaryContact","PrimaryAgency","Contacts","Destination","EventSource","Tags","NoteText","NoteTimestamp","SubmittedAt"\n'
        rows = []
        contacts_blob = format_contacts_blob(entry["contacts"])
        primary = entry["contacts"][0] if entry["contacts"] else {"name":"","agency":""}
        for i, note in enumerate(entry["notes"], 1):
            rows.append(
                f'"{entry["session_id"]}","{i}","{entry["session_type"]}",'
                f'"{primary["name"]}","{primary["agency"]}","{contacts_blob}",'
                f'"{entry["destination"]}","{entry["event_source"]}","{", ".join(entry["tags"])}",'
                f'"{note["text"].replace(chr(34),chr(39))}","{note["timestamp"]}","{entry["submitted_at"]}"\n'
            )
        st.download_button(
            "⬇️ Download as CSV",
            data=csv_header + "".join(rows),
            file_name=f"interview_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
        )

    if st.button("➕ Capture another", type="primary"):
        reset_all()
        st.rerun()
    st.stop()


# ─────────────────────────────────────────────
# STEP 1 — MODE SELECTOR
# ─────────────────────────────────────────────

if st.session_state.mode is None:
    st.caption("What are you capturing?")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown('<div class="mode-card">', unsafe_allow_html=True)
        if st.button("🧑 Solo conversation\n\n*One person*", use_container_width=True, key="pick_solo"):
            st.session_state.mode = "solo"
            st.session_state.contacts = [{"name":"","agency":"","role":"","hs_id":None,"hs_data":None}]
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    with col2:
        st.markdown('<div class="mode-card">', unsafe_allow_html=True)
        if st.button("👥 Group conversation\n\n*Multiple people*", use_container_width=True, key="pick_group"):
            st.session_state.mode = "group"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    st.stop()


# Mode ribbon
mode_label = "Solo conversation" if st.session_state.mode == "solo" else "Group conversation"
col_a, col_b = st.columns([4, 1])
with col_a:
    st.markdown(f'<div class="mode-label">{mode_label}</div>', unsafe_allow_html=True)
with col_b:
    if st.button("← Change", use_container_width=True):
        reset_all()
        st.rerun()


# ─────────────────────────────────────────────
# STEP 2 — CONTACT(S)
# ─────────────────────────────────────────────

st.divider()
hs_token = get_hubspot_token()

# ── SOLO MODE ──
if st.session_state.mode == "solo":
    st.subheader("Who did you talk to?")
    c = st.session_state.contacts[0]

    colA, colB = st.columns([1, 1])
    with colA:
        c["name"] = st.text_input("Name *", value=c.get("name",""), placeholder="First and last name", key="solo_name")
    with colB:
        c["agency"] = st.text_input("Organization / Agency *", value=c.get("agency",""), placeholder="e.g. Bright Horizons", key="solo_agency")

    c["role"] = st.text_input("Title / Role", value=c.get("role",""), placeholder="e.g. Executive Director", key="solo_role")

    # HubSpot lookup
    with st.container():
        h1, h2 = st.columns([4, 1])
        with h1:
            st.markdown("**🔍 HubSpot Lookup** *(optional)*")
            if not hs_token:
                st.caption("HubSpot not configured.")
            else:
                st.caption("Find existing contact to pre-fill and load support history.")
        with h2:
            can_search = hs_token and (c["name"].strip() or c["agency"].strip())
            if st.button("Search", disabled=not can_search, use_container_width=True, key="solo_search"):
                with st.spinner("Searching HubSpot…"):
                    st.session_state.solo_results = search_hubspot_contacts(c["name"], c["agency"], hs_token)
                    st.session_state.solo_search_run = True

    if st.session_state.solo_search_run:
        results = st.session_state.solo_results or []
        if not results:
            st.info("No match identified")
        else:
            options = []
            for r in results:
                p = r["properties"]
                label = f"{p.get('firstname','')} {p.get('lastname','')}".strip() or "(no name)"
                if p.get("company"):  label += f"  ·  {p['company']}"
                if p.get("jobtitle"): label += f"  ·  {p['jobtitle']}"
                options.append(label)
            options.append("None of these")
            choice = st.radio("Select the right person:", options, key="solo_choice")
            if choice != "None of these":
                chosen = results[options.index(choice)]
                if c.get("hs_id") != chosen["id"]:
                    p = chosen["properties"]
                    c["name"]   = f"{p.get('firstname','')} {p.get('lastname','')}".strip() or c["name"]
                    if p.get("company"):  c["agency"] = p["company"]
                    if p.get("jobtitle"): c["role"]   = p["jobtitle"]
                    c["hs_id"]   = chosen["id"]
                    c["hs_data"] = chosen
                    with st.spinner("Loading HubSpot history…"):
                        c["hs_tickets"] = get_contact_tickets(chosen["id"], hs_token)
                    st.rerun()
            else:
                c["hs_id"] = None; c["hs_data"] = None; c.pop("hs_tickets", None)


# ── GROUP MODE ──
else:
    st.subheader("Group setup")
    st.caption("Start by finding the agency, then add attendees.")

    st.session_state.group_agency = st.text_input(
        "Agency / Organization *",
        value=st.session_state.group_agency,
        placeholder="e.g. Bright Futures Head Start", key="group_agency_input"
    )

    h1, h2 = st.columns([4, 1])
    with h1:
        st.markdown("**🔍 Find contacts at this agency**")
    with h2:
        can_search_group = hs_token and st.session_state.group_agency.strip()
        if st.button("Search", disabled=not can_search_group, use_container_width=True, key="group_search"):
            with st.spinner("Searching HubSpot…"):
                st.session_state.group_results = search_contacts_by_agency(st.session_state.group_agency, hs_token)
                st.session_state.group_search_run = True

    if st.session_state.group_search_run:
        results = st.session_state.group_results or []
        if not results:
            st.info("No contacts found in HubSpot for that agency. Add people manually below.")
        else:
            st.markdown(f"**{len(results)} contact(s) at {st.session_state.group_agency}:**")
            added_ids = {c.get("hs_id") for c in st.session_state.contacts if c.get("hs_id")}
            for r in results:
                p = r["properties"]
                label = f"{p.get('firstname','')} {p.get('lastname','')}".strip() or "(no name)"
                meta  = p.get("jobtitle", "") or "(no title)"
                is_added = r["id"] in added_ids
                cols = st.columns([5, 1])
                with cols[0]:
                    st.markdown(f"**{label}** · *{meta}*")
                with cols[1]:
                    if is_added:
                        st.markdown("✓ Added")
                    else:
                        if st.button("+ Add", key=f"add_{r['id']}"):
                            with st.spinner("Loading…"):
                                tickets = get_contact_tickets(r["id"], hs_token)
                            st.session_state.contacts.append({
                                "name": label,
                                "agency": p.get("company") or st.session_state.group_agency,
                                "role": p.get("jobtitle",""),
                                "hs_id": r["id"],
                                "hs_data": r,
                                "hs_tickets": tickets,
                            })
                            st.rerun()

    # Manual add
    st.divider()
    st.markdown("**+ Add someone not in HubSpot**")
    m1, m2, m3 = st.columns([2, 2, 1])
    with m1:
        manual_name = st.text_input("Name", key="group_manual_name", label_visibility="collapsed", placeholder="Name")
    with m2:
        manual_role = st.text_input("Role", key="group_manual_role", label_visibility="collapsed", placeholder="Role (optional)")
    with m3:
        if st.button("Add", use_container_width=True, key="manual_add"):
            if manual_name.strip():
                st.session_state.contacts.append({
                    "name": manual_name.strip(),
                    "agency": st.session_state.group_agency,
                    "role": manual_role.strip(),
                    "hs_id": None,
                    "hs_data": None,
                })
                st.session_state.group_manual_name = ""
                st.session_state.group_manual_role = ""
                st.rerun()

    # Show attendees
    if st.session_state.contacts:
        st.divider()
        st.markdown(f"**Attendees ({len(st.session_state.contacts)}):**")
        for idx, c in enumerate(st.session_state.contacts):
            cols = st.columns([5, 1])
            with cols[0]:
                hs_tag = " 🟢 HubSpot" if c.get("hs_id") else ""
                role_part = f" · {c['role']}" if c.get("role") else ""
                st.markdown(f"**{c['name']}**{role_part}{hs_tag}")
            with cols[1]:
                if st.button("Remove", key=f"rm_{idx}", use_container_width=True):
                    st.session_state.contacts.pop(idx)
                    st.rerun()


# ─────────────────────────────────────────────
# STEP 3 — CONTEXT
# ─────────────────────────────────────────────

st.divider()
st.subheader("Context")

c1, c2 = st.columns(2)
with c1:
    destination = st.selectbox(
        "Destination *", DESTINATION_OPTIONS, key="destination",
        help="Routes to the matching SharePoint list for that product line"
    )
with c2:
    event_source = st.selectbox("Where did you meet?", SOURCE_OPTIONS, key="event_source")


# ─────────────────────────────────────────────
# STEP 4 — NOTES
# ─────────────────────────────────────────────

st.divider()
st.subheader("Notes")
st.caption("🎙 Mic captures your voice from the device's microphone. For in-person capture — not call audio.")

components.html(audio_recorder_html(f"mic_{len(st.session_state.notes)}"), height=200)

for i, note in enumerate(st.session_state.notes):
    with st.container():
        st.markdown(f'<div class="mode-label">Note {i+1}</div>', unsafe_allow_html=True)
        cols = st.columns([10, 1])
        with cols[0]:
            note["text"] = st.text_area(
                f"Note {i+1} text",
                value=note["text"],
                key=f"note_text_{i}",
                height=100,
                label_visibility="collapsed",
                placeholder="Paste transcription or type a note…",
            )
        with cols[1]:
            if len(st.session_state.notes) > 1:
                if st.button("🗑", key=f"rm_note_{i}", help="Remove this note"):
                    st.session_state.notes.pop(i)
                    st.rerun()

if st.button("+ Add another note", use_container_width=True):
    st.session_state.notes.append({"text": "", "timestamp": datetime.now().isoformat()})
    st.rerun()


# ─────────────────────────────────────────────
# STEP 5 — TAGS
# ─────────────────────────────────────────────

st.divider()
st.subheader("Tags")
st.caption(f"Topics for this session. Separate by product line (Procare vs ChildPlus).")

existing_tags = fetch_tags(st.session_state.destination)

# Selected pills
if st.session_state.tags:
    cols = st.columns(len(st.session_state.tags) + 1)
    for i, t in enumerate(st.session_state.tags):
        with cols[i]:
            if st.button(f"✕ {t}", key=f"rm_tag_{i}"):
                st.session_state.tags.remove(t)
                st.rerun()

# Tag picker: existing tags + freeform input
pick_col, add_col = st.columns([3, 1])
with pick_col:
    available = [t for t in existing_tags if t not in st.session_state.tags]
    if available:
        picked = st.selectbox(
            "Pick existing tag",
            options=["— Pick existing —"] + available,
            key="existing_tag_picker",
        )
        if picked != "— Pick existing —" and picked not in st.session_state.tags:
            st.session_state.tags.append(picked)
            st.rerun()
    else:
        st.caption("No existing tags yet for this destination.")

with add_col:
    if st.button("+ New tag", use_container_width=True):
        st.session_state.pending_tag_input = "OPEN"

if st.session_state.pending_tag_input == "OPEN":
    new_tag_text = st.text_input("Type new tag name", key="new_tag_field", placeholder="e.g. reporting")
    tcol1, tcol2 = st.columns(2)
    with tcol1:
        if st.button("Check & add", use_container_width=True, key="check_tag"):
            candidate = new_tag_text.strip()
            if candidate:
                similar = find_similar_tag(candidate, existing_tags)
                if similar and similar.lower() != candidate.lower():
                    st.session_state.pending_similar_tag = {"new": candidate, "similar": similar}
                elif similar:  # exact match (case-insensitive)
                    if similar not in st.session_state.tags:
                        st.session_state.tags.append(similar)
                    st.session_state.pending_tag_input = ""
                    st.rerun()
                else:
                    save_new_tag(candidate, st.session_state.destination)
                    st.session_state.tags.append(candidate)
                    st.session_state.pending_tag_input = ""
                    st.rerun()
    with tcol2:
        if st.button("Cancel", use_container_width=True, key="cancel_tag"):
            st.session_state.pending_tag_input = ""
            st.session_state.pending_similar_tag = None
            st.rerun()

# Similar tag prompt
if st.session_state.pending_similar_tag:
    s = st.session_state.pending_similar_tag
    st.warning(f"**Similar tag exists:** `{s['similar']}`  —  did you mean that?")
    scol1, scol2 = st.columns(2)
    with scol1:
        if st.button(f"✓ Use existing: {s['similar']}", use_container_width=True):
            if s["similar"] not in st.session_state.tags:
                st.session_state.tags.append(s["similar"])
            st.session_state.pending_similar_tag = None
            st.session_state.pending_tag_input = ""
            st.rerun()
    with scol2:
        if st.button(f"+ Add as new: {s['new']}", use_container_width=True):
            save_new_tag(s["new"], st.session_state.destination)
            st.session_state.tags.append(s["new"])
            st.session_state.pending_similar_tag = None
            st.session_state.pending_tag_input = ""
            st.rerun()


# ─────────────────────────────────────────────
# STEP 6 — SAVE
# ─────────────────────────────────────────────

st.divider()

st.markdown('<div class="primary-save">', unsafe_allow_html=True)
save_clicked = st.button("💾 Save session", use_container_width=True, key="save_btn", type="primary")
st.markdown('</div>', unsafe_allow_html=True)

if save_clicked:
    # Validation
    errors = []
    valid_contacts = [c for c in st.session_state.contacts if c.get("name","").strip() and c.get("agency","").strip()]
    if not valid_contacts:
        errors.append("At least one contact with name and agency is required.")
    valid_notes = [n for n in st.session_state.notes if n["text"].strip()]
    if not valid_notes:
        errors.append("At least one note with text is required.")

    if errors:
        for e in errors: st.error(e)
    else:
        session_data = {
            "session_id":   st.session_state.session_id,
            "session_type": "Solo" if st.session_state.mode == "solo" else "Group",
            "contacts":     valid_contacts,
            "destination":  st.session_state.destination,
            "event_source": st.session_state.event_source,
            "notes":        valid_notes,
            "tags":         st.session_state.tags,
            "submitted_at": datetime.now().isoformat(),
        }
        with st.spinner(f"Saving {len(valid_notes)} note(s)…"):
            success, message = save_session_notes(session_data)

        if success:
            st.session_state.last_entry = session_data
            st.session_state.submitted  = True
            st.rerun()
        elif message == "sharepoint_not_configured":
            session_data["fallback_csv"] = True
            st.session_state.last_entry  = session_data
            st.session_state.submitted   = True
            st.rerun()
        else:
            st.error(f"Save failed: {message}")


# ─────────────────────────────────────────────
# HUBSPOT CONTEXT (BOTTOM)
# ─────────────────────────────────────────────

hs_contacts = [c for c in st.session_state.contacts if c.get("hs_id")]
if hs_contacts:
    st.divider()
    st.subheader("📋 HubSpot Context")
    st.caption("Read-only — not saved with notes. For reference only.")

    if len(hs_contacts) == 1:
        selected_contact = hs_contacts[0]
    else:
        labels = [c["name"] or "(no name)" for c in hs_contacts]
        pick = st.selectbox("View context for:", labels, key="hs_view_picker")
        selected_contact = hs_contacts[labels.index(pick)]

    hs = selected_contact["hs_data"]
    p  = hs["properties"]

    with st.container():
        st.markdown('<div class="hs-panel">', unsafe_allow_html=True)
        col1, col2 = st.columns(2)
        with col1:
            full_name = f"{p.get('firstname','').strip()} {p.get('lastname','').strip()}".strip()
            st.markdown(f"**{full_name or selected_contact['name']}**")
            if p.get("email"): st.markdown(f"✉️ {p['email']}")
            if p.get("phone"): st.markdown(f"📞 {p['phone']}")
        with col2:
            if p.get("company"):  st.markdown(f"🏢 {p['company']}")
            if p.get("jobtitle"): st.markdown(f"💼 {p['jobtitle']}")
            st.markdown(f"[Open in HubSpot ↗]({hs.get('url', '#')})")
        st.markdown("</div>", unsafe_allow_html=True)

        tickets = selected_contact.get("hs_tickets") or []
        st.markdown(f"**Recent Support Tickets** — last {TICKET_DAYS} days")
        if not tickets:
            st.caption("No tickets in this period.")
        else:
            for t in tickets:
                tp = t["properties"]
                raw = tp.get("subject","") or ""
                subj = raw.split(" - ", 1)[1] if " - " in raw else raw
                try:
                    date_str = datetime.fromisoformat(tp["createdate"].replace("Z","+00:00")).strftime("%b %d, %Y")
                except Exception:
                    date_str = ""
                pri   = (tp.get("hs_ticket_priority") or "").upper()
                emoji = {"HIGH":"🔴","MEDIUM":"🟡","LOW":"🟢"}.get(pri, "⚪")
                body  = tp.get("content","") or ""
                snip  = (body[:200] + "…") if len(body) > 200 else body
                with st.expander(f"{emoji} {subj or '(no subject)'} — {date_str}"):
                    if pri:
                        st.markdown(f'<span class="ticket-meta">Priority: {pri.title()}</span>', unsafe_allow_html=True)
                    if snip: st.markdown(snip)
                    st.markdown(f"[View in HubSpot ↗]({t.get('url','#')})")
