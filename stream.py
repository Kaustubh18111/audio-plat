"""
stream.py — TermStream Edge v2.0
Production Streamlit frontend for the AWS audio platform.

Stack: Streamlit · boto3 · Cognito USER_PASSWORD_AUTH · DynamoDB · S3
"""

import streamlit as st
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# ─── AWS CONFIGURATION ───────────────────────────────────────────────────────
REGION              = "ap-south-1"
COGNITO_CLIENT_ID   = "1ate091qv7ibstkvo0il3lsbrv"
BUCKET_NAME         = "audioplatformstack-audiostoragebucketd8d3b0dc-qfiv3hvchgq4"
PRESIGN_EXPIRY      = 3600   # seconds


# ─── PAGE CONFIG (must be first Streamlit call) ───────────────────────────────
st.set_page_config(
    page_title="TermStream Edge",
    page_icon="⬡",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items=None,
)


# ─── CACHED AWS CLIENTS ───────────────────────────────────────────────────────
@st.cache_resource
def _clients():
    cognito  = boto3.client("cognito-idp", region_name=REGION)
    dynamodb = boto3.resource("dynamodb",  region_name=REGION)
    s3       = boto3.client("s3",          region_name=REGION)
    return cognito, dynamodb, s3


def _cognito():  return _clients()[0]
def _dynamodb(): return _clients()[1]
def _s3():       return _clients()[2]


# ─── TABLE DISCOVERY ─────────────────────────────────────────────────────────
@st.cache_resource
def _discover_table():
    db = _dynamodb()
    client = boto3.client("dynamodb", region_name=REGION)
    try:
        for name in client.list_tables()["TableNames"]:
            if "AudioMetadataTable" in name:
                return db.Table(name)
    except Exception:
        pass
    return None


# ─── DATA LAYER ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def fetch_catalog() -> list:
    """Scan DynamoDB for Schema == V4 items. Cached 5 min to prevent thrashing."""
    import boto3.dynamodb.conditions as cond
    table = _discover_table()
    if table is None:
        return []
    try:
        resp  = table.scan(FilterExpression=cond.Attr("Schema").eq("V4"))
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp   = table.scan(
                FilterExpression=cond.Attr("Schema").eq("V4"),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items += resp.get("Items", [])

        return [
            {
                "song_id":   item.get("SongID",      ""),
                "track":     item.get("TrackName",   "Unknown Track"),
                "artist":    item.get("Artist",      "Unknown Artist"),
                "release":   item.get("ReleaseName", ""),
                "tenant":    item.get("TenantID",    ""),
                "file_key":  item.get("FileName",    ""),
                "cover_key": item.get("CoverKey",    "NONE"),
            }
            for item in items
        ]
    except Exception as e:
        st.sidebar.error(f"[ERR] CATALOG: {e}")
        return []


@st.cache_data(ttl=3000, show_spinner=False)
def presign(tenant: str, key: str) -> str | None:
    """Generate a presigned S3 URL. Returns None on failure or missing key."""
    if not key or key in ("NONE", ""):
        return None
    try:
        return _s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_NAME, "Key": f"{tenant}/{key}"},
            ExpiresIn=PRESIGN_EXPIRY,
        )
    except (ClientError, NoCredentialsError):
        return None


