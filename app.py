import streamlit as st
import requests
import uuid
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from streamlit_mic_recorder import speech_to_text

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

DESTINATION_OPTIONS = ["Procare", "ChildPlus"]
SOURCE_OPTIONS      = ["Conference", "Site Visit", "Zoom", "Other"]
HS_BASE             = "https://api.hubapi.com"
TICKET_DAYS         = 90
TICKET_MAX          = 5
TAG_SIMILARITY      = 0.75  # 75% similarity threshold for "did you mean?"

# HubSpot is only used for ChildPlus. Procare contacts are captured manually.
HS_DESTINATION = "ChildPlus"

# HubSpot contact properties pulled on every search.
# Includes ChildPlus-specific identifiers (database_name, license, IKN) so that
# duplicate / similar contacts can be visually disambiguated by the user.
HS_CONTACT_PROPS = [
    "firstname", "lastname", "email", "company", "phone", "jobtitle",
    "database_name", "childplus_license_number", "ikn__c",
]

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


def _contact_sort_key(contact: dict) -> tuple:
    """Sort contacts alphabetically by last name then first name (case-insensitive).
    Empty / missing names sort to the bottom so meaningful records stay at top."""
    p = contact.get("properties", {})
    last  = (p.get("lastname")  or "").strip().lower()
    first = (p.get("firstname") or "").strip().lower()
    return (last == "", last, first == "", first)


def _search_contacts(filters: list, token: str) -> list:
    if not filters:
        return []
    resp = requests.post(
        f"{HS_BASE}/crm/v3/objects/contacts/search",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "filterGroups": [{"filters": filters}],
            "properties": HS_CONTACT_PROPS,
            "limit": 25,
        },
        timeout=10,
    )
    if not resp.ok:
        return []
    results = resp.json().get("results", [])
    results.sort(key=_contact_sort_key)
    return results


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


