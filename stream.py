"""
stream.py — TermStream Edge v2.0 | Rich TUI Edition
A termusic-inspired TUI that streams audio from AWS S3 via mpv.

Dependencies (pip install):
    rich boto3 readchar requests

Usage:
    python stream.py
"""

from __future__ import annotations

import base64
import json
import os
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
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
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
MAUVE      = "#cba6f7"
MAUVE_DIM  = "#9475c4"
GREEN      = "#a6e3a1"
RED        = "#f38ba8"
SKY        = "#89dceb"
TEXT       = "#cdd6f4"
SUBTEXT    = "#a6adc8"
OVERLAY    = "#6c7086"
SURFACE2   = "#585b70"
SURFACE1   = "#45475a"
SURFACE0   = "#313244"
BASE       = "#1e1e2e"
MANTLE     = "#181825"
CRUST      = "#11111b"

# ─── BOTO3 CLIENTS ───────────────────────────────────────────────────────────
_cognito  = boto3.client("cognito-idp", region_name=REGION)
_dynamodb = boto3.resource("dynamodb",  region_name=REGION)
_s3       = boto3.client("s3",          region_name=REGION)


def _get_table():
    db_client = boto3.client("dynamodb", region_name=REGION)
    for name in db_client.list_tables().get("TableNames", []):
        if "AudioMetadataTable" in name:
            return _dynamodb.Table(name)
    return None


# ─── DATA TYPES ──────────────────────────────────────────────────────────────
@dataclass
class Track:
    song_id:   str
    track:     str
    artist:    str
    release:   str
    tenant:    str
    file_key:  str
    cover_key: str


