import streamlit as st
import requests
import uuid
import html
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
TAG_SIMILARITY      = 0.75  # Retained for future use; tag autocomplete is OFF in stopgap.

# HubSpot is only used for ChildPlus. Procare contacts are captured manually.
HS_DESTINATION = "ChildPlus"

HS_CONTACT_PROPS = [
    "firstname", "lastname", "email", "company", "phone", "jobtitle",
    "database_name", "childplus_license_number", "ikn__c",
]

# Confluence notes-table columns. Order is the source of truth — every new row
# emits values in this exact order, and the bootstrap header row uses these
# strings as <th> labels. If you add/remove/reorder columns, both the header
# and the row-build logic in save_session_to_confluence() update together.
NOTES_TABLE_HEADERS = [
    "SubmittedAt",
    "PrimaryContact",
    "PrimaryAgency",
    "PrimaryDatabase",
    "EventSource",
    "Tags",
    "NoteText",
    "SessionType",
    "NoteIndex",
    "NoteCount",
    "Contacts",
    "NoteTimestamp",
    "SessionID",
]

# SharePoint list names — kept here for future migration but not actively used.
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
    if not agency.strip():
        return []
    return _search_contacts(
        [{"propertyName": "company", "operator": "CONTAINS_TOKEN", "value": agency.strip()}],
        token,
    )


def render_contact_card(props: dict) -> str:
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
# CONFLUENCE HELPERS  (active backend)
# ─────────────────────────────────────────────

def get_confluence_config() -> dict | None:
    """Pull Confluence credentials and target page IDs from Streamlit secrets.
    Returns None if any required secret is missing — caller falls back to CSV."""
    try:
        return {
            "email":  st.secrets["ATLASSIAN_EMAIL"],
            "token":  st.secrets["ATLASSIAN_API_TOKEN"],
            "domain": st.secrets["ATLASSIAN_DOMAIN"],
            "page_ids": {
                "ChildPlus": st.secrets["CHILDPLUS_NOTES_PAGE_ID"],
                "Procare":   st.secrets["PROCARE_NOTES_PAGE_ID"],
            },
        }
    except (KeyError, FileNotFoundError):
        return None