def render_contact_card(props: dict) -> str:
    """Build the markdown card body shown in solo search results & group attendee list.
    Always renders Agency, Database, Email, Role rows, marking missing fields explicitly
    so users can disambiguate similar/duplicate contacts."""
    full_name = f"{props.get('firstname','').strip()} {props.get('lastname','').strip()}".strip() or "(no name)"

    def field(value, missing_label="—"):
        v = (value or "").strip()
        return v if v else f":gray[{missing_label}]"

    db_id_parts = []
    if (props.get("database_name") or "").strip():
        db_id_parts.append(props["database_name"].strip())
    if (props.get("ikn__c") or "").strip():
        db_id_parts.append(f"IKN {props['ikn__c'].strip()}")
    if (props.get("childplus_license_number") or "").strip():
        db_id_parts.append(f"Lic {props['childplus_license_number'].strip()}")
    db_display = " · ".join(db_id_parts) if db_id_parts else ":gray[—]"

    lines = [
        f"**{full_name}**",
        f"🏢 **Agency:** {field(props.get('company'), '— no agency on record')}",
        f"🗄 **Database / ID:** {db_display}",
        f"✉️ **Email:** {field(props.get('email'), '— no email on record')}",
        f"💼 **Role:** {field(props.get('jobtitle'), '— no role on record')}",
    ]
    return "  \n".join(lines)


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
                {"name": "PrimaryDatabase", "text": {}},
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
    primary       = session_data["contacts"][0] if session_data["contacts"] else {"name": "", "agency": "", "database": ""}

    for idx, note in enumerate(notes, start=1):
        payload = {"fields": {
            "Title":           f"{primary['name']} — note {idx}/{len(notes)}",
            "SessionID":       session_data["session_id"],
            "NoteIndex":       idx,
            "NoteCount":       len(notes),
            "SessionType":     session_data["session_type"],
            "Contacts":        contacts_blob,
            "PrimaryContact":  primary["name"],
            "PrimaryAgency":   primary["agency"],
            "PrimaryDatabase": primary.get("database", ""),
            "Destination":     dest,
            "EventSource":     session_data["event_source"],
            "Tags":            ", ".join(session_data["tags"]),
            "NoteText":        note["text"],
            "NoteTimestamp":   note["timestamp"],
            "SubmittedAt":     session_data["submitted_at"],
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
        agency_part = c.get("agency", "").strip()
        db_part = c.get("database", "").strip()
        if agency_part and db_part:
            bits.append(f"@ {agency_part} [{db_part}]")
        elif agency_part:
            bits.append(f"@ {agency_part}")
        elif db_part:
            bits.append(f"@ [{db_part}]")
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
            return existing
        score = SequenceMatcher(None, new_norm, existing.lower()).ratio()
        if score > best_score:
            best_score, best_match = score, existing
    return best_match if best_score >= TAG_SIMILARITY else None


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
        "destination":     None,
        "mode":            None,
        "session_id":      str(uuid.uuid4()),
        "contacts":        [],
        "solo_search_run": False,
        "solo_results":    None,
        "solo_hs_id":      None,
        "solo_hs_data":    None,
        "solo_hs_tickets": None,
        "group_search_run": False,
        "group_results":   None,
        "group_agency":    "",
        "group_manual_name":   "",
        "group_manual_role":   "",
        "event_source":    SOURCE_OPTIONS[0],
        "notes":           [{"text": "", "timestamp": datetime.now().isoformat()}],
        "tags":            [],
        "pending_tag_input": "",
        "pending_similar_tag": None,
        "submitted":       False,
        "last_entry":      None,
        # Solo-mode contact fields — single source of truth
        "solo_name":       "",
        "solo_agency":     "",
        "solo_role":       "",
        "solo_database":   "",
        # Audio recording state
        "pending_transcript": None,   # transcript awaiting user review/accept
        "recorder_counter":   0,      # bumped after each accept/discard to reset recorder
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


def build_solo_contact() -> dict:
    """Read the solo contact's current state from session_state widget keys."""
    return {
        "name":     st.session_state.get("solo_name", "").strip(),
        "agency":   st.session_state.get("solo_agency", "").strip(),
        "role":     st.session_state.get("solo_role", "").strip(),
        "database": st.session_state.get("solo_database", "").strip(),
        "hs_id":    st.session_state.get("solo_hs_id"),
        "hs_data":  st.session_state.get("solo_hs_data"),
        "hs_tickets": st.session_state.get("solo_hs_tickets"),
    }


def add_transcript_to_notes(transcript: str):
    """Place an accepted transcript in the next available note slot.

    Rule: 'open' = a note whose current text is empty/whitespace.
    If at least one note is open, fill the first one. Otherwise append a new note.
    Never overwrites a note that the user has typed in.
    """
    text = (transcript or "").strip()
    if not text:
        return

    # Look at the live widget value first (what the user has actually typed),
    # falling back to the dict for newly-added notes that haven't rendered yet.
    def note_is_open(idx: int) -> bool:
        widget_key = f"note_text_{idx}"
        if widget_key in st.session_state:
            return not st.session_state[widget_key].strip()
        return not st.session_state.notes[idx]["text"].strip()

    empty_idx = next(
        (i for i in range(len(st.session_state.notes)) if note_is_open(i)),
        None,
    )

    now_ts = datetime.now().isoformat()
    if empty_idx is not None:
        # Fill the open slot. Set BOTH the dict and the widget key so the text_area
        # reflects the new value on the next render (Streamlit widget keys override
        # the `value=` argument once they exist).
        st.session_state.notes[empty_idx]["text"] = text
        st.session_state.notes[empty_idx]["timestamp"] = now_ts
        st.session_state[f"note_text_{empty_idx}"] = text
    else:
        # Append a brand-new note. Pre-seed the widget key so the text_area for
        # this new index shows the transcript on its first render.
        new_idx = len(st.session_state.notes)
        st.session_state.notes.append({"text": text, "timestamp": now_ts})
        st.session_state[f"note_text_{new_idx}"] = text


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
        csv_header = '"SessionID","NoteIndex","SessionType","PrimaryContact","PrimaryAgency","PrimaryDatabase","Contacts","Destination","EventSource","Tags","NoteText","NoteTimestamp","SubmittedAt"\n'
        rows = []
        contacts_blob = format_contacts_blob(entry["contacts"])
        primary = entry["contacts"][0] if entry["contacts"] else {"name":"","agency":"","database":""}
        for i, note in enumerate(entry["notes"], 1):
            rows.append(
                f'"{entry["session_id"]}","{i}","{entry["session_type"]}",'
                f'"{primary["name"]}","{primary["agency"]}","{primary.get("database","")}","{contacts_blob}",'
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
# STEP 1 — DESTINATION (product line)
# ─────────────────────────────────────────────

if st.session_state.destination is None:
    st.caption("Step 1 of 2 — Which product line?")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown('<div class="mode-card">', unsafe_allow_html=True)
        if st.button("🏢 Procare\n\n*Procare contacts and notes*", use_container_width=True, key="pick_procare"):
            st.session_state.destination = "Procare"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    with col2:
        st.markdown('<div class="mode-card">', unsafe_allow_html=True)
        if st.button("🧒 ChildPlus\n\n*ChildPlus contacts and notes*", use_container_width=True, key="pick_childplus"):
            st.session_state.destination = "ChildPlus"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    st.stop()


# ─────────────────────────────────────────────
# STEP 2 — MODE (solo or group)
# ─────────────────────────────────────────────

if st.session_state.mode is None:
    st.caption(f"📋 {st.session_state.destination}  ·  Step 2 of 2 — What are you capturing?")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown('<div class="mode-card">', unsafe_allow_html=True)
        if st.button("🧑 Solo conversation\n\n*One person*", use_container_width=True, key="pick_solo"):
            st.session_state.mode = "solo"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    with col2:
        st.markdown('<div class="mode-card">', unsafe_allow_html=True)
        if st.button("👥 Group conversation\n\n*Multiple people*", use_container_width=True, key="pick_group"):
            st.session_state.mode = "group"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    st.write("")
    if st.button("← Back to product line"):
        st.session_state.destination = None
        st.rerun()
    st.stop()


# ─────────────────────────────────────────────
# RIBBON — both selections + change button
# ─────────────────────────────────────────────

mode_label   = "Solo conversation" if st.session_state.mode == "solo" else "Group conversation"
ribbon_text  = f"{st.session_state.destination}  ·  {mode_label}"
hs_available = st.session_state.destination == HS_DESTINATION

col_a, col_b = st.columns([4, 1])
with col_a:
    st.markdown(f'<div class="mode-label">{ribbon_text}</div>', unsafe_allow_html=True)
with col_b:
    if st.button("← Change", use_container_width=True):
        reset_all()
        st.rerun()


# ─────────────────────────────────────────────
# STEP 3 — CONTACT(S)
# ─────────────────────────────────────────────

st.divider()
hs_token = get_hubspot_token()

# ── SOLO MODE ──
if st.session_state.mode == "solo":
    st.subheader("Who did you talk to?")

    colA, colB = st.columns([1, 1])
    with colA:
        st.text_input("Name *", placeholder="First and last name", key="solo_name")
    with colB:
        st.text_input("Organization / Agency *", placeholder="e.g. Bright Horizons", key="solo_agency")

    st.text_input("Title / Role", placeholder="e.g. Executive Director", key="solo_role")

    if hs_available:
        with st.container():
            h1, h2 = st.columns([4, 1])
            with h1:
                st.markdown("**🔍 HubSpot Lookup** *(optional)*")
                if not hs_token:
                    st.caption("HubSpot not configured.")
                else:
                    st.caption("Find existing contact to pre-fill and load support history.")
            with h2:
                can_search = hs_token and (
                    st.session_state.solo_name.strip() or st.session_state.solo_agency.strip()
                )
                if st.button("Search", disabled=not can_search, use_container_width=True, key="solo_search"):
                    with st.spinner("Searching HubSpot…"):
                        st.session_state.solo_results = search_hubspot_contacts(
                            st.session_state.solo_name,
                            st.session_state.solo_agency,
                            hs_token,
                        )
                        st.session_state.solo_search_run = True

        if st.session_state.solo_search_run:
            results = st.session_state.solo_results or []
            if not results:
                st.info("No match identified")
            else:
                if len(results) > 1:
                    st.caption(f"**{len(results)} possible matches** — sorted alphabetically by last name. Each card shows agency, database, email, and role to help you pick the right record.")

                current_id = st.session_state.solo_hs_id
                for r in results:
                    p = r["properties"]
                    is_selected = (r["id"] == current_id)
                    with st.container(border=True):
                        cc1, cc2 = st.columns([5, 1])
                        with cc1:
                            st.markdown(render_contact_card(p))
                        with cc2:
                            if is_selected:
                                st.success("Selected")
                                if st.button("Clear", key=f"clr_{r['id']}", use_container_width=True):
                                    st.session_state.solo_hs_id      = None
                                    st.session_state.solo_hs_data    = None
                                    st.session_state.solo_hs_tickets = None
                                    st.rerun()
                            else:
                                if st.button("Use this", key=f"use_{r['id']}", use_container_width=True, type="primary"):
                                    full_name = f"{p.get('firstname','')} {p.get('lastname','')}".strip()
                                    if full_name:
                                        st.session_state.solo_name = full_name
                                    if p.get("company"):
                                        st.session_state.solo_agency = p["company"]
                                    if p.get("jobtitle"):
                                        st.session_state.solo_role = p["jobtitle"]
                                    st.session_state.solo_database = p.get("database_name") or ""
                                    st.session_state.solo_hs_id    = r["id"]
                                    st.session_state.solo_hs_data  = r
                                    with st.spinner("Loading HubSpot history…"):
                                        st.session_state.solo_hs_tickets = get_contact_tickets(r["id"], hs_token)
                                    st.rerun()


# ── GROUP MODE ──
else:
    st.subheader("Group setup")

    if hs_available:
        st.caption("Start by finding the agency, then add attendees.")
    else:
        st.caption("Add each person below. Procare contacts are added manually.")

    st.session_state.group_agency = st.text_input(
        "Agency / Organization *",
        value=st.session_state.group_agency,
        placeholder="e.g. Bright Futures Head Start", key="group_agency_input"
    )

    if hs_available:
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
                st.markdown(f"**{len(results)} contact(s) at {st.session_state.group_agency}** — sorted alphabetically by last name:")
                added_ids = {ct.get("hs_id") for ct in st.session_state.contacts if ct.get("hs_id")}
                for r in results:
                    p = r["properties"]
                    is_added = r["id"] in added_ids
                    with st.container(border=True):
                        gcols = st.columns([5, 1])
                        with gcols[0]:
                            st.markdown(render_contact_card(p))
                        with gcols[1]:
                            if is_added:
                                st.markdown("✓ Added")
                            else:
                                if st.button("+ Add", key=f"add_{r['id']}", use_container_width=True):
                                    with st.spinner("Loading…"):
                                        tickets = get_contact_tickets(r["id"], hs_token)
                                    st.session_state.contacts.append({
                                        "name": f"{p.get('firstname','')} {p.get('lastname','')}".strip() or "(no name)",
                                        "agency": p.get("company") or st.session_state.group_agency,
                                        "role": p.get("jobtitle","") or "",
                                        "database": p.get("database_name","") or "",
                                        "hs_id": r["id"],
                                        "hs_data": r,
                                        "hs_tickets": tickets,
                                    })
                                    st.rerun()

        st.divider()
        st.markdown("**+ Add someone not in HubSpot**")
    else:
        st.divider()
        st.markdown("**+ Add an attendee**")

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
                    "database": "",
                    "hs_id": None,
                    "hs_data": None,
                })
                st.session_state.group_manual_name = ""
                st.session_state.group_manual_role = ""
                st.rerun()

    if st.session_state.contacts:
        st.divider()
        st.markdown(f"**Attendees ({len(st.session_state.contacts)}):**")
        for idx, c in enumerate(st.session_state.contacts):
            cols = st.columns([5, 1])
            with cols[0]:
                hs_tag = " 🟢 HubSpot" if c.get("hs_id") else ""
                role_part = f" · {c['role']}" if c.get("role") else ""
                db_part = f" · 🗄 {c['database']}" if c.get("database") else ""
                st.markdown(f"**{c['name']}**{role_part}{db_part}{hs_tag}")
            with cols[1]:
                if st.button("Remove", key=f"rm_{idx}", use_container_width=True):
                    st.session_state.contacts.pop(idx)
                    st.rerun()


# ─────────────────────────────────────────────
# STEP 4 — EVENT SOURCE
# ─────────────────────────────────────────────

st.divider()
st.subheader("Where did you meet?")
st.selectbox(
    "Where did you meet?", SOURCE_OPTIONS, key="event_source",
    label_visibility="collapsed",
)


# ─────────────────────────────────────────────
# STEP 5 — NOTES (with audio capture)
# ─────────────────────────────────────────────

st.divider()
st.subheader("Notes")

# ── Audio capture: record → review → confirm → goes into next open note ──
with st.container(border=True):
    st.markdown("**🎙 Record a quote or thought**")
    st.caption("Tap to record, tap again to stop. Your voice from the device's mic — not call audio.")

    # Show the recorder unless we're already reviewing a transcript
    if st.session_state.pending_transcript is None:
        new_text = speech_to_text(
            start_prompt="🎙  Start recording",
            stop_prompt="⏹  Stop recording",
            language="en",
            use_container_width=True,
            just_once=True,
            key=f"recorder_{st.session_state.recorder_counter}",
        )
        if new_text:
            st.session_state.pending_transcript = new_text
            st.rerun()
    else:
        # Review UI: edit transcript, then accept or discard
        st.markdown("**Review what you said** — edit if anything is off, then add it to your notes.")
        edited = st.text_area(
            "Transcript",
            value=st.session_state.pending_transcript,
            key="transcript_review_text",
            height=140,
            label_visibility="collapsed",
        )
        rcol1, rcol2 = st.columns(2)
        with rcol1:
            if st.button("✓  Looks good — add to notes", use_container_width=True, type="primary", key="accept_transcript"):
                add_transcript_to_notes(edited)
                st.session_state.pending_transcript = None
                st.session_state.recorder_counter += 1
                # Drop the review widget's key so it doesn't carry stale text
                st.session_state.pop("transcript_review_text", None)
                st.rerun()
        with rcol2:
            if st.button("✕  Discard", use_container_width=True, key="discard_transcript"):
                st.session_state.pending_transcript = None
                st.session_state.recorder_counter += 1
                st.session_state.pop("transcript_review_text", None)
                st.rerun()

# ── Notes list ──
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
                placeholder="Type a note, or use the recorder above.",
            )
        with cols[1]:
            if len(st.session_state.notes) > 1:
                if st.button("🗑", key=f"rm_note_{i}", help="Remove this note"):
                    st.session_state.notes.pop(i)
                    # Clean up the widget key for the deleted slot
                    st.session_state.pop(f"note_text_{i}", None)
                    st.rerun()

