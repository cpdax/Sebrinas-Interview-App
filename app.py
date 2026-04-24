import streamlit as st
import streamlit.components.v1 as components
import requests
from datetime import datetime

# ─────────────────────────────────────────────
# STATIC OPTIONS (no internal info here)
# ─────────────────────────────────────────────

DESTINATION_OPTIONS = ["Procare", "ChildPlus"]
SOURCE_OPTIONS = ["Conference", "Site Visit", "Zoom", "Other"]

# ─────────────────────────────────────────────
# SHAREPOINT / GRAPH API HELPERS
# ─────────────────────────────────────────────

def get_sharepoint_config() -> dict | None:
    """Load SharePoint connection config from Streamlit secrets. Returns None if not configured."""
    try:
        return {
            "tenant_id":         st.secrets["TENANT_ID"],
            "client_id":         st.secrets["CLIENT_ID"],
            "client_secret":     st.secrets["CLIENT_SECRET"],
            "hostname":          st.secrets["SHAREPOINT_HOSTNAME"],
            "procare_site_path": st.secrets["PROCARE_SITE_PATH"],
            "childplus_site_path": st.secrets["CHILDPLUS_SITE_PATH"],
            "list_name":         st.secrets["LIST_NAME"],
        }
    except (KeyError, FileNotFoundError):
        return None


def get_graph_token(cfg: dict) -> str | None:
    """Acquire an app-only Microsoft Graph token via client credentials flow."""
    url = f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token"
    resp = requests.post(
        url,
        data={
            "grant_type":    "client_credentials",
            "client_id":     cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "scope":         "https://graph.microsoft.com/.default",
        },
        timeout=10,
    )
    if resp.ok:
        return resp.json().get("access_token")
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def get_site_id(hostname: str, site_path: str, token: str) -> str | None:
    """Resolve a SharePoint site URL to its Graph site ID."""
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{hostname}:{site_path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if resp.ok:
        return resp.json().get("id")
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def get_or_create_list(site_id: str, list_name: str, token: str) -> str | None:
    """Return the ID of an existing SharePoint list, creating it if absent."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists",
        headers=headers,
        timeout=10,
    )
    if resp.ok:
        for lst in resp.json().get("value", []):
            if lst.get("displayName") == list_name:
                return lst["id"]

    payload = {
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
    }
    create_resp = requests.post(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists",
        headers=headers,
        json=payload,
        timeout=15,
    )
    if create_resp.ok:
        return create_resp.json().get("id")
    return None


def save_to_sharepoint(form_data: dict) -> tuple[bool, str]:
    """Post one row to the destination SharePoint list. Returns (success, message)."""
    cfg = get_sharepoint_config()
    if not cfg:
        return False, "sharepoint_not_configured"

    token = get_graph_token(cfg)
    if not token:
        return False, "Could not acquire Graph token — check Azure AD credentials in secrets"

    site_path = cfg["procare_site_path"] if form_data["destination"] == "Procare" else cfg["childplus_site_path"]

    site_id = get_site_id(cfg["hostname"], site_path, token)
    if not site_id:
        return False, "Could not resolve SharePoint site"

    list_id = get_or_create_list(site_id, cfg["list_name"], token)
    if not list_id:
        return False, "Could not get or create SharePoint list"

    item_payload = {
        "fields": {
            "Title":        form_data["contact_name"] or "(no name)",
            "ContactName":  form_data["contact_name"],
            "Organization": form_data["organization"],
            "Role":         form_data["role"],
            "Destination":  form_data["destination"],
            "EventSource":  form_data["event_source"],
            "Notes":        form_data["notes"],
            "SubmittedAt":  form_data["submitted_at"],
        }
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists/{list_id}/items",
        headers=headers,
        json=item_payload,
        timeout=15,
    )
    if resp.ok:
        return True, "Saved to SharePoint"
    return False, f"Graph API error {resp.status_code}: {resp.text[:200]}"


# ─────────────────────────────────────────────
# AUDIO RECORDING COMPONENT
# ─────────────────────────────────────────────

AUDIO_HTML = """
<style>
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
  #recorder {
    display: flex; flex-direction: column; align-items: center;
    gap: 12px; padding: 16px;
    background: #f8f9fa; border-radius: 12px; border: 1px solid #dee2e6;
  }
  #recordBtn {
    width: 64px; height: 64px; border-radius: 50%; border: none; cursor: pointer;
    font-size: 28px; background: #dc3545; color: white;
    box-shadow: 0 4px 12px rgba(220,53,69,0.3);
    transition: all 0.2s;
  }
  #recordBtn.listening { background: #198754; animation: pulse 1s infinite; }
  #recordBtn:disabled { background: #adb5bd; cursor: default; }
  @keyframes pulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.1); } }
  #status { font-size: 13px; color: #6c757d; }
  #transcriptBox {
    width: 100%; box-sizing: border-box;
    padding: 10px; border-radius: 8px;
    border: 1px solid #ced4da; font-size: 14px;
    min-height: 60px; resize: vertical;
    display: none;
  }
  #copyBtn {
    padding: 6px 16px; background: #0d6efd; color: white;
    border: none; border-radius: 6px; cursor: pointer; font-size: 13px;
    display: none;
  }
  #copyBtn:hover { background: #0b5ed7; }
  #copied { font-size: 12px; color: #198754; display: none; }
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
  const btn = document.getElementById('recordBtn');
  const status = document.getElementById('status');
  const tbox = document.getElementById('transcriptBox');
  const copyBtn = document.getElementById('copyBtn');
  const copied = document.getElementById('copied');

  if (!SpeechRec) {
    status.textContent = '⚠️ Voice recording not supported in this browser.';
    btn.disabled = true;
  } else {
    const rec = new SpeechRec();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = 'en-US';
    let running = false;
    let finalText = '';

    btn.addEventListener('click', () => {
      if (!running) {
        finalText = '';
        tbox.value = '';
        rec.start();
        running = true;
        btn.textContent = '⏹';
        btn.classList.add('listening');
        status.textContent = 'Recording… tap to stop';
      } else {
        rec.stop();
      }
    });

    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) { finalText += t + ' '; }
        else { interim += t; }
      }
      tbox.value = finalText + interim;
    };

    rec.onend = () => {
      running = false;
      btn.textContent = '🎙';
      btn.classList.remove('listening');
      if (finalText.trim()) {
        status.textContent = '✅ Done — copy text and paste into notes below';
        tbox.style.display = 'block';
        copyBtn.style.display = 'inline-block';
      } else {
        status.textContent = 'No speech detected. Tap to try again.';
      }
    };

    rec.onerror = (e) => {
      status.textContent = 'Error: ' + e.error + '. Tap to retry.';
      running = false;
      btn.textContent = '🎙';
      btn.classList.remove('listening');
    };
  }

  function copyText() {
    navigator.clipboard.writeText(tbox.value).then(() => {
      copied.style.display = 'inline';
      setTimeout(() => { copied.style.display = 'none'; }, 2000);
    });
  }