def fetch_confluence_page(cfg: dict, page_id: str) -> tuple[dict | None, str]:
    """GET page with current storage body and version number.
    Returns (page_data, error_message). page_data is None on failure."""
    url = f"https://{cfg['domain']}/wiki/rest/api/content/{page_id}"
    try:
        resp = requests.get(
            url,
            params={"expand": "body.storage,version"},
            auth=(cfg["email"], cfg["token"]),
            headers={"Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException as e:
        return None, f"Network error fetching page: {e}"
    if not resp.ok:
        return None, f"GET failed {resp.status_code}: {resp.text[:200]}"
    return resp.json(), ""


def update_confluence_page(cfg: dict, page_id: str, title: str, new_storage: str, new_version: int) -> tuple[bool, str]:
    """PUT the updated page body. Confluence requires the next version number."""
    url = f"https://{cfg['domain']}/wiki/rest/api/content/{page_id}"
    try:
        resp = requests.put(
            url,
            auth=(cfg["email"], cfg["token"]),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={
                "version": {"number": new_version},
                "title":   title,
                "type":    "page",
                "body": {
                    "storage": {
                        "value":          new_storage,
                        "representation": "storage",
                    }
                },
            },
            timeout=20,
        )
    except requests.RequestException as e:
        return False, f"Network error: {e}"
    if not resp.ok:
        return False, f"PUT failed {resp.status_code}: {resp.text[:300]}"
    return True, "ok"


def _cell_content(text: str) -> str:
    """Escape a value for safe inclusion in a Confluence storage <td>.
    Newlines become <br/> so multi-line note text renders as expected."""
    if not text:
        return ""
    return html.escape(str(text), quote=False).replace("\n", "<br/>")


def _build_header_row() -> str:
    return "<tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in NOTES_TABLE_HEADERS) + "</tr>"


def _build_data_row(values: list[str]) -> str:
    return "<tr>" + "".join(f"<td>{_cell_content(v)}</td>" for v in values) + "</tr>"


def _append_rows_to_storage(storage: str, new_rows_html: str) -> str:
    """Insert new <tr> elements at the end of the page's existing table.
    If the page has no table yet (first save), bootstrap one with headers + rows.
    Assumes there is at most one notes table per page — true for our case since
    these pages are dedicated to this app."""
    if "</tbody>" in storage:
        idx = storage.rfind("</tbody>")
        return storage[:idx] + new_rows_html + storage[idx:]
    else:
        return storage + (
            "<table>"
            "<tbody>"
            f"{_build_header_row()}"
            f"{new_rows_html}"
            "</tbody>"
            "</table>"
        )


def save_session_to_confluence(session_data: dict) -> tuple[bool, str]:
    """Append a row per note to the destination's Confluence page table."""
    cfg = get_confluence_config()
    if not cfg:
        return False, "confluence_not_configured"

    dest = session_data["destination"]
    page_id = cfg["page_ids"].get(dest)
    if not page_id:
        return False, f"No Confluence page configured for {dest}"

    page, err = fetch_confluence_page(cfg, page_id)
    if not page:
        return False, err or "Could not fetch Confluence page"

    current_storage = page.get("body", {}).get("storage", {}).get("value", "")
    current_version = page.get("version", {}).get("number", 1)
    title           = page.get("title", "")

    notes         = session_data["notes"]
    contacts_blob = format_contacts_blob(session_data["contacts"])
    primary       = session_data["contacts"][0] if session_data["contacts"] else {"name": "", "agency": "", "database": ""}

    rows_html = ""
    for idx, note in enumerate(notes, start=1):
        row_values = [
            session_data["submitted_at"],
            primary.get("name", ""),
            primary.get("agency", ""),
            primary.get("database", ""),
            session_data["event_source"],
            ", ".join(session_data["tags"]),
            note["text"],
            session_data["session_type"],
            str(idx),
            str(len(notes)),
            contacts_blob,
            note["timestamp"],
            session_data["session_id"],
        ]
        rows_html += _build_data_row(row_values)

    new_storage = _append_rows_to_storage(current_storage, rows_html)
    new_version = current_version + 1

    success, message = update_confluence_page(cfg, page_id, title, new_storage, new_version)
    if success:
        return True, f"Saved {len(notes)} note(s) to Confluence"
    return False, message


# ─────────────────────────────────────────────
# SHAREPOINT HELPERS  (DEFERRED — kept for future migration)
# ─────────────────────────────────────────────
# Confluence is the active backend during the SharePoint approval period.
# Once Azure AD app registration is approved by IT, switch the call site in
# the Save handler from save_session_to_confluence() to save_session_notes_sharepoint()
# and re-introduce the get_sharepoint_config() check at app startup.

def get_sharepoint_config() -> dict | None:
    try:
        return {
            "tenant_id":           st.secrets["TENANT_ID"],
            "client_id":           st.secrets["CLIENT_ID"],
            "client_secret":       st.secrets["CLIENT_SECRET"],
            "hostname":            st.secrets["SHAREPOINT_HOSTNAME"],
            "procare_site_path":   st.secrets["PROCARE_SITE_PATH"],
            "childplus_site_path": st.secrets["CHILDPLUS_SITE_PATH"],
            "procare_notes_list":  st.secrets["PROCARE_NOTES_LIST"],
            "childplus_notes_list":st.secrets["CHILDPLUS_NOTES_LIST"],
            "procare_tags_list":   st.secrets["PROCARE_TAGS_LIST"],
            "childplus_tags_list": st.secrets["CHILDPLUS_TAGS_LIST"],
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


# (SharePoint list-creation, tag fetch/save, and notes-save helpers retained
# in the previous file revision — removed here for brevity since none are
# called in the active code path. Restore from git history when needed.)


# ─────────────────────────────────────────────
# CONTACT BLOB FORMATTER (used by Confluence rows + CSV fallback)
# ─────────────────────────────────────────────

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
# TAG SIMILARITY  (kept for future restore — not used in stopgap)
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
  .note-box { background:#f8f9fa; border:1px solid #dee2e6; border-radius:10px; padding:14px; margin-bottom:10px; }
  .contact-box { background:#ffffff; border:1px solid #dee2e6; border-radius:10px; padding:14px; margin-bottom:10px; }
  .hs-panel { background:#f0f4ff; border:1px solid #c7d7ff; border-radius:10px; padding:14px; margin-top:8px; }
  .ticket-meta { font-size:12px; color:#6c757d; }
  .success-banner { background:#d1e7dd; color:#0f5132; padding:16px; border-radius:10px; text-align:center; font-weight:600; }
  .fallback-banner { background:#fff3cd; color:#664d03; padding:12px; border-radius:8px; font-size:13px; }
  .mode-label { font-size:12px; color:#6c757d; text-transform:uppercase; letter-spacing:0.05em; font-weight:600; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# PASSWORD GATE
# Runs before any other UI. Once authenticated for the session, never re-prompts
# until the browser tab is closed. Fails closed if APP_PASSWORD secret is missing.
# ─────────────────────────────────────────────

def show_password_gate():
    if st.session_state.get("password_correct"):
        return
    try:
        expected = st.secrets["APP_PASSWORD"]
    except (KeyError, FileNotFoundError):
        st.error("⚠️ APP_PASSWORD is not configured in Streamlit secrets. Contact the admin.")
        st.stop()

    st.title("🎤 Customer Notes")
    st.caption("This app is password-protected. Please enter the access password to continue.")
    pw = st.text_input("Password", type="password", label_visibility="collapsed", key="pw_input")

    if pw:
        if pw == expected:
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()

show_password_gate()


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
        "submitted":       False,
        "last_entry":      None,
        # Solo-mode contact fields — single source of truth
        "solo_name":       "",
        "solo_agency":     "",
        "solo_role":       "",
        "solo_database":   "",
        # Audio recording state
        "pending_transcript": None,
        "recorder_counter":   0,
        # Tag input (simplified stopgap — see TODO in the Tags section)
        "new_tag_input":   "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()


def reset_all():
    """Reset everything except the password lock — Sebrina shouldn't have to re-enter
    the password just because she finished one capture and is starting another."""
    keep_authenticated = st.session_state.get("password_correct", False)
    keys = list(st.session_state.keys())
    for k in keys:
        del st.session_state[k]
    init_state()
    if keep_authenticated:
        st.session_state.password_correct = True


def build_solo_contact() -> dict:
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
    text = (transcript or "").strip()
    if not text:
        return

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
        st.session_state.notes[empty_idx]["text"] = text
        st.session_state.notes[empty_idx]["timestamp"] = now_ts
        st.session_state[f"note_text_{empty_idx}"] = text
    else:
        new_idx = len(st.session_state.notes)
        st.session_state.notes.append({"text": text, "timestamp": now_ts})
        st.session_state[f"note_text_{new_idx}"] = text


# ─────────────────────────────────────────────
# CALLBACKS
# Streamlit forbids writing to a widget key after the widget has rendered in the
# same script run. Callbacks (on_click) execute BEFORE the next script run starts,
# so writes to widget keys (solo_name, solo_agency, etc.) happen safely there.
# ─────────────────────────────────────────────

def _apply_hubspot_pick(contact: dict, token: str):
    p = contact.get("properties", {})
    full_name = f"{p.get('firstname','')} {p.get('lastname','')}".strip()
    if full_name:
        st.session_state.solo_name = full_name
    if p.get("company"):
        st.session_state.solo_agency = p["company"]
    if p.get("jobtitle"):
        st.session_state.solo_role = p["jobtitle"]
    st.session_state.solo_database   = p.get("database_name") or ""
    st.session_state.solo_hs_id      = contact["id"]
    st.session_state.solo_hs_data    = contact
    st.session_state.solo_hs_tickets = get_contact_tickets(contact["id"], token)


def _clear_hubspot_pick():
    st.session_state.solo_hs_id      = None
    st.session_state.solo_hs_data    = None
    st.session_state.solo_hs_tickets = None


def _apply_manual_add():
    name = st.session_state.get("group_manual_name", "").strip()
    role = st.session_state.get("group_manual_role", "").strip()
    if not name:
        return
    st.session_state.contacts.append({
        "name":     name,
        "agency":   st.session_state.get("group_agency", ""),
        "role":     role,
        "database": "",
        "hs_id":    None,
        "hs_data":  None,
    })
    st.session_state.group_manual_name = ""
    st.session_state.group_manual_role = ""


def _add_hubspot_attendee(contact: dict, token: str):
    p = contact.get("properties", {})
    tickets = get_contact_tickets(contact["id"], token)
    st.session_state.contacts.append({
        "name":       f"{p.get('firstname','')} {p.get('lastname','')}".strip() or "(no name)",
        "agency":     p.get("company") or st.session_state.get("group_agency", ""),
        "role":       p.get("jobtitle", "") or "",
        "database":   p.get("database_name", "") or "",
        "hs_id":      contact["id"],
        "hs_data":    contact,
        "hs_tickets": tickets,
    })


def _add_tag():
    """Tag-add callback — runs before next render so we can clear the widget key."""
    candidate = st.session_state.get("new_tag_input", "").strip()
    if candidate and candidate not in st.session_state.tags:
        st.session_state.tags.append(candidate)
    st.session_state.new_tag_input = ""


# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────

st.title("🎤 Customer Notes")

# Success banner
if st.session_state.submitted:
    entry        = st.session_state.last_entry or {}
    note_count   = len(entry.get("notes", []))
    contact_count = len(entry.get("contacts", []))
    save_error   = entry.get("save_error")
    used_csv     = entry.get("fallback_csv", False)

    if used_csv:
        # Confluence didn't take it — show a softer banner so it's clear the
        # data isn't lost (download is offered below) but write didn't land.
        st.markdown(
            f'<div class="fallback-banner">⚠️ Saved locally — Confluence write failed. '
            f'{note_count} note(s) for {contact_count} contact(s) are below as CSV.</div>',
            unsafe_allow_html=True,
        )
        if save_error:
            with st.expander("Why did it fail?"):
                st.code(save_error)
    else:
        st.markdown(
            f'<div class="success-banner">✅ Saved to Confluence — {note_count} note(s) for {contact_count} contact(s)</div>',
            unsafe_allow_html=True,
        )

    if used_csv:
        csv_header = (
            '"SubmittedAt","PrimaryContact","PrimaryAgency","PrimaryDatabase",'
            '"EventSource","Tags","NoteText","SessionType","NoteIndex","NoteCount",'
            '"Contacts","NoteTimestamp","SessionID","Destination"\n'
        )
        rows = []
        contacts_blob = format_contacts_blob(entry["contacts"])
        primary       = entry["contacts"][0] if entry["contacts"] else {"name":"","agency":"","database":""}
        for i, note in enumerate(entry["notes"], 1):
            note_safe = note["text"].replace(chr(34), chr(39))
            rows.append(
                f'"{entry["submitted_at"]}","{primary["name"]}","{primary["agency"]}","{primary.get("database","")}",'
                f'"{entry["event_source"]}","{", ".join(entry["tags"])}","{note_safe}","{entry["session_type"]}",'
                f'"{i}","{len(entry["notes"])}","{contacts_blob}","{note["timestamp"]}","{entry["session_id"]}",'
                f'"{entry["destination"]}"\n'
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
                                st.button(
                                    "Clear",
                                    key=f"clr_{r['id']}",
                                    use_container_width=True,
                                    on_click=_clear_hubspot_pick,
                                )
                            else:
                                st.button(
                                    "Use this",
                                    key=f"use_{r['id']}",
                                    use_container_width=True,
                                    type="primary",
                                    on_click=_apply_hubspot_pick,
                                    args=(r, hs_token),
                                )

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
                                st.button(
                                    "+ Add",
                                    key=f"add_{r['id']}",
                                    use_container_width=True,
                                    on_click=_add_hubspot_attendee,
                                    args=(r, hs_token),
                                )

        st.divider()
        st.markdown("**+ Add someone not in HubSpot**")
    else:
        st.divider()
        st.markdown("**+ Add an attendee**")

    m1, m2, m3 = st.columns([2, 2, 1])
    with m1:
        st.text_input("Name", key="group_manual_name", label_visibility="collapsed", placeholder="Name")
    with m2:
        st.text_input("Role", key="group_manual_role", label_visibility="collapsed", placeholder="Role (optional)")
    with m3:
        st.button(
            "Add",
            use_container_width=True,
            key="manual_add",
            on_click=_apply_manual_add,
        )

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

with st.container(border=True):
    st.markdown("**🎙 Record a quote or thought**")
    st.caption("Tap to record, tap again to stop. Your voice from the device's mic — not call audio.")

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
                st.session_state.pop("transcript_review_text", None)
                st.rerun()
        with rcol2:
            if st.button("✕  Discard", use_container_width=True, key="discard_transcript"):
                st.session_state.pending_transcript = None
                st.session_state.recorder_counter += 1
                st.session_state.pop("transcript_review_text", None)
                st.rerun()

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
                    st.session_state.pop(f"note_text_{i}", None)
                    st.rerun()

if st.button("+ Add another note", use_container_width=True):
    st.session_state.notes.append({"text": "", "timestamp": datetime.now().isoformat()})
    st.rerun()


# ─────────────────────────────────────────────
# STEP 6 — TAGS
# Stopgap version: simple text input + Add. No autocomplete, no similarity check.
# TODO (post-stopgap):
#   - Restore "Pick existing tag" dropdown sourced from the destination's tag store
#   - Restore find_similar_tag() check with "did you mean X?" prompt
#   - Source of existing tags can be either:
#       (a) SharePoint Tags list (when AD approval comes through), or
#       (b) parsed from the Tags column of the Confluence notes table
#   - The `find_similar_tag()` function and TAG_SIMILARITY constant are still
#     in the file, ready to wire back up.
# ─────────────────────────────────────────────

st.divider()
st.subheader("Tags")
st.caption(f"Topics for this session — saved with the {st.session_state.destination} note.")

# Selected tag pills (X to remove)
if st.session_state.tags:
    cols = st.columns(min(len(st.session_state.tags) + 1, 6))
    for i, t in enumerate(st.session_state.tags):
        with cols[i % len(cols)]:
            if st.button(f"✕ {t}", key=f"rm_tag_{i}"):
                st.session_state.tags.remove(t)
                st.rerun()

# Add a new tag (free-text only during stopgap)
tcol1, tcol2 = st.columns([4, 1])
with tcol1:
    st.text_input(
        "Add a tag",
        key="new_tag_input",
        placeholder="Type a tag and click Add",
        label_visibility="collapsed",
    )
with tcol2:
    st.button(
        "Add tag",
        on_click=_add_tag,
        use_container_width=True,
        key="add_tag_btn",
    )


# ─────────────────────────────────────────────
# STEP 7 — SAVE
# Tries Confluence first; on any failure, falls back to CSV download so the
# user never loses what they captured.
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

        with st.spinner(f"Saving {len(valid_notes)} note(s) to Confluence…"):
            success, message = save_session_to_confluence(session_data)

        if success:
            # Clean Confluence write
            st.session_state.last_entry = session_data
            st.session_state.submitted  = True
            st.rerun()
        else:
            # Anything went wrong — guarantee data isn't lost by handing back a CSV
            session_data["fallback_csv"] = True
            session_data["save_error"]   = message
            st.session_state.last_entry  = session_data
            st.session_state.submitted   = True
            st.rerun()


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