if st.button("+ Add another note", use_container_width=True):
    st.session_state.notes.append({"text": "", "timestamp": datetime.now().isoformat()})
    st.rerun()


# ─────────────────────────────────────────────
# STEP 6 — TAGS
# ─────────────────────────────────────────────

st.divider()
st.subheader("Tags")
st.caption(f"Topics for this session — saved to the {st.session_state.destination} tag list.")

existing_tags = fetch_tags(st.session_state.destination)

if st.session_state.tags:
    cols = st.columns(len(st.session_state.tags) + 1)
    for i, t in enumerate(st.session_state.tags):
        with cols[i]:
            if st.button(f"✕ {t}", key=f"rm_tag_{i}"):
                st.session_state.tags.remove(t)
                st.rerun()

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
                elif similar:
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
# STEP 7 — SAVE
# ─────────────────────────────────────────────

st.divider()

st.markdown('<div class="primary-save">', unsafe_allow_html=True)
save_clicked = st.button("💾 Save session", use_container_width=True, key="save_btn", type="primary")
st.markdown('</div>', unsafe_allow_html=True)

if save_clicked:
    if st.session_state.mode == "solo":
        contacts_to_save = [build_solo_contact()]
    else:
        contacts_to_save = st.session_state.contacts

    errors = []
    valid_contacts = [c for c in contacts_to_save if c.get("name","").strip() and c.get("agency","").strip()]
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
# HUBSPOT CONTEXT (BOTTOM) — only when destination = ChildPlus
# ─────────────────────────────────────────────

if hs_available:
    hs_contacts = []
    if st.session_state.mode == "solo":
        if st.session_state.solo_hs_id and st.session_state.solo_hs_data:
            hs_contacts = [build_solo_contact()]
    else:
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
                if p.get("company"):       st.markdown(f"🏢 {p['company']}")
                if p.get("jobtitle"):      st.markdown(f"💼 {p['jobtitle']}")
                if p.get("database_name"): st.markdown(f"🗄 **DB:** {p['database_name']}")
                if p.get("ikn__c"):        st.markdown(f"🔢 **IKN:** {p['ikn__c']}")
                if p.get("childplus_license_number"):
                    st.markdown(f"🪪 **License:** {p['childplus_license_number']}")
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