# ─── AUTH LOGIC ───────────────────────────────────────────────────────────────
def cognito_login(username: str, password: str) -> dict:
    """
    Run Cognito USER_PASSWORD_AUTH.  Raises ClientError on failure.
    Returns {username, artist_name, role, id_token}.
    """
    cognito = _cognito()
    resp    = cognito.initiate_auth(
        ClientId=COGNITO_CLIENT_ID,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    id_token = resp["AuthenticationResult"]["IdToken"]

    # Fetch profile for display name / role
    artist_name, role = username, "ListenerProfile"
    table = _discover_table()
    if table:
        try:
            profile = table.get_item(Key={"TenantID": username, "SongID": "PROFILE_DATA"})
            if "Item" in profile:
                artist_name = profile["Item"].get("ArtistName", username)
                role        = profile["Item"].get("Schema",     "ListenerProfile")
        except Exception:
            pass

    return {"username": username, "artist_name": artist_name, "role": role, "id_token": id_token}


# ─── CATPPUCCIN MOCHA CSS ─────────────────────────────────────────────────────
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700&display=swap');

*, *::before, *::after {
    font-family: 'JetBrains Mono', 'Courier New', monospace !important;
    border-radius: 0px !important;
}

html, body,
[data-testid="stAppViewContainer"],
[data-testid="stMain"] {
    background-color: #1e1e2e !important;
    color: #cdd6f4 !important;
}

[data-testid="stSidebar"],
[data-testid="stSidebarContent"] {
    background-color: #181825 !important;
    border-right: 1px solid #313244 !important;
}

#MainMenu, footer, header { visibility: hidden !important; }

/* Inputs */
input, textarea,
[data-baseweb="input"] input,
[data-baseweb="textarea"] textarea {
    background-color: #181825 !important;
    color: #cdd6f4 !important;
    border: 1px solid #45475a !important;
}
input::placeholder, textarea::placeholder { color: #585b70 !important; }
input:focus, textarea:focus {
    border-color: #cba6f7 !important;
    box-shadow: 0 0 0 1px #cba6f7 !important;
    outline: none !important;
}

label, [data-testid="stFormLabel"] {
    color: #a6adc8 !important;
    font-size: 0.72rem !important;
    letter-spacing: 0.12em !important;
    text-transform: uppercase !important;
}

/* Primary button */
button[kind="primary"], [data-testid="stBaseButton-primary"] {
    background-color: #cba6f7 !important;
    color: #1e1e2e !important;
    border: none !important;
    font-weight: 700 !important;
    letter-spacing: 0.14em !important;
    text-transform: uppercase !important;
    transition: background-color 0.1s !important;
}
button[kind="primary"]:hover { background-color: #b4befe !important; }

/* Secondary button */
button[kind="secondary"], [data-testid="stBaseButton-secondary"] {
    background-color: #313244 !important;
    color: #cdd6f4 !important;
    border: 1px solid #45475a !important;
    letter-spacing: 0.08em !important;
}
button[kind="secondary"]:hover { border-color: #cba6f7 !important; }

h1, h2, h3 { color: #cba6f7 !important; }
h4, h5, h6 { color: #cdd6f4 !important; }
hr         { border-color: #313244 !important; }

audio {
    width: 100% !important;
    background-color: #181825 !important;
    border: 1px solid #313244 !important;
    height: 36px !important;
}

[data-testid="stAlert"] {
    background-color: #181825 !important;
    border-left: 3px solid #f38ba8 !important;
    color: #cdd6f4 !important;
}
[data-testid="stAlert"][data-type="success"] { border-left-color: #a6e3a1 !important; }
[data-testid="stAlert"][data-type="info"]    { border-left-color: #89dceb !important; }

::-webkit-scrollbar       { width: 4px; background: #181825; }
::-webkit-scrollbar-thumb { background: #45475a; }

.track-name {
    color: #cba6f7;
    font-size: 0.82rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin: 0.5rem 0 0.1rem;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.track-artist  { color: #a6adc8; font-size: 0.7rem; letter-spacing: 0.04em; text-transform: uppercase; margin-bottom: 0.15rem; }
.track-release { color: #585b70; font-size: 0.62rem; text-transform: uppercase; margin-bottom: 0.4rem; }

.id-name    { color: #cba6f7; font-size: 0.9rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; }
.id-version { color: #45475a; font-size: 0.62rem; letter-spacing: 0.1em; margin-bottom: 0.75rem; }

.syslog     { font-size: 0.64rem; color: #585b70; line-height: 1.65; }
.syslog .ok { color: #a6e3a1; }
.syslog .er { color: #f38ba8; }
.syslog .in { color: #89dceb; }

.section-bar {
    background: #181825;
    border-left: 3px solid #cba6f7;
    padding: 0.35rem 0.75rem;
    font-size: 0.68rem;
    letter-spacing: 0.2em;
    color: #cba6f7;
    font-weight: 700;
    text-transform: uppercase;
    margin-bottom: 1.25rem;
}

.cover-placeholder {
    width: 100%; aspect-ratio: 1/1;
    background: #181825;
    border: 1px solid #313244;
    display: flex; align-items: center; justify-content: center;
    color: #313244; font-size: 2.5rem;
}

.auth-logo {
    color: #cba6f7;
    font-size: 1.05rem;
    font-weight: 700;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    text-align: center;
    margin-bottom: 0.2rem;
}
.auth-sub {
    color: #45475a;
    font-size: 0.62rem;
    letter-spacing: 0.16em;
    text-align: center;
    margin-bottom: 1.5rem;
    text-transform: uppercase;
}
</style>
"""


# ─── AUTH SCREEN ──────────────────────────────────────────────────────────────
def render_auth():
    st.markdown(CSS, unsafe_allow_html=True)

    _, center, _ = st.columns([1, 1.4, 1])
    with center:
        st.markdown("""
        <div style="background:#181825;border:1px solid #313244;padding:2rem 2rem 1.5rem;">
            <div class="auth-logo">⬡ &nbsp;TERMSTREAM EDGE</div>
            <div class="auth-sub">// IDENTITY VERIFICATION REQUIRED</div>
        </div>
        """, unsafe_allow_html=True)

        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("IDENTITY", placeholder="username or email address")
            password = st.text_input("ACCESS_KEY", type="password", placeholder="••••••••••")
            submit   = st.form_submit_button("◈  AUTHENTICATE", type="primary", use_container_width=True)

        if submit:
            if not username.strip() or not password:
                st.error("[ERR] IDENTITY AND ACCESS_KEY ARE REQUIRED")
                return

            with st.spinner("[SYS] HANDSHAKE WITH COGNITO VAULT..."):
                try:
                    info = cognito_login(username.strip(), password)
                    st.session_state.update({
                        "is_authenticated": True,
                        "user_info":        info,
                        "system_logs": [
                            ("ok", "COGNITO TOKEN ACTIVE"),
                            ("ok", "S3 VAULT CONNECTED"),
                            ("in", f"IDENTITY: {info['artist_name'].upper()}"),
                            ("in", f"ROLE: {info['role'].upper()}"),
                            ("ok", "CATALOG STREAM READY"),
                        ],
                    })
                    st.rerun()

                except _cognito().exceptions.NotAuthorizedException:
                    st.error("[ERR] INVALID CREDENTIALS — ACCESS DENIED")
                except _cognito().exceptions.UserNotFoundException:
                    st.error("[ERR] IDENTITY NOT FOUND IN VAULT")
                except _cognito().exceptions.UserNotConfirmedException:
                    st.error("[ERR] ACCOUNT UNCONFIRMED — CHECK REGISTRATION EMAIL")
                except ClientError as e:
                    code = e.response["Error"]["Code"]
                    st.error(f"[ERR] AWS CLIENT ERROR: {code}")
                except NoCredentialsError:
                    st.error("[ERR] NO AWS CREDENTIALS — CONFIGURE AWS CLI OR IAM ROLE")
                except Exception as e:
                    st.error(f"[ERR] AUTH FAILURE: {str(e)[:120]}")


# ─── DASHBOARD SCREEN ─────────────────────────────────────────────────────────
def render_dashboard():
    st.markdown(CSS, unsafe_allow_html=True)

    info        = st.session_state.get("user_info", {})
    system_logs = st.session_state.get("system_logs", [])

    # ── Sidebar ─────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown(f"""
        <div class="id-name">{info.get('artist_name', 'UNKNOWN').upper()}</div>
        <div class="id-version">TERMSTREAM EDGE v2.0 &nbsp;·&nbsp; {REGION}</div>
        """, unsafe_allow_html=True)

        log_html = '<div class="syslog">'
        for lvl, msg in system_logs:
            log_html += f'<div class="{lvl}">›&nbsp;[{lvl.upper()}]&nbsp;{msg}</div>'
        log_html += "</div>"
        st.markdown(log_html, unsafe_allow_html=True)

        st.divider()

        if st.button("◄ DISCONNECT", use_container_width=True):
            for k in ("is_authenticated", "user_info", "system_logs"):
                st.session_state.pop(k, None)
            fetch_catalog.clear()
            st.rerun()

        st.markdown("""
        <div class="syslog" style="margin-top:0.75rem;">
            <div class="in">› [SYS] SEARCH — UPCOMING RELEASE</div>
            <div class="in">› [SYS] LIBRARY — UPCOMING RELEASE</div>
        </div>
        """, unsafe_allow_html=True)

    # ── Main area ────────────────────────────────────────────────────────────
    st.markdown(
        '<div class="section-bar">PROMPT: ./FETCH_TRENDING_DATA &nbsp;—&nbsp; ACTIVE_CATALOG</div>',
        unsafe_allow_html=True,
    )

    with st.spinner("[SYS] IMPORTING CATALOG FROM DYNAMODB..."):
        catalog = fetch_catalog()

    if not catalog:
        st.error("[ERR] CATALOG UNAVAILABLE — VERIFY AWS CREDENTIALS AND DYNAMODB TABLE STATE")
        return

    # Render catalog in rows of 3
    COLS = 3
    for row_start in range(0, len(catalog), COLS):
        row  = catalog[row_start : row_start + COLS]
        cols = st.columns(COLS, gap="medium")

        for col, track in zip(cols, row):
            tenant    = track["tenant"]
            file_key  = track["file_key"]
            cover_key = track["cover_key"]

            with col:
                # Cover art
                cover_url = presign(tenant, cover_key)
                if cover_url:
                    try:
                        st.image(cover_url, use_column_width=True)
                    except Exception:
                        st.markdown('<div class="cover-placeholder">♬</div>', unsafe_allow_html=True)
                else:
                    st.markdown('<div class="cover-placeholder">♬</div>', unsafe_allow_html=True)

                # Metadata
                st.markdown(f"""
                <div class="track-name">{track['track']}</div>
                <div class="track-artist">{track['artist']}</div>
                <div class="track-release">{track['release'] or '—'}</div>
                """, unsafe_allow_html=True)

                # Audio player
                if file_key:
                    audio_url = presign(tenant, file_key)
                    if audio_url:
                        st.audio(audio_url, format="audio/mpeg")
                    else:
                        st.markdown(
                            '<div class="syslog"><span class="er">› [ERR] AUDIO STREAM UNAVAILABLE</span></div>',
                            unsafe_allow_html=True,
                        )
                else:
                    st.markdown(
                        '<div class="syslog"><span class="er">› [ERR] NO FILE KEY IN CATALOG</span></div>',
                        unsafe_allow_html=True,
                    )

                st.markdown("<hr>", unsafe_allow_html=True)


# ─── SESSION STATE BOOTSTRAP ─────────────────────────────────────────────────
if "is_authenticated" not in st.session_state:
    st.session_state["is_authenticated"] = False

# ─── ROUTER ──────────────────────────────────────────────────────────────────
if st.session_state["is_authenticated"]:
    render_dashboard()
else:
    render_auth()