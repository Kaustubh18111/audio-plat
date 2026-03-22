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
    pip install rich boto3 requests pillow
"""

from __future__ import annotations

import base64
import getpass
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import termios
import tty
from dataclasses import dataclass, field
from typing import Optional

import boto3
import requests
from botocore.exceptions import ClientError, NoCredentialsError
try:
    from PIL import Image
except Exception:
    Image = None
from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
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
    last_drawn_cover: Optional[int] = None

    # Render geometry (updated each frame, read by mouse handler)
    term_w:       int = 120
    term_h:       int = 36
    last_term_w:  int = 0
    catalog_row0: int = 5     # terminal row where catalog rows start (1-based)
    sidebar_w:    int = 24    # columns wide (approx 20% of term_w)
    inspector_x:  int = 91    # column where inspector starts (approx 75%)
    sidebar_nav_rows: dict[str, int] = field(default_factory=dict)
    catalog_win_start: int = 0
    catalog_win_rows:  int = 0
    supports_mouse: bool = False

    # UI signals
    needs_redraw: bool     = True
    quit:         bool     = False
    logs:         list[str] = field(default_factory=list)
    status_msg:   str      = "[SYS] KERNEL BOOT..."

    # Cached visible entries: list of (full_catalog_idx, Track)
    _visible_cache_key: Optional[tuple] = None
    _visible_cache: list[tuple[int, Track]] = field(default_factory=list)

    # Thread lock for state mutations from input/progress/render threads
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


def _visible_entries_locked(state: AppState) -> list[tuple[int, Track]]:
    """
    Return visible entries as (full_catalog_idx, Track).
    Caller must hold state._lock.
    """
    key = (
        state.active_tab,
        state.search_query.lower(),
        state.username,
        id(state.catalog),
        len(state.catalog),
    )
    if key == state._visible_cache_key:
        return state._visible_cache

    if state.active_tab == TAB_SETTINGS:
        entries: list[tuple[int, Track]] = []
    else:
        entries = []
        q = state.search_query.lower() if state.search_query else ""
        for full_idx, track in enumerate(state.catalog):
            if state.active_tab == TAB_LIBRARY and track.tenant != state.username:
                continue
            if q and q not in track.track.lower() and q not in track.artist.lower():
                continue
            entries.append((full_idx, track))

    state._visible_cache_key = key
    state._visible_cache = entries
    return entries


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
    with state._lock:
        return [track for _, track in _visible_entries_locked(state)]


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
    with state._lock:
        proc = state.mpv_proc
        state.mpv_proc = None

    if proc:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    try:
        os.remove(MPV_SOCK)
    except OSError:
        pass
    with state._lock:
        state.is_playing = False
        state.elapsed    = 0.0
        state.duration   = 0.0


def spawn_mpv(state: AppState, url: str):
    kill_mpv(state)
    try:
        proc = subprocess.Popen(
            ["mpv", "--no-video", "--msg-level=all=no",
             f"--input-ipc-server={MPV_SOCK}", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with state._lock:
            state.mpv_proc   = proc
            state.is_playing = True
            state.elapsed    = 0.0
            state.duration   = 0.0
    except OSError as e:
        with state._lock:
            push_log(state, f"[ERR] MPV ERROR: {e}")
            state.needs_redraw = True


def play_track(state: AppState, full_catalog_idx: int):
    """Spawn MPV for the track at full_catalog_idx."""
    with state._lock:
        if not (0 <= full_catalog_idx < len(state.catalog)):
            return
        track = state.catalog[full_catalog_idx]
        push_log(state, f"[SYS] LOADING: {track.track.upper()}")
        state.needs_redraw = True

    url = presign(track.tenant, track.file_key)
    if url:
        spawn_mpv(state, url)
        with state._lock:
            state.playing_idx = full_catalog_idx
            state.cover_bytes = None
            state.cover_track = None
            push_log(state, f"[OK] STREAMING: {track.track.upper()}")
            state.needs_redraw = True
    else:
        with state._lock:
            push_log(state, "[ERR] PRESIGN FAILED — CHECK IAM/S3")
            state.needs_redraw = True


# ─── BACKGROUND THREADS ───────────────────────────────────────────────────────

def _progress_thread(state: AppState):
    while not state.quit:
        with state._lock:
            is_playing = state.is_playing
            mpv_proc = state.mpv_proc

        if is_playing and mpv_proc:
            if mpv_proc.poll() is not None:
                with state._lock:
                    state.is_playing   = False
                    state.playing_idx  = None
                    state.elapsed      = 0.0
                    state.duration     = 0.0
                    state.needs_redraw = True
            else:
                pos = mpv_get_property("time-pos")
                dur = mpv_get_property("duration")
                paused = mpv_get_property("pause")
                with state._lock:
                    changed = False
                    if isinstance(pos, (int, float)) and abs(float(pos) - state.elapsed) > 0.3:
                        state.elapsed = float(pos)
                        changed = True
                    if isinstance(dur, (int, float)) and float(dur) != state.duration:
                        state.duration = float(dur)
                        changed = True
                    if isinstance(paused, bool):
                        real_playing = not paused
                        if state.is_playing != real_playing:
                            state.is_playing = real_playing
                            changed = True
                    if changed:
                        state.needs_redraw = True
        time.sleep(0.5)


def _cover_thread(state: AppState, full_idx: int, tenant: str, cover_key: str):
    url = presign(tenant, cover_key)
    if not url:
        with state._lock:
            state.cover_bytes = None
            state.cover_track = full_idx
            state.needs_redraw = True
        return
    try:
        resp = requests.get(url, timeout=8)
        if resp.ok:
            image_bytes: Optional[bytes] = None
            if Image is not None:
                try:
                    with Image.open(io.BytesIO(resp.content)) as img:
                        out = io.BytesIO()
                        img.convert("RGBA").save(out, format="PNG")
                        image_bytes = out.getvalue()
                except Exception:
                    image_bytes = None
            with state._lock:
                state.cover_bytes  = image_bytes
                state.cover_track  = full_idx
                state.needs_redraw = True
        else:
            with state._lock:
                state.cover_bytes = None
                state.cover_track = full_idx
                state.needs_redraw = True
    except Exception:
        with state._lock:
            state.cover_bytes = None
            state.cover_track = full_idx
            state.needs_redraw = True


# ─── MOUSE TRACKING ──────────────────────────────────────────────────────────

# SGR mouse sequence pattern: ESC [ < Cb ; Cx ; Cy M  (press) or m (release)
_MOUSE_RE = re.compile(r"\033\[<(\d+);(\d+);(\d+)([Mm])")


def enable_mouse():
    sys.stdout.write("\033[?1003h\033[?1015h\033[?1006h")
    sys.stdout.flush()


def disable_mouse():
    sys.stdout.write("\033[?1003l\033[?1015l\033[?1006l")
    sys.stdout.flush()


def supports_sgr_mouse() -> bool:
    term = os.environ.get("TERM", "").lower()
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    return (
        "xterm" in term
        or "kitty" in term
        or "alacritty" in term
        or "wezterm" in term
        or "vte" in term
        or "screen" in term
        or "tmux" in term
        or "kitty" in term_program
    )


def handle_mouse_event(state: AppState, btn: int, col: int, row: int, pressed: bool):
    """
    Hit detection for mouse clicks on the rendered layout.
    - Left button (btn==0) press only.
    - Coordinates are 1-based terminal cells.
    """
    if not pressed or btn != 0:
        return

    with state._lock:
        tw = state.term_w
        sidebar_end = max(state.sidebar_w, int(tw * 0.20))
        inspector_x = state.inspector_x
        active_tab = state.active_tab
        nav_rows = dict(state.sidebar_nav_rows)
        catalog_row0 = state.catalog_row0
        win_start = state.catalog_win_start
        win_rows = state.catalog_win_rows
        vis = _visible_entries_locked(state)

    if col <= sidebar_end:
        if row == nav_rows.get(TAB_CATALOG):
            with state._lock:
                state.active_tab   = TAB_CATALOG
                state.search_mode  = False
                state.search_query = ""
                state.selected_idx = 0
                state.needs_redraw = True
        elif row == nav_rows.get(TAB_LIBRARY):
            with state._lock:
                state.active_tab   = TAB_LIBRARY
                state.search_mode  = False
                state.search_query = ""
                state.selected_idx = 0
                state.needs_redraw = True
        elif row == nav_rows.get(TAB_SETTINGS):
            with state._lock:
                state.active_tab   = TAB_SETTINGS
                state.search_mode  = False
                state.search_query = ""
                state.needs_redraw = True

    elif col < inspector_x:
        if active_tab == TAB_SETTINGS:
            return
        data_row = row - catalog_row0
        if data_row < 0:
            return

        if data_row >= win_rows:
            return
        clicked_vis_idx = win_start + data_row
        if 0 <= clicked_vis_idx < len(vis):
            full_idx, _ = vis[clicked_vis_idx]
            with state._lock:
                state.selected_idx = clicked_vis_idx
                state.needs_redraw = True
            play_track(state, full_idx)


# ─── UNIFIED INPUT THREAD ────────────────────────────────────────────────────

KEY_ESC = "__ESC__"
KEY_UP = "__UP__"
KEY_DOWN = "__DOWN__"
KEY_ENTER = "__ENTER__"
KEY_BACKSPACE = "__BACKSPACE__"
KEY_SPACE = "__SPACE__"


def _decode_stdin_events(buffer: bytearray, mouse_enabled: bool) -> list[tuple]:
    events: list[tuple] = []
    i = 0

    while i < len(buffer):
        b = buffer[i]

        if mouse_enabled and b == 0x1B and i + 2 < len(buffer) and buffer[i + 1] == ord("[") and buffer[i + 2] == ord("<"):
            j = i + 3
            while j < len(buffer) and buffer[j] not in (ord("M"), ord("m")):
                j += 1
            if j >= len(buffer):
                break
            seq = bytes(buffer[i:j + 1]).decode("utf-8", errors="ignore")
            m = _MOUSE_RE.fullmatch(seq)
            if m:
                events.append(("mouse", int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) == "M"))
            i = j + 1
            continue

        if b == 0x1B:
            if i + 2 < len(buffer) and buffer[i + 1] == ord("["):
                code = buffer[i + 2]
                if code == ord("A"):
                    events.append(("key", KEY_UP))
                    i += 3
                    continue
                if code == ord("B"):
                    events.append(("key", KEY_DOWN))
                    i += 3
                    continue
            events.append(("key", KEY_ESC))
            i += 1
            continue

        if b in (10, 13):
            events.append(("key", KEY_ENTER))
            i += 1
            continue
        if b in (8, 127):
            events.append(("key", KEY_BACKSPACE))
            i += 1
            continue
        if b == ord(" "):
            events.append(("key", KEY_SPACE))
            i += 1
            continue

        if b < 128:
            ch = chr(b)
            if ch.isprintable():
                events.append(("key", ch))
            i += 1
            continue

        decoded = None
        for n in (2, 3, 4):
            if i + n > len(buffer):
                break
            try:
                decoded = bytes(buffer[i:i + n]).decode("utf-8")
                i += n
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            break
        if decoded.isprintable():
            events.append(("key", decoded))

    if i:
        del buffer[:i]
    return events


def _normalize_indexes_locked(state: AppState):
    entries = _visible_entries_locked(state)
    if state.selected_idx >= len(entries):
        state.selected_idx = max(0, len(entries) - 1)
    if state.playing_idx is not None and not (0 <= state.playing_idx < len(state.catalog)):
        state.playing_idx = None
        state.is_playing = False
        state.elapsed = 0.0
        state.duration = 0.0
    if state.cover_track is not None and not (0 <= state.cover_track < len(state.catalog)):
        state.cover_track = None
        state.cover_bytes = None


def _handle_key_event(state: AppState, key: str):
    play_request: Optional[int] = None
    reload_request = False
    toggle_pause = False

    with state._lock:
        if state.search_mode:
            if key == KEY_ESC:
                state.search_mode  = False
                state.search_query = ""
                state.selected_idx = 0
            elif key == KEY_BACKSPACE:
                state.search_query = state.search_query[:-1]
                state.selected_idx = 0
            elif key == KEY_ENTER:
                state.search_mode  = False
            elif len(key) == 1 and key.isprintable():
                state.search_query += key
                state.selected_idx  = 0
            state.needs_redraw = True
            return

        if key in (KEY_ESC, "q", "Q"):
            state.quit = True
            return

        if key in ("/", "s", "S"):
            state.search_mode  = True
            state.search_query = ""
            state.selected_idx = 0
            state.needs_redraw = True
            return

        entries = _visible_entries_locked(state)

        if key == KEY_UP:
            if entries:
                state.selected_idx = max(0, state.selected_idx - 1)
                state.needs_redraw = True
        elif key == KEY_DOWN:
            if entries:
                state.selected_idx = min(len(entries) - 1, state.selected_idx + 1)
                state.needs_redraw = True
        elif key == KEY_ENTER:
            if state.active_tab != TAB_SETTINGS and entries and state.selected_idx < len(entries):
                play_request = entries[state.selected_idx][0]
        elif key == KEY_SPACE:
            if state.mpv_proc and state.mpv_proc.poll() is None:
                toggle_pause = True
        elif key == "1":
            state.active_tab   = TAB_CATALOG
            state.selected_idx = 0
            state.needs_redraw = True
        elif key == "2":
            state.active_tab   = TAB_LIBRARY
            state.selected_idx = 0
            state.needs_redraw = True
        elif key == "3":
            state.active_tab   = TAB_SETTINGS
            state.needs_redraw = True
        elif key in ("r", "R"):
            push_log(state, "[SYS] RELOADING CATALOG...")
            state.needs_redraw = True
            reload_request = True

    if toggle_pause:
        mpv_cycle_pause()
        paused = mpv_get_property("pause")
        with state._lock:
            if isinstance(paused, bool):
                state.is_playing = not paused
            state.needs_redraw = True

    if play_request is not None:
        play_track(state, play_request)

    if reload_request:
        refreshed = fetch_catalog()
        with state._lock:
            state.catalog = refreshed
            state.selected_idx = 0
            _normalize_indexes_locked(state)
            push_log(state, f"[OK] CATALOG READY — {len(state.catalog)} TRACKS")
            state.needs_redraw = True


def _input_thread(state: AppState):
    import select

    fd = sys.stdin.fileno()
    buffer = bytearray()

    while not state.quit:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            continue
        try:
            chunk = os.read(fd, 256)
        except Exception:
            with state._lock:
                state.quit = True
            break
        if not chunk:
            continue

        buffer.extend(chunk)
        events = _decode_stdin_events(buffer, mouse_enabled=state.supports_mouse)
        for event in events:
            if event[0] == "mouse":
                _, btn, col, row, pressed = event
                handle_mouse_event(state, btn, col, row, pressed)
            else:
                _, key = event
                _handle_key_event(state, key)


# ─── KITTY INJECTION ─────────────────────────────────────────────────────────

def inject_kitty_full(image_bytes: bytes, col: int, row: int, cols: int, rows: int, img_id: int):
    """Uploads the image to terminal GPU and displays it."""
    b64 = base64.standard_b64encode(image_bytes).decode()
    chunk_sz = 4096
    chunks = [b64[i: i + chunk_sz] for i in range(0, len(b64), chunk_sz)]
    out = sys.stdout
    out.write(f"\033[{row};{col}H")
    for i, chunk in enumerate(chunks):
        more = 1 if i + 1 < len(chunks) else 0
        if i == 0:
            # FIXED: Removed s=auto,v=auto. Added z=-1 and q=2.
            out.write(f"\033_Ga=T,i={img_id},f=100,c={cols},r={rows},z=-1,q=2,m={more};{chunk}\033\\")
        else:
            out.write(f"\033_Gm={more};{chunk}\033\\")
    out.flush()


def inject_kitty_put(col: int, row: int, cols: int, rows: int, img_id: int):
    """Restamps a previously uploaded image from terminal GPU."""
    # FIXED: Added z=-1 and q=2
    sys.stdout.write(f"\033[{row};{col}H\033_Ga=p,i={img_id},c={cols},r={rows},z=-1,q=2\033\\")
    sys.stdout.flush()


def clear_kitty():
    sys.stdout.write("\033_Ga=d,d=a\033\\")
    sys.stdout.flush()


# ─── LAYOUT BUILDERS (no bgcolor on any Panel/Style — native transparency) ───

def build_header(state: AppState) -> Panel:
    play_icon = ""
    if state.playing_idx is not None and 0 <= state.playing_idx < len(state.catalog):
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
    entries = _visible_entries_locked(state)

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

    if not entries:
        msg = "  No results." if state.search_query else "  No tracks. Press R to reload."
        t.add_row("—", msg, "", "")
        state.catalog_win_start = 0
        state.catalog_win_rows = 0
    else:
        visible_rows = max(height - 8, 4)
        half         = visible_rows // 2
        start        = max(0, state.selected_idx - half)
        end          = min(len(entries), start + visible_rows)
        start        = max(0, end - visible_rows)

        state.catalog_win_start = start
        state.catalog_win_rows = end - start

        for vi in range(start, end):
            full_idx, track = entries[vi]
            is_sel    = vi == state.selected_idx
            is_play   = full_idx == state.playing_idx

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
        entries = _visible_entries_locked(state)
        if entries and state.selected_idx < len(entries):
            focus_full = entries[state.selected_idx][0]

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
    inner_w = max(state.term_w - 4, 16)  # panel borders + horizontal padding
    # " {icon} " + " {mm:ss/mm:ss} " = 3 + 13 chars
    bar_w   = max(inner_w - 16, 8)

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
    fd = sys.stdin.fileno()
    saved_attrs = None
    try:
        saved_attrs = termios.tcgetattr(fd)
    except Exception:
        saved_attrs = None

    try:
        if saved_attrs is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved_attrs)
        username = input("  IDENTITY   : ")
        password = getpass.getpass("  ACCESS_KEY : ")
    except (EOFError, KeyboardInterrupt):
        return None
    finally:
        if saved_attrs is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved_attrs)
            except Exception:
                pass

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
    fd = sys.stdin.fileno()
    saved_attrs = None
    mouse_enabled = supports_sgr_mouse()
    state.supports_mouse = mouse_enabled

    try:
        try:
            saved_attrs = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except Exception:
            saved_attrs = None

        if mouse_enabled:
            enable_mouse()
        hide_cursor()

        threading.Thread(target=_progress_thread, args=(state,), daemon=True).start()
        threading.Thread(target=_input_thread, args=(state,), daemon=True).start()

        cover_spinner: Optional[threading.Thread] = None

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

        with Live(layout, console=console, screen=True, auto_refresh=False) as live:
            while True:
                with state._lock:
                    if state.quit:
                        break
                    needs_redraw = state.needs_redraw

                if not needs_redraw:
                    time.sleep(0.016)
                    continue

                cover_fetch_args = None
                with state._lock:
                    state.needs_redraw = False

                    sz = shutil.get_terminal_size((120, 36))
                    state.term_w = sz.columns
                    state.term_h = sz.lines

                    state.sidebar_w   = max(int(state.term_w * 0.20), 20)
                    state.inspector_x = max(int(state.term_w * 0.75), state.sidebar_w + 20)

                    header_size = 3
                    body_top = header_size + 1
                    sidebar_content_row0 = body_top + 1
                    state.sidebar_nav_rows = {
                        TAB_CATALOG: sidebar_content_row0,
                        TAB_LIBRARY: sidebar_content_row0 + 2,
                        TAB_SETTINGS: sidebar_content_row0 + 4,
                    }

                    state.catalog_row0 = body_top + 4

                    _normalize_indexes_locked(state)
                    _visible_entries_locked(state)

                    # FIXED: Fetch art for navigated track, not just playing track
                    focus_full = state.playing_idx
                    if focus_full is None and state.catalog:
                        entries = _visible_entries_locked(state)
                        if entries and state.selected_idx < len(entries):
                            focus_full = entries[state.selected_idx][0]

                    if focus_full is not None and not (0 <= focus_full < len(state.catalog)):
                        focus_full = None

                    if (
                        focus_full is not None
                        and state.cover_track != focus_full
                        and (cover_spinner is None or not cover_spinner.is_alive())
                    ):
                        t = state.catalog[focus_full]
                        if t.cover_key and t.cover_key != "NONE":
                            cover_fetch_args = (focus_full, t.tenant, t.cover_key)
                        else:
                            state.cover_bytes = None
                            state.cover_track = focus_full

                    layout["header"].update(build_header(state))
                    layout["sidebar"].update(build_sidebar(state))

                    if state.active_tab == TAB_SETTINGS:
                        state.catalog_win_start = 0
                        state.catalog_win_rows = 0
                        layout["center"].update(build_settings(state))
                    else:
                        layout["center"].update(build_catalog(state, state.term_h))

                    art_panel, meta_panel = build_inspector(state)
                    layout["art"].update(art_panel)
                    layout["meta"].update(meta_panel)
                    layout["footer"].update(build_footer(state))

                    cover_bytes = state.cover_bytes
                    cover_track = state.cover_track
                    term_w = state.term_w
                    term_h = state.term_h
                    inspector_x = state.inspector_x
                    extracted_focus = focus_full  # FIXED: Export the focus variable

                if cover_fetch_args is not None:
                    cover_spinner = threading.Thread(
                        target=_cover_thread,
                        args=(state, *cover_fetch_args),
                        daemon=True,
                    )
                    cover_spinner.start()

                live.update(layout, refresh=True)

                # FIXED: Uses extracted_focus instead of playing_idx
                if cover_bytes and cover_track == extracted_focus and extracted_focus is not None:
                    insp_w   = max(term_w - inspector_x - 1, 4)
                    art_rows = max(int((term_h - 7) * 0.60) - 1, 2)
                    img_id   = extracted_focus + 1000

                    if getattr(state, "last_term_w", 0) != term_w:
                        state.last_drawn_cover = None
                        state.last_term_w = term_w

                    if getattr(state, "last_drawn_cover", None) != extracted_focus:
                        inject_kitty_full(cover_bytes, inspector_x + 2, 5, insp_w, art_rows, img_id)
                        state.last_drawn_cover = extracted_focus
                    else:
                        inject_kitty_put(inspector_x + 2, 5, insp_w, art_rows, img_id)
    finally:
        if mouse_enabled:
            disable_mouse()
        show_cursor()
        kill_mpv(state)
        clear_kitty()
        if saved_attrs is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved_attrs)
            except Exception:
                pass
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