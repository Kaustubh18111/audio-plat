"""
stream.py — TermStream Edge v2.1 | Rich TUI + Hotfix Sprint
termusic-inspired TUI streaming from AWS S3 via mpv.

Fixes vs v2.0:
  - Anti-flicker: cursor-reposition (\033[H) instead of console.clear()
  - No bgcolor on any Panel/Style — native terminal transparency
  - X10/SGR mouse tracking with hit-detection for sidebar + catalog
  - Sidebar routing: catalog / library / settings tabs
  - TenantID-based library filter (not ID3 tags)
  - Fuzzy search mode (/ or s key) with live filtering

Dependencies:
    pip install rich boto3 readchar requests
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import boto3
import readchar
import requests
from botocore.exceptions import ClientError, NoCredentialsError
from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

# ─── AWS CONFIG ──────────────────────────────────────────────────────────────
REGION            = "ap-south-1"
BUCKET_NAME       = "audioplatformstack-audiostoragebucketd8d3b0dc-qfiv3hvchgq4"
COGNITO_CLIENT_ID = "1ate091qv7ibstkvo0il3lsbrv"
PRESIGN_EXPIRY    = 3600
MPV_SOCK          = "/tmp/termstream_mpv.sock"

# ─── CATPPUCCIN MOCHA ────────────────────────────────────────────────────────
MAUVE     = "#cba6f7"
MAUVE_DIM = "#9475c4"
GREEN     = "#a6e3a1"
RED       = "#f38ba8"
SKY       = "#89dceb"
YELLOW    = "#f9e2af"
TEXT      = "#cdd6f4"
SUBTEXT   = "#a6adc8"
OVERLAY   = "#6c7086"
SURFACE2  = "#585b70"
SURFACE1  = "#45475a"
SURFACE0  = "#313244"

# ─── BOTO3 CLIENTS ───────────────────────────────────────────────────────────
_cognito  = boto3.client("cognito-idp", region_name=REGION)
_dynamodb = boto3.resource("dynamodb",  region_name=REGION)
_s3       = boto3.client("s3",          region_name=REGION)

_TABLE_CACHE: dict = {}


def _get_table():
    if "table" not in _TABLE_CACHE:
        db_client = boto3.client("dynamodb", region_name=REGION)
        for name in db_client.list_tables().get("TableNames", []):
            if "AudioMetadataTable" in name:
                _TABLE_CACHE["table"] = _dynamodb.Table(name)
                break
    return _TABLE_CACHE.get("table")


# ─── DATA TYPES ──────────────────────────────────────────────────────────────
@dataclass
class Track:
    song_id:   str
    track:     str
    artist:    str
    release:   str
    tenant:    str       # TenantID — the uploader's account, NOT an ID3 tag
    file_key:  str
    cover_key: str


# Sidebar tab identifiers
TAB_CATALOG  = "catalog"
TAB_LIBRARY  = "library"
TAB_SETTINGS = "settings"


@dataclass
class AppState:
    # Auth
    logged_in:   bool = False
    username:    str  = ""
    artist_name: str  = ""
    role:        str  = ""

    # Full catalog (all Schema==V4 items)
    catalog: list[Track] = field(default_factory=list)

    # Sidebar routing
    active_tab: str = TAB_CATALOG  # "catalog" | "library" | "settings"

    # Fuzzy search
    search_mode:  bool = False
    search_query: str  = ""

    # Selection (index into *visible* list)
    selected_idx: int          = 0
    playing_idx:  Optional[int] = None   # index in full catalog

    # Playback
    is_playing: bool              = False
    elapsed:    float             = 0.0
    duration:   float             = 0.0
    mpv_proc:   Optional[subprocess.Popen] = None

    # Cover art
    cover_bytes: Optional[bytes] = None
    cover_track: Optional[int]   = None   # full-catalog idx whose art is loaded

    # Render geometry (updated each frame, read by mouse handler)
    term_w:       int = 120
    term_h:       int = 36
    catalog_row0: int = 5     # terminal row where catalog rows start (1-based)
    sidebar_w:    int = 24    # columns wide (approx 20% of term_w)
    inspector_x:  int = 91    # column where inspector starts (approx 75%)

    # UI signals
    needs_redraw: bool     = True
    quit:         bool     = False
    logs:         list[str] = field(default_factory=list)
    status_msg:   str      = "[SYS] KERNEL BOOT..."

    # Thread lock for state mutations from mouse/keyboard threads
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def push_log(state: AppState, msg: str):
    state.logs.append(msg)
    if len(state.logs) > 6:
        state.logs.pop(0)
    state.status_msg = msg


def fetch_catalog() -> list[Track]:
    import boto3.dynamodb.conditions as cond
    table = _get_table()
    if not table:
        return []
    items: list = []
    try:
        resp   = table.scan(FilterExpression=cond.Attr("Schema").eq("V4"))
        items += resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp   = table.scan(
                FilterExpression=cond.Attr("Schema").eq("V4"),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items += resp.get("Items", [])
    except Exception:
        pass
    return [
        Track(
            song_id   = i.get("SongID",      ""),
            track     = i.get("TrackName",   "Unknown"),
            artist    = i.get("Artist",      "Unknown"),
            release   = i.get("ReleaseName", ""),
            tenant    = i.get("TenantID",    ""),
            file_key  = i.get("FileName",    ""),
            cover_key = i.get("CoverKey",    "NONE"),
        )
        for i in items
    ]


def visible_tracks(state: AppState) -> list[Track]:
    """
    Return the subset of catalog tracks that should appear in the center panel
    based on active_tab and search_query.

    Library tab filters by TenantID == current user (not Artist/ID3 tags).
    Search filters the active view by track name or artist (case-insensitive).
    """
    if state.active_tab == TAB_SETTINGS:
        return []

    if state.active_tab == TAB_LIBRARY:
        base = [t for t in state.catalog if t.tenant == state.username]
    else:
        base = list(state.catalog)

    if state.search_query:
        q = state.search_query.lower()
        base = [
            t for t in base
            if q in t.track.lower() or q in t.artist.lower()
        ]
    return base


def presign(tenant: str, key: str) -> Optional[str]:
    if not key or key == "NONE":
        return None
    try:
        return _s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_NAME, "Key": f"{tenant}/{key}"},
            ExpiresIn=PRESIGN_EXPIRY,
        )
    except (ClientError, NoCredentialsError):
        return None


def cognito_login(username: str, password: str) -> dict:
    resp = _cognito.initiate_auth(
        ClientId=COGNITO_CLIENT_ID,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    artist_name, role = username, "ListenerProfile"
    table = _get_table()
    if table:
        try:
            profile = table.get_item(Key={"TenantID": username, "SongID": "PROFILE_DATA"})
            if "Item" in profile:
                artist_name = profile["Item"].get("ArtistName", username)
                role        = profile["Item"].get("Schema",     "ListenerProfile")
        except Exception:
            pass
    return {"username": username, "artist_name": artist_name, "role": role}


# ─── MPV IPC ─────────────────────────────────────────────────────────────────

def _mpv_send(cmd: dict) -> Optional[dict]:
    payload = json.dumps(cmd) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(MPV_SOCK)
            sock.sendall(payload.encode())
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in chunk:
                    break
            for line in data.decode(errors="ignore").splitlines():
                try:
                    return json.loads(line)
                except Exception:
                    pass
    except Exception:
        return None


def mpv_cycle_pause():
    _mpv_send({"command": ["cycle", "pause"]})


def mpv_get_property(prop: str) -> Optional[float]:
    resp = _mpv_send({"command": ["get_property", prop]})
    if resp and resp.get("error") == "success":
        return resp.get("data")
    return None


def kill_mpv(state: AppState):
    if state.mpv_proc:
        try:
            state.mpv_proc.kill()
            state.mpv_proc.wait(timeout=2)
        except Exception:
            pass
        state.mpv_proc = None
    try:
        os.remove(MPV_SOCK)
    except FileNotFoundError:
        pass
    state.is_playing = False
    state.elapsed    = 0.0
    state.duration   = 0.0


def spawn_mpv(state: AppState, url: str):
    kill_mpv(state)
    proc = subprocess.Popen(
        ["mpv", "--no-video", "--msg-level=all=no",
         f"--input-ipc-server={MPV_SOCK}", url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    state.mpv_proc   = proc
    state.is_playing = True
    state.elapsed    = 0.0
    state.duration   = 0.0


def play_track(state: AppState, full_catalog_idx: int):
    """Spawn MPV for the track at full_catalog_idx."""
    track = state.catalog[full_catalog_idx]
    push_log(state, f"[SYS] LOADING: {track.track.upper()}")
    url = presign(track.tenant, track.file_key)
    if url:
        spawn_mpv(state, url)
        state.playing_idx = full_catalog_idx
        state.cover_bytes = None
        state.cover_track = None
        push_log(state, f"[OK] STREAMING: {track.track.upper()}")
    else:
        push_log(state, "[ERR] PRESIGN FAILED — CHECK IAM/S3")
    state.needs_redraw = True


# ─── BACKGROUND THREADS ───────────────────────────────────────────────────────

def _progress_thread(state: AppState):
    while not state.quit:
        if state.is_playing and state.mpv_proc:
            if state.mpv_proc.poll() is not None:
                state.is_playing   = False
                state.playing_idx  = None
                state.elapsed      = 0.0
                state.needs_redraw = True
            else:
                pos = mpv_get_property("time-pos")
                dur = mpv_get_property("duration")
                changed = False
                if pos is not None and abs(pos - state.elapsed) > 0.3:
                    state.elapsed = float(pos)
                    changed = True
                if dur is not None and dur != state.duration:
                    state.duration = float(dur)
                    changed = True
                if changed:
                    state.needs_redraw = True
        time.sleep(0.5)


def _cover_thread(state: AppState, full_idx: int, tenant: str, cover_key: str):
    url = presign(tenant, cover_key)
    if not url:
        return
    try:
        resp = requests.get(url, timeout=8)
        if resp.ok:
            with state._lock:
                state.cover_bytes  = resp.content
                state.cover_track  = full_idx
                state.needs_redraw = True
    except Exception:
        pass


# ─── MOUSE TRACKING ──────────────────────────────────────────────────────────

# SGR mouse sequence pattern: ESC [ < Cb ; Cx ; Cy M  (press) or m (release)
_MOUSE_RE = re.compile(r"\033\[<(\d+);(\d+);(\d+)([Mm])")


def enable_mouse():
    sys.stdout.write("\033[?1003h\033[?1015h\033[?1006h")
    sys.stdout.flush()


def disable_mouse():
    sys.stdout.write("\033[?1003l\033[?1015l\033[?1006l")
    sys.stdout.flush()


def handle_mouse_event(state: AppState, btn: int, col: int, row: int, pressed: bool):
    """
    Hit detection for mouse clicks on the rendered layout.
    - Left button (btn==0) press only.
    - Coordinates are 1-based terminal cells.
    """
    if not pressed or btn != 0:
        return

    tw = state.term_w

    # Sidebar boundary (first ~20% of terminal width)
    sidebar_end = max(state.sidebar_w, int(tw * 0.20))

    if col <= sidebar_end:
        # ── SIDEBAR HIT ──────────────────────────────────────────────────────
        # Sidebar nav rows are at fixed offsets in the sidebar panel.
        # Header takes row 1 (panel title); nav items start at row 4.
        if row == 4:
            with state._lock:
                state.active_tab   = TAB_CATALOG
                state.search_mode  = False
                state.search_query = ""
                state.selected_idx = 0
                state.needs_redraw = True
        elif row == 6:
            with state._lock:
                state.active_tab   = TAB_LIBRARY
                state.search_mode  = False
                state.search_query = ""
                state.selected_idx = 0
                state.needs_redraw = True
        elif row == 8:
            with state._lock:
                state.active_tab   = TAB_SETTINGS
                state.search_mode  = False
                state.search_query = ""
                state.needs_redraw = True

    elif col < state.inspector_x:
        # ── CATALOG GRID HIT ─────────────────────────────────────────────────
        if state.active_tab == TAB_SETTINGS:
            return
        # Catalog table body starts after header row (row 3 header, row 4 separator = first data row 5)
        # catalog_row0 is updated every render
        data_row = row - state.catalog_row0
        if data_row < 0:
            return

        vis = visible_tracks(state)
        # Account for windowing: the visible window starts at max(0, selected - half)
        visible_rows = max(state.term_h - 8, 3)
        half         = visible_rows // 2
        win_start    = max(0, state.selected_idx - half)
        win_start    = max(0, min(win_start, max(0, len(vis) - visible_rows)))

        clicked_vis_idx = win_start + data_row
        if 0 <= clicked_vis_idx < len(vis):
            clicked_track = vis[clicked_vis_idx]
            # Find this track's full_catalog index
            try:
                full_idx = state.catalog.index(clicked_track)
            except ValueError:
                full_idx = None

            with state._lock:
                state.selected_idx = clicked_vis_idx
                state.needs_redraw = True

            if full_idx is not None:
                play_track(state, full_idx)


def _mouse_input_thread(state: AppState):
    """
    Reads raw bytes from stdin looking for SGR mouse sequences.
    Runs alongside the keyboard thread.
    """
    import select
    fd = sys.stdin.fileno()
    buf = b""
    while not state.quit:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            continue
        try:
            chunk = os.read(fd, 256)
        except Exception:
            break
        buf += chunk
        decoded = buf.decode("utf-8", errors="replace")
        for m in _MOUSE_RE.finditer(decoded):
            btn     = int(m.group(1))
            col     = int(m.group(2))
            row     = int(m.group(3))
            pressed = m.group(4) == "M"
            handle_mouse_event(state, btn, col, row, pressed)
        # Trim consumed sequences
        buf = b""


# ─── KEYBOARD THREAD ─────────────────────────────────────────────────────────

def _keyboard_thread(state: AppState):
    while not state.quit:
        try:
            key = readchar.readkey()
        except Exception:
            state.quit = True
            break

        with state._lock:
            # ── SEARCH MODE ────────────────────────────────────────────────
            if state.search_mode:
                if key == readchar.key.ESC:
                    state.search_mode  = False
                    state.search_query = ""
                    state.selected_idx = 0
                elif key in (readchar.key.BACKSPACE, "\x7f"):
                    state.search_query = state.search_query[:-1]
                    state.selected_idx = 0
                elif key == readchar.key.ENTER:
                    state.search_mode  = False
                elif len(key) == 1 and key.isprintable():
                    state.search_query += key
                    state.selected_idx  = 0
                state.needs_redraw = True
                continue

            # ── NORMAL MODE ────────────────────────────────────────────────
            if key in (readchar.key.ESC, "q", "Q"):
                state.quit = True
                break

            elif key in ("/", "s", "S"):
                state.search_mode  = True
                state.search_query = ""
                state.selected_idx = 0
                state.needs_redraw = True

            elif key == readchar.key.UP:
                vis = visible_tracks(state)
                if vis:
                    state.selected_idx = max(0, state.selected_idx - 1)
                    state.needs_redraw = True

            elif key == readchar.key.DOWN:
                vis = visible_tracks(state)
                if vis:
                    state.selected_idx = min(len(vis) - 1, state.selected_idx + 1)
                    state.needs_redraw = True

            elif key == readchar.key.ENTER:
                vis = visible_tracks(state)
                if state.active_tab != TAB_SETTINGS and vis and state.selected_idx < len(vis):
                    clicked_track = vis[state.selected_idx]
                    try:
                        full_idx = state.catalog.index(clicked_track)
                        play_track(state, full_idx)
                    except ValueError:
                        pass

            elif key == " ":
                if state.mpv_proc and state.mpv_proc.poll() is None:
                    mpv_cycle_pause()
                    state.is_playing  = not state.is_playing
                    state.needs_redraw = True

            elif key in ("1",):
                state.active_tab   = TAB_CATALOG
                state.selected_idx = 0
                state.needs_redraw = True

            elif key in ("2",):
                state.active_tab   = TAB_LIBRARY
                state.selected_idx = 0
                state.needs_redraw = True

            elif key in ("3",):
                state.active_tab   = TAB_SETTINGS
                state.needs_redraw = True

            elif key in ("r", "R"):
                push_log(state, "[SYS] RELOADING CATALOG...")
                state.needs_redraw = True
                state.catalog      = fetch_catalog()
                state.selected_idx = 0
                push_log(state, f"[OK] CATALOG READY — {len(state.catalog)} TRACKS")
                state.needs_redraw = True


# ─── KITTY INJECTION ─────────────────────────────────────────────────────────

def inject_kitty(image_bytes: bytes, col: int, row: int, cols: int, rows: int):
    """
    Inject Kitty graphics directly to raw stdout AFTER rich has rendered.
    col/row are 1-based terminal coordinates.
    """
    b64      = base64.standard_b64encode(image_bytes).decode()
    chunk_sz = 4096
    chunks   = [b64[i: i + chunk_sz] for i in range(0, len(b64), chunk_sz)]
    total    = len(chunks)
    out      = sys.stdout

    out.write(f"\033[{row};{col}H")
    for i, chunk in enumerate(chunks):
        more = 1 if i + 1 < total else 0
        if i == 0:
            out.write(
                f"\033_Ga=T,f=100,c={cols},r={rows},s=auto,v=auto,q=2,m={more};{chunk}\033\\"
            )
        else:
            out.write(f"\033_Gm={more};{chunk}\033\\")
    out.flush()


def clear_kitty():
    sys.stdout.write("\033_Ga=d,d=a\033\\")
    sys.stdout.flush()


# ─── LAYOUT BUILDERS (no bgcolor on any Panel/Style — native transparency) ───

def build_header(state: AppState) -> Panel:
    play_icon = ""
    if state.playing_idx is not None and state.catalog:
        t = state.catalog[state.playing_idx]
        icon = "▶" if state.is_playing else "⏸"
        play_icon = f"  {icon} {t.track.upper()} · {t.artist.upper()}"

    row = Table.grid(expand=True)
    row.add_column()
    row.add_column(justify="right")
    row.add_row(
        Text("⬡  TERMSTREAM EDGE", style=Style(color=MAUVE, bold=True)),
        Text(f"{state.artist_name or '—'}{play_icon}", style=Style(color=SUBTEXT)),
    )
    return Panel(row, box=box.SIMPLE_HEAD, padding=(0, 1))


def build_sidebar(state: AppState) -> Panel:
    items = [
        (TAB_CATALOG,  "1", "⌂ GLOBAL CATALOG"),
        (TAB_LIBRARY,  "2", "≡ YOUR LIBRARY"),
        (TAB_SETTINGS, "3", "⚙ SETTINGS"),
    ]
    t = Table.grid(padding=(0, 1))
    t.add_column()
    for tab, key, label in items:
        active = state.active_tab == tab
        style = Style(color=MAUVE, bold=True) if active else Style(color=OVERLAY)
        prefix = "█ " if active else "  "
        t.add_row(Text(f"{prefix}{label}", style=style))
        t.add_row(Text(""))  # spacer row for mouse hit alignment

    t.add_row(Text(""))
    t.add_row(Text("KEYBINDS", style=Style(color=SURFACE1, bold=True)))
    for kb, desc in [
        ("↑↓",  "Navigate"),
        ("⏎",   "Play"),
        ("SPC", "Pause"),
        ("/ S", "Search"),
        ("R",   "Reload"),
        ("Q",   "Quit"),
    ]:
        g = Table.grid()
        g.add_column(min_width=5, style=Style(color=MAUVE_DIM, bold=True))
        g.add_column(style=Style(color=SUBTEXT))
        g.add_row(kb, desc)
        t.add_row(g)

    return Panel(t, title=f"[{MAUVE}]LIBRARY[/{MAUVE}]", box=box.SIMPLE_HEAD, padding=(0, 1))


def build_catalog(state: AppState, height: int) -> Panel:
    vis = visible_tracks(state)

    # Search mode header
    title_parts: list = [f"[{MAUVE}]"]
    if state.active_tab == TAB_CATALOG:
        title_parts = [f"[{MAUVE}]GLOBAL CATALOG[/{MAUVE}]"]
    elif state.active_tab == TAB_LIBRARY:
        title_parts = [f"[{MAUVE}]YOUR LIBRARY[/{MAUVE}]"]
    else:
        title_parts = [f"[{MAUVE}]SETTINGS[/{MAUVE}]"]

    title_str = title_parts[0] if isinstance(title_parts[0], str) else "CATALOG"
    if state.search_mode or state.search_query:
        q_display = state.search_query + ("█" if state.search_mode else "")
        title_str += f" · [{YELLOW}]SEARCH: {q_display}[/{YELLOW}]"

    t = Table(
        show_header=True,
        header_style=Style(color=MAUVE, bold=True),
        box=box.SIMPLE,
        expand=True,
        highlight=False,
    )
    t.add_column("#",       width=5,   no_wrap=True)
    t.add_column("TRACK",   ratio=4,   no_wrap=True)
    t.add_column("ARTIST",  ratio=3,   no_wrap=True)
    t.add_column("RELEASE", ratio=3,   no_wrap=True)

    if not vis:
        msg = "  No results." if state.search_query else "  No tracks. Press R to reload."
        t.add_row("—", msg, "", "")
    else:
        visible_rows = max(height - 8, 4)
        half         = visible_rows // 2
        start        = max(0, state.selected_idx - half)
        end          = min(len(vis), start + visible_rows)
        start        = max(0, end - visible_rows)

        for vi in range(start, end):
            track     = vis[vi]
            is_sel    = vi == state.selected_idx
            # Find if this track is the one currently playing
            try:
                full_idx  = state.catalog.index(track)
                is_play   = full_idx == state.playing_idx
            except ValueError:
                is_play = False

            cursor     = "█ " if is_sel else "  "
            num_style  = Style(color=OVERLAY)
            name_style = Style(color=MAUVE, bold=True) if is_sel else (Style(color=GREEN) if is_play else Style(color=TEXT))
            art_style  = Style(color=SKY) if is_play else Style(color=SUBTEXT)
            play_sfx   = " ▶" if is_play else ""

            t.add_row(
                Text(f"{cursor}{vi + 1}", style=num_style, no_wrap=True),
                Text(f"{track.track}{play_sfx}", style=name_style, overflow="ellipsis"),
                Text(track.artist, style=art_style, overflow="ellipsis"),
                Text(track.release or "—", style=Style(color=SURFACE2), overflow="ellipsis"),
            )

    return Panel(t, title=title_str, box=box.SIMPLE_HEAD, padding=(0, 1))


def build_settings(state: AppState) -> Panel:
    g = Table.grid(padding=(0, 1))
    g.add_column(min_width=16, style=Style(color=OVERLAY))
    g.add_column(style=Style(color=TEXT))

    rows = [
        ("IDENTITY",    state.artist_name or state.username),
        ("USERNAME",    state.username),
        ("ROLE",        state.role),
        ("REGION",      REGION),
        ("BUCKET",      BUCKET_NAME[:36] + "..."),
        ("COGNITO ID",  COGNITO_CLIENT_ID),
        ("VERSION",     "TermStream Edge v2.1"),
        ("CATALOG",     f"{len(state.catalog)} tracks loaded"),
    ]
    for label, val in rows:
        g.add_row(Text(label, style=Style(color=OVERLAY)), Text(val, style=Style(color=TEXT)))

    g.add_row(Text(""), Text(""))
    g.add_row(
        Text("LIBRARY FILTER", style=Style(color=OVERLAY)),
        Text("TenantID == your username (not ID3 Artist tag)", style=Style(color=SUBTEXT)),
    )

    return Panel(
        Align.left(g),
        title=f"[{MAUVE}]SETTINGS[/{MAUVE}]",
        box=box.SIMPLE_HEAD,
        padding=(1, 2),
    )


def build_inspector(state: AppState) -> tuple[Panel, Panel]:
    # Art placeholder — Kitty overlays this after render
    art = Panel(Text(""), box=box.SIMPLE_HEAD, padding=(0, 0))

    # Metadata
    focus_full = state.playing_idx
    if focus_full is None and state.catalog:
        vis = visible_tracks(state)
        if vis and state.selected_idx < len(vis):
            t = vis[state.selected_idx]
            try:
                focus_full = state.catalog.index(t)
            except ValueError:
                pass

    if focus_full is not None and focus_full < len(state.catalog):
        tr = state.catalog[focus_full]
        lines = [
            Text(tr.track.upper(),  style=Style(color=MAUVE,   bold=True), overflow="fold"),
            Text(tr.artist.upper(), style=Style(color=SKY),                 overflow="fold"),
            Text(tr.release or "—", style=Style(color=SURFACE2),            overflow="fold"),
            Text(""),
            Text(f"TenantID: {tr.tenant}", style=Style(color=OVERLAY)),
            Text(f"SongID:   {tr.song_id}", style=Style(color=OVERLAY)),
        ]
    else:
        lines = [Text("SELECT A TRACK", style=Style(color=OVERLAY))]

    g = Table.grid()
    g.add_column()
    for ln in lines:
        g.add_row(ln)

    meta = Panel(g, title=f"[{MAUVE}]INSPECTOR[/{MAUVE}]", box=box.SIMPLE_HEAD, padding=(0, 1))
    return art, meta


def build_footer(state: AppState) -> Panel:
    elapsed = int(state.elapsed)
    dur     = int(state.duration)
    em, es  = divmod(elapsed, 60)
    dm, ds  = divmod(dur,     60)
    bar_w   = max(state.term_w - 28, 8)

    if dur > 0:
        filled   = int((state.elapsed / state.duration) * bar_w)
        bar_str  = "█" * filled + "░" * (bar_w - filled)
        time_str = f"{em:02d}:{es:02d}/{dm:02d}:{ds:02d}"
    elif state.is_playing:
        pulse    = int(state.elapsed * 2) % max(bar_w, 1)
        arr      = list("░" * bar_w)
        for p in range(pulse, min(pulse + 4, bar_w)):
            arr[p] = "█"
        bar_str  = "".join(arr)
        time_str = f"{em:02d}:{es:02d}/??:??"
    else:
        bar_str  = "░" * bar_w
        time_str = "--:--/--:--"

    icons = "▶" if state.is_playing else "⏸"
    col   = GREEN if state.is_playing else SURFACE1

    prog = Text()
    prog.append(f" {icons} ", style=Style(color=col, bold=True))
    prog.append(bar_str,  style=Style(color=MAUVE))
    prog.append(f" {time_str} ", style=Style(color=SUBTEXT))

    log_line = Text(f"  {state.status_msg}", style=Style(color=OVERLAY))

    g = Table.grid(expand=True)
    g.add_column()
    g.add_row(prog)
    g.add_row(log_line)

    return Panel(g, box=box.SIMPLE_HEAD, padding=(0, 1))


# ─── AUTH ─────────────────────────────────────────────────────────────────────

def run_auth(console: Console) -> Optional[dict]:
    sys.stdout.write("\033[H\033[J")   # clear screen once for auth
    console.print(Panel(
        Align.center(
            Text.assemble(
                Text("\n⬡  TERMSTREAM EDGE\n", style=f"bold {MAUVE}"),
                Text("// IDENTITY VERIFICATION REQUIRED\n\n", style=OVERLAY),
            ),
            vertical="middle",
        ),
        box=box.SIMPLE_HEAD,
        width=58,
    ))
    try:
        username = input("  IDENTITY   : ")
        password = getpass.getpass("  ACCESS_KEY : ")
    except (EOFError, KeyboardInterrupt):
        return None

    console.print(f"\n  [{OVERLAY}][SYS] HANDSHAKE WITH COGNITO...[/{OVERLAY}]")
    try:
        info = cognito_login(username.strip(), password)
        console.print(f"  [{GREEN}][OK] AUTHENTICATED AS {info['artist_name'].upper()}[/{GREEN}]")
        time.sleep(0.6)
        return info
    except _cognito.exceptions.NotAuthorizedException:
        console.print(f"  [{RED}][ERR] INVALID CREDENTIALS — ACCESS DENIED[/{RED}]")
    except _cognito.exceptions.UserNotFoundException:
        console.print(f"  [{RED}][ERR] IDENTITY NOT FOUND[/{RED}]")
    except _cognito.exceptions.UserNotConfirmedException:
        console.print(f"  [{RED}][ERR] ACCOUNT UNCONFIRMED — CHECK EMAIL[/{RED}]")
    except NoCredentialsError:
        console.print(f"  [{RED}][ERR] NO AWS CREDENTIALS — CONFIGURE AWS CLI[/{RED}]")
    except Exception as e:
        console.print(f"  [{RED}][ERR] {str(e)[:100]}[/{RED}]")
    time.sleep(1.2)
    return None


# ─── MAIN RENDER LOOP ─────────────────────────────────────────────────────────

def run_dashboard(console: Console, state: AppState):
    enable_mouse()
    hide_cursor()

    # Start background threads
    threading.Thread(target=_progress_thread, args=(state,), daemon=True).start()
    threading.Thread(target=_keyboard_thread, args=(state,), daemon=True).start()
    threading.Thread(target=_mouse_input_thread, args=(state,), daemon=True).start()

    cover_spinner: Optional[threading.Thread] = None

    while not state.quit:
        if not state.needs_redraw:
            time.sleep(0.016)
            continue

        state.needs_redraw = False

        # ── Terminal size ───────────────────────────────────────────────────
        sz           = shutil.get_terminal_size((120, 36))
        state.term_w = sz.columns
        state.term_h = sz.lines

        # Update geometry hints for mouse hit-detection
        state.sidebar_w   = max(int(state.term_w * 0.20), 20)
        state.inspector_x = max(int(state.term_w * 0.75), state.sidebar_w + 20)
        state.catalog_row0 = 5   # rows: header(3) + panel border(1) + table header(1)

        # ── Cover art fetch ─────────────────────────────────────────────────
        focus_full = state.playing_idx
        if focus_full is None and state.catalog:
            vis = visible_tracks(state)
            if vis and state.selected_idx < len(vis):
                try:
                    focus_full = state.catalog.index(vis[state.selected_idx])
                except ValueError:
                    pass

        if (
            focus_full is not None
            and state.cover_track != focus_full
            and (cover_spinner is None or not cover_spinner.is_alive())
        ):
            t = state.catalog[focus_full]
            if t.cover_key and t.cover_key != "NONE":
                cover_spinner = threading.Thread(
                    target=_cover_thread,
                    args=(state, focus_full, t.tenant, t.cover_key),
                    daemon=True,
                )
                cover_spinner.start()
            else:
                state.cover_bytes = None
                state.cover_track = focus_full

        # ── Build layout ────────────────────────────────────────────────────
        layout = Layout()
        layout.split_column(
            Layout(name="header",  size=3),
            Layout(name="body",    ratio=1),
            Layout(name="footer",  size=4),
        )
        layout["body"].split_row(
            Layout(name="sidebar",   ratio=20),
            Layout(name="center",    ratio=55),
            Layout(name="inspector", ratio=25),
        )
        layout["inspector"].split_column(
            Layout(name="art",  ratio=60),
            Layout(name="meta", ratio=40),
        )

        layout["header"].update(build_header(state))
        layout["sidebar"].update(build_sidebar(state))

        if state.active_tab == TAB_SETTINGS:
            layout["center"].update(build_settings(state))
        else:
            layout["center"].update(build_catalog(state, state.term_h))

        art_panel, meta_panel = build_inspector(state)
        layout["art"].update(art_panel)
        layout["meta"].update(meta_panel)
        layout["footer"].update(build_footer(state))

        # ── ANTI-FLICKER RENDER: cursor to (0,0) then overwrite ─────────────
        # Do NOT call console.clear() — repositioning avoids flicker and
        # lets native terminal transparency bleed through.
        sys.stdout.write("\033[H")
        console.print(layout)

        # ── POST-RENDER KITTY INJECTION ─────────────────────────────────────
        if state.cover_bytes and state.cover_track == focus_full:
            insp_w   = max(state.term_w - state.inspector_x - 1, 4)
            art_rows = max(int((state.term_h - 7) * 0.60) - 1, 2)
            inject_kitty(
                state.cover_bytes,
                col  = state.inspector_x + 2,   # 1-based, inside panel border
                row  = 5,                        # below header(3) + border(1) + 1-based
                cols = insp_w,
                rows = art_rows,
            )

    # ── Cleanup ──────────────────────────────────────────────────────────────
    disable_mouse()
    show_cursor()
    kill_mpv(state)
    clear_kitty()
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()


def hide_cursor():
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()


def show_cursor():
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────

def main():
    console = Console(force_terminal=True, color_system="truecolor")

    user_info = None
    while user_info is None:
        user_info = run_auth(console)
        if user_info is None:
            console.print(f"  [{RED}]Try again (Ctrl-C to quit).[/{RED}]\n")
            time.sleep(0.4)

    state              = AppState()
    state.logged_in    = True
    state.username     = user_info["username"]
    state.artist_name  = user_info["artist_name"]
    state.role         = user_info.get("role", "")

    sys.stdout.write("\033[H\033[J")
    console.print(f"  [{MAUVE}][SYS] SYNCING CATALOG FROM DYNAMODB...[/{MAUVE}]")
    state.catalog = fetch_catalog()
    push_log(state, f"[OK] CATALOG READY — {len(state.catalog)} TRACKS")
    state.needs_redraw = True

    try:
        run_dashboard(console, state)
    finally:
        disable_mouse()
        show_cursor()
        kill_mpv(state)
        clear_kitty()

    console.print(f"\n  [{MAUVE}][SYS] TERMSTREAM EDGE OFFLINE.[/{MAUVE}]\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        disable_mouse()
        show_cursor()
        clear_kitty()
        print("\n")
        sys.exit(0)