</script>
"""


# ─────────────────────────────────────────────
# PAGE
# ─────────────────────────────────────────────

st.set_page_config(
    page_title="Customer Interview Notes",
    page_icon="🎤",
    layout="centered",
)

st.markdown("""
<style>
  .main > div { max-width: 640px; margin: auto; }
  label { font-weight: 600; }
  .stTextArea textarea { font-size: 15px; }
  .stButton > button {
    width: 100%; padding: 14px; font-size: 16px;
    border-radius: 10px; font-weight: 600;
  }
  .success-banner {
    background: #d1e7dd; color: #0f5132;
    padding: 16px; border-radius: 10px;
    text-align: center; font-weight: 600;
  }
  .fallback-banner {
    background: #fff3cd; color: #664d03;
    padding: 12px; border-radius: 8px; font-size: 13px;
  }
</style>
""", unsafe_allow_html=True)

st.title("🎤 Customer Notes")
st.caption("Capture a conversation — it takes 30 seconds.")

if "submitted" not in st.session_state:
    st.session_state.submitted = False
if "last_entry" not in st.session_state:
    st.session_state.last_entry = None

if st.session_state.submitted:
    st.markdown('<div class="success-banner">✅ Saved! Ready for the next one.</div>', unsafe_allow_html=True)
    st.write("")
    if st.button("Add another"):
        st.session_state.submitted = False
        st.rerun()

    if st.session_state.last_entry and st.session_state.last_entry.get("fallback_csv"):
        st.markdown('<div class="fallback-banner">⚠️ SharePoint not connected yet — download below to save locally.</div>', unsafe_allow_html=True)
        entry = st.session_state.last_entry
        csv_row = (
            f'"{ entry["contact_name"] }","{ entry["organization"] }","{ entry["role"] }",'
            f'"{ entry["destination"] }","{ entry["event_source"] }","{ entry["notes"].replace(chr(34), chr(39)) }",'
            f'"{ entry["submitted_at"] }"\n'
        )
        csv_header = '"Contact Name","Organization","Role","Destination","Event Source","Notes","Submitted At"\n'
        st.download_button(
            "⬇️ Download as CSV",
            data=csv_header + csv_row,
            file_name=f"interview_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
        )
    st.stop()

with st.form("interview_form", clear_on_submit=True):
    st.subheader("Who did you talk to?")
    contact_name = st.text_input("Name *", placeholder="First and last name")
    organization = st.text_input("Organization / Agency *", placeholder="e.g. Bright Horizons, Rockford Head Start")
    role         = st.text_input("Title / Role", placeholder="e.g. Executive Director, Program Coordinator")

    st.divider()
    st.subheader("Context")
    destination  = st.selectbox("Destination *", DESTINATION_OPTIONS, help="Routes your notes to the right SharePoint list")
    event_source = st.selectbox("Where did you meet?", SOURCE_OPTIONS)

    st.divider()
    st.subheader("Notes")
    st.markdown("**Record a quote or key point**")
    st.caption("Tap the mic, speak, then copy the transcription into the notes below.")
    components.html(AUDIO_HTML, height=220)

    notes = st.text_area(
        "Notes",
        placeholder="Paste transcription here, or type directly.\n\nWhat challenges did they mention? Any good quotes? What stood out?",
        height=180,
        label_visibility="collapsed",
    )

    submitted = st.form_submit_button("💾  Save", use_container_width=True)

if submitted:
    if not contact_name.strip() or not organization.strip():
        st.error("Name and Organization are required.")
        st.stop()

    form_data = {
        "contact_name": contact_name.strip(),
        "organization": organization.strip(),
        "role":         role.strip(),
        "destination":  destination,
        "event_source": event_source,
        "notes":        notes.strip(),
        "submitted_at": datetime.now().isoformat(),
    }

    success, message = save_to_sharepoint(form_data)

    if success:
        st.session_state.last_entry = form_data
        st.session_state.submitted = True
        st.rerun()
    elif message == "sharepoint_not_configured":
        form_data["fallback_csv"] = True
        st.session_state.last_entry = form_data
        st.session_state.submitted = True
        st.rerun()
    else:
        st.error(f"Save failed: {message}")