@dataclass
class AppState:
    # Auth
    logged_in:    bool        = False
    username:     str         = ""
    artist_name:  str         = ""

    # Catalog
    catalog:      list[Track] = field(default_factory=list)

    # Selection
    selected_idx: int               = 0
    playing_idx:  Optional[int]     = None

    # Playback
    is_playing:   bool        = False
    elapsed:      float       = 0.0
    duration:     float       = 0.0
    mpv_proc:     Optional[subprocess.Popen] = None

    # Cover art
    cover_bytes:  Optional[bytes] = None
    cover_track:  Optional[int]   = None   # idx of track whose art is loaded

    # UI signals
    needs_redraw: bool        = True
    quit:         bool        = False
    logs:         list[str]   = field(default_factory=list)
    status_msg:   str         = "[SYS] KERNEL BOOT..."


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
    items = []
    try:
        resp = table.scan(FilterExpression=cond.Attr("Schema").eq("V4"))
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
    """Send a JSON command to MPV's IPC socket. Returns the response or None."""
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
        state.mpv_proc  = None
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
        [
            "mpv",
            "--no-video",
            "--msg-level=all=no",
            f"--input-ipc-server={MPV_SOCK}",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    state.mpv_proc   = proc
    state.is_playing = True
    state.elapsed    = 0.0
    state.duration   = 0.0


# ─── PROGRESS POLLER THREAD ──────────────────────────────────────────────────

def _progress_thread(state: AppState):
    """Polls MPV every 500ms for time-pos and duration."""
    while not state.quit:
        if state.is_playing and state.mpv_proc:
            if state.mpv_proc.poll() is not None:
                # MPV exited
                state.is_playing = False
                state.playing_idx = None
                state.elapsed     = 0.0
                state.needs_redraw = True
            else:
                pos = mpv_get_property("time-pos")
                dur = mpv_get_property("duration")
                if pos is not None:
                    state.elapsed      = float(pos)
                    state.needs_redraw = True
                if dur is not None:
                    state.duration = float(dur)
        time.sleep(0.5)


# ─── KEYBOARD THREAD ─────────────────────────────────────────────────────────

def _keyboard_thread(state: AppState):
    """
    Reads raw key events and mutates state.
    Runs in a daemon thread so it dies when main exits.
    """
    while not state.quit:
        try:
            key = readchar.readkey()
        except Exception:
            state.quit = True
            break

        if key in (readchar.key.ESC, "q", "Q"):
            state.quit = True
            break

        elif key == readchar.key.UP:
            if state.catalog:
                state.selected_idx = max(0, state.selected_idx - 1)
                state.needs_redraw = True

        elif key == readchar.key.DOWN:
            if state.catalog:
                state.selected_idx = min(len(state.catalog) - 1, state.selected_idx + 1)
                state.needs_redraw = True

        elif key == readchar.key.ENTER:
            if state.catalog:
                idx   = state.selected_idx
                track = state.catalog[idx]
                push_log(state, f"[SYS] LOADING: {track.track.upper()}")
                state.needs_redraw = True
                state.cover_bytes  = None
                state.cover_track  = None

                url = presign(track.tenant, track.file_key)
                if url:
                    spawn_mpv(state, url)
                    state.playing_idx = idx
                    push_log(state, f"[SYS] STREAMING: {track.track.upper()}")
                else:
                    push_log(state, "[ERR] PRESIGN FAILED — CHECK IAM/S3")
                state.needs_redraw = True

        elif key == " ":
            if state.mpv_proc and state.mpv_proc.poll() is None:
                mpv_cycle_pause()
                state.is_playing  = not state.is_playing
                state.needs_redraw = True

        elif key in ("r", "R"):
            # Reload catalog
            push_log(state, "[SYS] RELOADING CATALOG...")
            state.needs_redraw = True
            state.catalog      = fetch_catalog()
            state.selected_idx = 0
            push_log(state, f"[SYS] CATALOG READY — {len(state.catalog)} TRACKS")
            state.needs_redraw = True


# ─── COVER ART THREAD ────────────────────────────────────────────────────────

def _cover_thread(state: AppState, idx: int, tenant: str, cover_key: str):
    """Fetch cover art bytes in background and mark needs_redraw."""
    url = presign(tenant, cover_key)
    if not url:
        return
    try:
        resp = requests.get(url, timeout=8)
        if resp.ok:
            state.cover_bytes  = resp.content
            state.cover_track  = idx
            state.needs_redraw = True
    except Exception:
        pass


# ─── KITTY INJECTION ─────────────────────────────────────────────────────────

def inject_kitty(image_bytes: bytes, col: int, row: int, cols: int, rows: int):
    """
    Inject a Kitty Graphics Protocol image directly to stdout.
    col/row are 1-based terminal cell coordinates (matching ANSI escape convention).
    """
    b64       = base64.standard_b64encode(image_bytes).decode()
    chunk_sz  = 4096
    chunks    = [b64[i : i + chunk_sz] for i in range(0, len(b64), chunk_sz)]
    total     = len(chunks)

    sys.stdout.write(f"\033[{row};{col}H")

    for i, chunk in enumerate(chunks):
        more = 1 if i + 1 < total else 0
        if i == 0:
            sys.stdout.write(
                f"\033_Ga=T,f=100,c={cols},r={rows},s=auto,v=auto,q=2,m={more};{chunk}\033\\"
            )
        else:
            sys.stdout.write(f"\033_Gm={more};{chunk}\033\\")

    sys.stdout.flush()


def clear_kitty():
    sys.stdout.write("\033_Ga=d,d=a\033\\")
    sys.stdout.flush()


# ─── LAYOUT BUILDERS ─────────────────────────────────────────────────────────

def build_header(state: AppState) -> Panel:
    left  = Text("⬡  TERMSTREAM EDGE", style=f"bold {MAUVE}")
    right = Text(f"◈  {state.artist_name or 'UNAUTHENTICATED'}", style=f"{SUBTEXT}")
    row   = Table.grid(expand=True)
    row.add_column()
    row.add_column(justify="right")
    row.add_row(left, right)
    return Panel(row, style=f"on {MANTLE}", box=box.SIMPLE_HEAD, padding=(0, 1))


def build_sidebar(state: AppState) -> Panel:
    items = [
        ("[bold]◈[/bold] GLOBAL CATALOG",  True),
        ("  YOUR LIBRARY",                  False),
        ("  SETTINGS",                      False),
    ]
    t = Table.grid(padding=(0, 1))
    t.add_column()
    for label, active in items:
        style = Style(color=MAUVE, bold=True) if active else Style(color=OVERLAY)
        t.add_row(Text.from_markup(label, style=style))

    t.add_row(Text(""))
    t.add_row(Text("KEYBINDS", style=Style(color=SURFACE1, bold=True)))
    for kb, desc in [
        ("↑ ↓", "Navigate"),
        ("⏎",   "Play"),
        ("SPC", "Pause / Resume"),
        ("R",   "Reload Catalog"),
        ("Q",   "Quit"),
    ]:
        row_g = Table.grid()
        row_g.add_column(min_width=5, style=Style(color=MAUVE_DIM, bold=True))
        row_g.add_column(style=Style(color=SUBTEXT))
        row_g.add_row(kb, desc)
        t.add_row(row_g)

    return Panel(t, title=f"[{MAUVE}]LIBRARY[/{MAUVE}]", style=f"on {MANTLE}", box=box.SIMPLE_HEAD, padding=(0, 1))


def build_catalog(state: AppState, height: int) -> Panel:
    t = Table(
        show_header=True,
        header_style=Style(color=MAUVE, bold=True),
        box=box.SIMPLE,
        expand=True,
        row_styles=[Style(bgcolor=MANTLE), Style(bgcolor=BASE)],
        highlight=False,
    )
    t.add_column("#",       style=Style(color=OVERLAY), width=4, no_wrap=True)
    t.add_column("TRACK",   style=Style(color=TEXT),    ratio=4, no_wrap=True)
    t.add_column("ARTIST",  style=Style(color=SUBTEXT), ratio=3, no_wrap=True)
    t.add_column("RELEASE", style=Style(color=SURFACE2),ratio=3, no_wrap=True)

    if not state.catalog:
        t.add_row("—", "[dim]No catalog loaded. Press R to refresh.[/dim]", "", "")
    else:
        # Show a visible window around selected_idx to avoid giant blank panels
        visible_rows = max(height - 6, 5)
        half         = visible_rows // 2
        start        = max(0, state.selected_idx - half)
        end          = min(len(state.catalog), start + visible_rows)
        start        = max(0, end - visible_rows)   # adjust start if near bottom

        for raw_idx in range(start, end):
            track   = state.catalog[raw_idx]
            cursor  = "█ " if raw_idx == state.selected_idx else "  "
            playing = raw_idx == state.playing_idx

            num_style   = Style(color=OVERLAY)
            name_style  = Style(color=MAUVE,    bold=True) if raw_idx == state.selected_idx else Style(color=TEXT)
            artist_style= Style(color=SKY)      if playing else Style(color=SUBTEXT)
            play_icon   = " ▶" if playing else ""

            num_txt    = Text(f"{cursor}{raw_idx + 1}", style=num_style)
            track_txt  = Text(f"{track.track}{play_icon}", style=name_style, overflow="ellipsis")
            artist_txt = Text(track.artist,  style=artist_style, overflow="ellipsis")
            rel_txt    = Text(track.release or "—", style=Style(color=SURFACE2), overflow="ellipsis")

            t.add_row(num_txt, track_txt, artist_txt, rel_txt)

    return Panel(t, title=f"[{MAUVE}]CATALOG[/{MAUVE}]", style=f"on {BASE}", box=box.SIMPLE_HEAD, padding=(0, 1))


def build_inspector(state: AppState) -> tuple[Panel, Panel]:
    """Returns (art_panel, meta_panel)."""
    # Art placeholder — Kitty will overwrite this area post-render
    art_inner = Text("", justify="center")
    art_panel = Panel(
        Align.center(art_inner, vertical="middle"),
        style=f"on {MANTLE}",
        box=box.SIMPLE_HEAD,
        padding=(0, 0),
    )

    # Meta panel
    if state.playing_idx is not None and state.catalog:
        t = state.catalog[state.playing_idx]
        lines = [
            Text(t.track.upper(),  style=Style(color=MAUVE,  bold=True), overflow="fold"),
            Text(t.artist.upper(), style=Style(color=SKY),                overflow="fold"),
            Text(t.release or "—", style=Style(color=SURFACE2),           overflow="fold"),
            Text(""),
            Text(f"ID: {t.song_id}", style=Style(color=OVERLAY)),
        ]
    elif state.catalog and state.selected_idx < len(state.catalog):
        t = state.catalog[state.selected_idx]
        lines = [
            Text(t.track.upper(),  style=Style(color=TEXT,   bold=True), overflow="fold"),
            Text(t.artist.upper(), style=Style(color=SUBTEXT),            overflow="fold"),
            Text(t.release or "—", style=Style(color=SURFACE2),           overflow="fold"),
            Text(""),
            Text(f"ID: {t.song_id}", style=Style(color=OVERLAY)),
        ]
    else:
        lines = [Text("SELECT A TRACK", style=Style(color=OVERLAY))]

    grid = Table.grid(padding=(0, 0))
    grid.add_column()
    for l in lines:
        grid.add_row(l)

    meta_panel = Panel(
        grid,
        title=f"[{MAUVE}]INSPECTOR[/{MAUVE}]",
        style=f"on {MANTLE}",
        box=box.SIMPLE_HEAD,
        padding=(0, 1),
    )
    return art_panel, meta_panel


def build_footer(state: AppState, width: int) -> Panel:
    # Progress bar text
    elapsed = int(state.elapsed)
    dur     = int(state.duration)
    em, es  = divmod(elapsed, 60)
    dm, ds  = divmod(dur,     60)

    bar_w   = max(width - 26, 8)
    if dur > 0:
        filled = int((state.elapsed / state.duration) * bar_w)
    else:
        # Pulsing indeterminate
        pulse  = int(state.elapsed * 2) % max(bar_w, 1)
        filled = -(pulse)     # sentinel for indeterminate

    if dur > 0:
        bar_str = "█" * filled + "░" * (bar_w - filled)
        time_str = f"{em:02d}:{es:02d}/{dm:02d}:{ds:02d}"
    elif state.is_playing:
        pulse   = int(state.elapsed * 2) % max(bar_w, 1)
        bar_arr = list("░" * bar_w)
        for p in range(pulse, min(pulse + 4, bar_w)):
            bar_arr[p] = "█"
        bar_str  = "".join(bar_arr)
        time_str = f"{em:02d}:{es:02d}/??:??"
    else:
        bar_str  = "░" * bar_w
        time_str = "--:--/--:--"

    play_icon  = "⏸" if state.is_playing else "▶"
    status_col = GREEN if state.is_playing else MAUVE_DIM

    prog_line = Text()
    prog_line.append(f" {play_icon} ", style=Style(color=status_col, bold=True))
    prog_line.append(bar_str, style=Style(color=MAUVE))
    prog_line.append(f" {time_str} ", style=Style(color=SUBTEXT))

    # Last log
    log_line = Text(f"  {state.status_msg}", style=Style(color=OVERLAY))

    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_row(prog_line)
    grid.add_row(log_line)

    return Panel(grid, style=f"on {MANTLE}", box=box.SIMPLE_HEAD, padding=(0, 1))


# ─── AUTH SCREEN ─────────────────────────────────────────────────────────────

def run_auth(console: Console) -> Optional[dict]:
    """
    Inline auth: reads username and password from stdin in cooked mode,
    then calls Cognito.  Returns user dict or None.
    """
    import getpass
    console.clear()
    console.print(Panel(
        Align.center(
            Text.assemble(
                Text("\n⬡  TERMSTREAM EDGE\n", style=f"bold {MAUVE}"),
                Text("// IDENTITY VERIFICATION REQUIRED\n\n", style=OVERLAY),
            ),
            vertical="middle",
        ),
        style=f"on {MANTLE}",
        box=box.SIMPLE_HEAD,
        width=56,
    ))

    try:
        # Temporarily restore cooked mode for text input
        username = input(f"  IDENTITY   : ")
        password = getpass.getpass(f"  ACCESS_KEY : ")
    except (EOFError, KeyboardInterrupt):
        return None

    console.print(f"\n  [{OVERLAY}][SYS] AUTHENTICATING WITH COGNITO...[/{OVERLAY}]")
    try:
        info = cognito_login(username.strip(), password)
        console.print(f"  [{GREEN}][OK] AUTHENTICATED AS {info['artist_name'].upper()}[/{GREEN}]\n")
        time.sleep(0.8)
        return info
    except _cognito.exceptions.NotAuthorizedException:
        console.print(f"  [{RED}][ERR] INVALID CREDENTIALS — ACCESS DENIED[/{RED}]")
    except _cognito.exceptions.UserNotFoundException:
        console.print(f"  [{RED}][ERR] IDENTITY NOT FOUND IN VAULT[/{RED}]")
    except _cognito.exceptions.UserNotConfirmedException:
        console.print(f"  [{RED}][ERR] ACCOUNT UNCONFIRMED — CHECK EMAIL[/{RED}]")
    except NoCredentialsError:
        console.print(f"  [{RED}][ERR] NO AWS CREDENTIALS — CONFIGURE AWS CLI[/{RED}]")
    except Exception as e:
        console.print(f"  [{RED}][ERR] {str(e)[:100].upper()}[/{RED}]")

    time.sleep(1.5)
    return None


# ─── MAIN RENDER LOOP ─────────────────────────────────────────────────────────

def run_dashboard(console: Console, state: AppState):
    # Kick off background threads
    prog_t = threading.Thread(target=_progress_thread, args=(state,), daemon=True)
    prog_t.start()

    key_t  = threading.Thread(target=_keyboard_thread,  args=(state,), daemon=True)
    key_t.start()

    cover_spinner: Optional[threading.Thread] = None

    # Track when we last fetched cover art
    last_cover_idx = None

    while not state.quit:
        if not state.needs_redraw:
            time.sleep(0.016)  # ~60 fps ceiling
            continue

        state.needs_redraw = False

        # ── Determine terminal size ──────────────────────────────────────────
        term_w = shutil.get_terminal_size((120, 36)).columns
        term_h = shutil.get_terminal_size((120, 36)).lines

        # ── Trigger cover art fetch for selected/playing track ───────────────
        focus_idx = state.playing_idx if state.playing_idx is not None else state.selected_idx
        if (
            state.catalog
            and focus_idx < len(state.catalog)
            and state.cover_track != focus_idx
            and (cover_spinner is None or not cover_spinner.is_alive())
        ):
            t = state.catalog[focus_idx]
            if t.cover_key and t.cover_key != "NONE":
                cover_spinner = threading.Thread(
                    target=_cover_thread,
                    args=(state, focus_idx, t.tenant, t.cover_key),
                    daemon=True,
                )
                cover_spinner.start()
            else:
                state.cover_bytes = None
                state.cover_track = focus_idx

        # ── Build layout ─────────────────────────────────────────────────────
        layout = Layout()
        layout.split_column(
            Layout(name="header",  size=3),
            Layout(name="body",    ratio=1),
            Layout(name="footer",  size=4),
        )
        layout["body"].split_row(
            Layout(name="sidebar",   ratio=20),
            Layout(name="catalog",   ratio=55),
            Layout(name="inspector", ratio=25),
        )
        layout["inspector"].split_column(
            Layout(name="art",  ratio=60),
            Layout(name="meta", ratio=40),
        )

        art_panel, meta_panel = build_inspector(state)

        layout["header"].update(build_header(state))
        layout["sidebar"].update(build_sidebar(state))
        layout["catalog"].update(build_catalog(state, term_h))
        layout["art"].update(art_panel)
        layout["meta"].update(meta_panel)
        layout["footer"].update(build_footer(state, term_w))

        # ── Clear and render ─────────────────────────────────────────────────
        clear_kitty()
        console.clear()
        console.print(layout)

        # ── POST-RENDER KITTY INJECTION ──────────────────────────────────────
        # The inspector art panel sits at the top-right of the body row.
        # Approximate its terminal coordinates:
        #   x offset = sidebar_ratio% + catalog_ratio% of term_w = 75%
        #   y offset = header rows (3) + 1 (panel border)
        # We leave 1 cell padding on each side so Ratatui borders remain visible.
        if state.cover_bytes and state.cover_track == focus_idx:
            insp_w   = max(int(term_w * 0.25) - 2, 4)
            art_rows = max(int((term_h - 7) * 0.60) - 2, 2)
            art_col  = int(term_w * 0.75) + 2        # 1-based column
            art_row  = 4 + 1                          # header(3) + border(1) + 1-based

            inject_kitty(
                state.cover_bytes,
                col  = art_col,
                row  = art_row,
                cols = insp_w,
                rows = art_rows,
            )

    # ── Cleanup ──────────────────────────────────────────────────────────────
    kill_mpv(state)
    clear_kitty()
    console.clear()


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────

def main():
    console = Console(force_terminal=True, color_system="truecolor")

    # Auth loop — retry until success or Ctrl-C
    user_info = None
    while user_info is None:
        user_info = run_auth(console)
        if user_info is None:
            console.print(f"  [{RED}]Try again or Ctrl-C to quit.[/{RED}]\n")
            time.sleep(0.5)

    # Build initial state
    state              = AppState()
    state.logged_in    = True
    state.username     = user_info["username"]
    state.artist_name  = user_info["artist_name"]

    console.clear()
    console.print(f"  [{MAUVE}][SYS] SYNCING CATALOG FROM DYNAMODB...[/{MAUVE}]")
    state.catalog = fetch_catalog()
    push_log(state, f"[OK] CATALOG READY — {len(state.catalog)} TRACKS")
    state.needs_redraw = True

    run_dashboard(console, state)

    # Final restore
    console.print(f"\n  [{MAUVE}][SYS] TERMSTREAM EDGE OFFLINE.[/{MAUVE}]\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        clear_kitty()
        print("\n\033[?25h")   # show cursor
        sys.exit(0)