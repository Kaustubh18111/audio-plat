use crossterm::{
    event::{
        DisableMouseCapture, EnableMouseCapture, Event, EventStream,
        KeyCode, MouseButton, MouseEvent, MouseEventKind,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use futures::StreamExt;
use ratatui::{
    backend::{Backend, CrosstermBackend},
    layout::{Alignment, Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Paragraph},
    Terminal,
};
use serde::Deserialize;
use std::{collections::HashMap, error::Error, io, io::Write, process::Command};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::sync::{mpsc, watch};
use base64::Engine;

// ─────────────────────────────────────────────────────────────────────────────
// BACKGROUND EVENT BUS
// ─────────────────────────────────────────────────────────────────────────────
enum BackgroundEvent {
    Log(String),
    StreamReady(String),
    StreamError(String),
    PlaybackProgress { elapsed: f64, duration: f64 },
    PlaybackPaused(bool),
    PlaybackEnded,
    CoverArtReady(Vec<u8>),
}

#[derive(Deserialize)]
struct StreamResponse {
    status: String,
    url: Option<String>,
    message: Option<String>,
}

// ─────────────────────────────────────────────────────────────────────────────
// CATPPUCCIN MOCHA PALETTE
// ─────────────────────────────────────────────────────────────────────────────
const MAUVE: Color        = Color::Rgb(203, 166, 247);
const MAUVE_LIGHT: Color  = Color::Rgb(226, 199, 255);
const GREEN: Color        = Color::Rgb(166, 227, 161);
const TEXT_ACTIVE: Color  = Color::Rgb(205, 214, 244);
const TEXT_MUTED: Color   = Color::Rgb(166, 173, 200);
const SURFACE_LOW: Color  = Color::Rgb(30,  30,  46);
const SURFACE_HIGH: Color = Color::Rgb(49,  50,  68);
const BASE: Color         = Color::Rgb(24,  24,  37);

// ─────────────────────────────────────────────────────────────────────────────
// DATA MODELS
// ─────────────────────────────────────────────────────────────────────────────
#[derive(Deserialize, Debug)]
struct AuthResponse {
    status: String,
    message: Option<String>,
    artist_name: Option<String>,
    #[allow(dead_code)]
    role: Option<String>,
}

#[derive(Deserialize, Debug, Clone)]
struct Track {
    #[allow(dead_code)]
    id: String,
    track: String,
    artist: String,
    #[allow(dead_code)]
    release: String,
    tenant: String,
    file_key: String,
    cover_key: String,
}

// ─────────────────────────────────────────────────────────────────────────────
// STATE ENUMS
// ─────────────────────────────────────────────────────────────────────────────
#[derive(PartialEq, Clone, Copy)]
enum AppState { Auth, Dashboard }

#[derive(PartialEq, Clone, Copy)]
enum AuthMode { Login, CreatorSignup, ListenerSignup }

#[derive(PartialEq, Clone, Copy)]
enum InputMode { Username, Password }

#[derive(PartialEq, Clone, Copy, Eq, Hash, Debug)]
enum ActiveTab { Home, Search, Library }

// ─────────────────────────────────────────────────────────────────────────────
// CLICKABLE REGIONS — keys live here, values are cached Rect each frame
// ─────────────────────────────────────────────────────────────────────────────
#[derive(PartialEq, Eq, Hash, Debug, Clone)]
enum ClickTarget {
    SidebarTab(ActiveTab),    // clicking a nav pill
    GridTrack(usize),         // clicking a track card
    PlayPause,                // clicking the ⏸/▶ control
}

// ─────────────────────────────────────────────────────────────────────────────
// APP STATE
// ─────────────────────────────────────────────────────────────────────────────
struct App {
    // Auth screen
    state:          AppState,
    auth_mode:      AuthMode,
    input_mode:     InputMode,
    username:       String,
    password:       String,
    system_message: String,

    // Catalog
    catalog:          Vec<Track>,
    selected_track:   usize,
    active_user_name: String,

    // Sidebar
    active_tab: ActiveTab,

    // Playback
    active_playing_track: Option<usize>,
    is_playing:           bool,
    system_logs:          Vec<String>,

    // MPV
    mpv_child:         Option<std::process::Child>,
    playback_elapsed:  f64,
    playback_duration: f64,
    bg_rx:             mpsc::Receiver<BackgroundEvent>,
    bg_tx:             mpsc::Sender<BackgroundEvent>,
    ipc_cancel:        Option<watch::Sender<bool>>,

    // Kitty cover art
    cover_art_data:    Option<Vec<u8>>,
    inspector_art_rect: Option<Rect>,
    kitty_rendered:    bool,

    // Geometry cache (rebuilt every draw())
    clickable: HashMap<ClickTarget, Rect>,
}

impl App {
    fn new() -> Self {
        let (bg_tx, bg_rx) = mpsc::channel::<BackgroundEvent>(64);
        App {
            state:          AppState::Auth,
            auth_mode:      AuthMode::Login,
            input_mode:     InputMode::Username,
            username:       String::new(),
            password:       String::new(),
            system_message: String::from("[SYS] AWAITING CREDENTIALS..."),

            catalog:          Vec::new(),
            selected_track:   0,
            active_user_name: String::new(),

            active_tab: ActiveTab::Home,

            active_playing_track: None,
            is_playing:           false,
            system_logs: vec![
                String::from("[SYS] KERNEL BOOT..."),
                String::from("[SYS] AWAITING AUTHENTICATION"),
            ],

            mpv_child:         None,
            playback_elapsed:  0.0,
            playback_duration: 0.0,
            bg_rx,
            bg_tx,
            ipc_cancel: None,

            cover_art_data:     None,
            inspector_art_rect: None,
            kitty_rendered:     false,

            clickable: HashMap::new(),
        }
    }

    fn kill_mpv(&mut self) {
        if let Some(cancel) = self.ipc_cancel.take() {
            let _ = cancel.send(true);
        }
        if let Some(ref mut child) = self.mpv_child {
            let _ = child.kill();
            let _ = child.wait();
        }
        self.mpv_child        = None;
        self.playback_elapsed = 0.0;
        self.playback_duration = 0.0;
        self.kitty_rendered   = false;
    }

    fn spawn_mpv(&mut self, url: &str) {
        self.kill_mpv();
        let _ = std::fs::remove_file("/tmp/termstream_mpv.sock");
        match Command::new("mpv")
            .arg("--no-video")
            .arg("--msg-level=all=no")
            .arg("--input-ipc-server=/tmp/termstream_mpv.sock")
            .arg(url)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
        {
            Ok(child) => {
                self.mpv_child  = Some(child);
                self.is_playing = true;
                self.push_log("[SYS] MPV DAEMON ONLINE".into());
            }
            Err(e) => {
                self.push_log(format!("[ERR] MPV SPAWN FAILED: {}", e));
                self.is_playing = false;
            }
        }
    }

    fn start_ipc_observer(&mut self) {
        if let Some(cancel) = self.ipc_cancel.take() {
            let _ = cancel.send(true);
        }

        let (cancel_tx, cancel_rx) = watch::channel(false);
        self.ipc_cancel = Some(cancel_tx);
        let tx = self.bg_tx.clone();

        tokio::spawn(async move {
            // Wait up to 6 s for the socket file
            let stream = {
                let mut conn = None;
                for _ in 0..30 {
                    match UnixStream::connect("/tmp/termstream_mpv.sock").await {
                        Ok(s) => { conn = Some(s); break; }
                        Err(_) => tokio::time::sleep(std::time::Duration::from_millis(200)).await,
                    }
                }
                conn
            };

            let stream = match stream {
                Some(s) => s,
                None => {
                    let _ = tx.send(BackgroundEvent::Log("[ERR] IPC SOCKET TIMEOUT".into())).await;
                    return;
                }
            };

            let _ = tx.send(BackgroundEvent::Log("[SYS] IPC PIPE CONNECTED".into())).await;

            let (reader, mut writer) = stream.into_split();
            let mut lines    = BufReader::new(reader).lines();
            let mut cancel   = cancel_rx;

            // Register observers — send immediately on connect
            for cmd in &[
                "{\"command\":[\"observe_property\",1,\"time-pos\"]}\n",
                "{\"command\":[\"observe_property\",2,\"duration\"]}\n",
                "{\"command\":[\"observe_property\",3,\"pause\"]}\n",
            ] {
                if writer.write_all(cmd.as_bytes()).await.is_err() {
                    let _ = tx.send(BackgroundEvent::Log("[ERR] IPC REGISTER FAILED".into())).await;
                    return;
                }
            }
            let _ = tx.send(BackgroundEvent::Log("[SYS] OBSERVERS REGISTERED".into())).await;

            let mut last_elapsed  = 0.0f64;
            let mut last_duration = 0.0f64;

            loop {
                tokio::select! {
                    line = lines.next_line() => {
                        match line {
                            Ok(Some(raw)) => {
                                if let Ok(val) = serde_json::from_str::<serde_json::Value>(&raw) {
                                    match val.get("event").and_then(|v| v.as_str()) {
                                        Some("property-change") => {
                                            let id = val.get("id").and_then(|v| v.as_u64()).unwrap_or(0);
                                            match id {
                                                1 => {
                                                    if let Some(d) = val.get("data").and_then(|v| v.as_f64()) {
                                                        last_elapsed = d;
                                                        let _ = tx.send(BackgroundEvent::PlaybackProgress {
                                                            elapsed: last_elapsed,
                                                            duration: last_duration,
                                                        }).await;
                                                    }
                                                }
                                                2 => {
                                                    if let Some(d) = val.get("data").and_then(|v| v.as_f64()) {
                                                        last_duration = d;
                                                        let _ = tx.send(BackgroundEvent::PlaybackProgress {
                                                            elapsed: last_elapsed,
                                                            duration: last_duration,
                                                        }).await;
                                                    }
                                                }
                                                3 => {
                                                    if let Some(paused) = val.get("data").and_then(|v| v.as_bool()) {
                                                        let _ = tx.send(BackgroundEvent::PlaybackPaused(paused)).await;
                                                    }
                                                }
                                                _ => {}
                                            }
                                        }
                                        Some("end-file") => {
                                            let _ = tx.send(BackgroundEvent::PlaybackEnded).await;
                                            return;
                                        }
                                        _ => {}
                                    }
                                }
                            }
                            Ok(None) | Err(_) => {
                                let _ = tx.send(BackgroundEvent::PlaybackEnded).await;
                                return;
                            }
                        }
                    }
                    _ = cancel.changed() => break,
                }
            }
        });
    }

    fn push_log(&mut self, msg: String) {
        self.system_logs.push(msg);
        if self.system_logs.len() > 5 {
            self.system_logs.remove(0);
        }
    }

    fn fetch_catalog(&mut self) {
        self.system_message = String::from("[SYS] FETCHING CATALOG...");
        match Command::new("python").arg("../backend.py").arg("catalog").output() {
            Ok(out) => {
                let stdout = String::from_utf8_lossy(&out.stdout);
                match serde_json::from_str::<Vec<Track>>(&stdout) {
                    Ok(tracks) => {
                        self.catalog = tracks;
                        self.system_message = String::from("[SYS] CATALOG SYNC COMPLETE");
                    }
                    Err(_) => self.system_message = String::from("[ERR] FAILED TO PARSE CATALOG"),
                }
            }
            Err(_) => self.system_message = String::from("[ERR] BROKER CONNECTION FAILED"),
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// KITTY GRAPHICS PROTOCOL
// ─────────────────────────────────────────────────────────────────────────────

/// Fire Kitty escape sequences to the raw terminal stdout.
/// Must be called AFTER terminal.draw() so Ratatui doesn't overwrite the image.
/// `target` is a 0-based Ratatui Rect; we convert to 1-based terminal coords.
fn render_kitty_image(bytes: &[u8], target: Rect) {
    if target.width == 0 || target.height == 0 { return; }

    let b64      = base64::engine::general_purpose::STANDARD.encode(bytes);
    let chunk_sz = 4096;
    let mut out  = io::stdout();

    // Move cursor to the art box origin (1-based terminal coords)
    let _ = write!(out, "\x1b[{};{}H", target.y + 1, target.x + 1);

    let chunks: Vec<&[u8]> = b64.as_bytes().chunks(chunk_sz).collect();
    let total = chunks.len();

    for (i, chunk) in chunks.iter().enumerate() {
        let chunk_str = std::str::from_utf8(chunk).unwrap_or("");
        let more = if i + 1 < total { 1 } else { 0 };

        if i == 0 {
            let _ = write!(
                out,
                "\x1b_Ga=T,f=100,c={},r={},s=auto,v=auto,q=2,m={};{}\x1b\\",
                target.width, target.height, more, chunk_str
            );
        } else {
            let _ = write!(out, "\x1b_Gm={};{}\x1b\\", more, chunk_str);
        }
    }

    let _ = out.flush();
}

fn clear_kitty_images() {
    let mut out = io::stdout();
    let _ = write!(out, "\x1b_Ga=d,d=a\x1b\\");
    let _ = out.flush();
}

// ─────────────────────────────────────────────────────────────────────────────
// ENTRY POINT
// ─────────────────────────────────────────────────────────────────────────────
#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let backend  = CrosstermBackend::new(stdout);
    let mut term = Terminal::new(backend)?;

    let mut app = App::new();
    let res = run_app(&mut term, &mut app).await;

    clear_kitty_images();
    disable_raw_mode()?;
    execute!(term.backend_mut(), LeaveAlternateScreen, DisableMouseCapture)?;
    term.show_cursor()?;
    if let Err(err) = res { eprintln!("{:?}", err); }
    Ok(())
}

// ─────────────────────────────────────────────────────────────────────────────
// UNIFIED ASYNC EVENT LOOP  (tokio::select! over EventStream + mpsc channel)
// ─────────────────────────────────────────────────────────────────────────────
async fn run_app<B: Backend>(terminal: &mut Terminal<B>, app: &mut App) -> io::Result<()> {
    let mut event_stream = EventStream::new();

    loop {
        // ── 1. DRAW ────────────────────────────────────────────────────────
        terminal.draw(|f| {
            f.render_widget(Block::default().style(Style::default().bg(BASE)), f.size());
            match app.state {
                AppState::Auth      => draw_auth(f, app),
                AppState::Dashboard => draw_dashboard(f, app),
            }
        })?;

        // ── 2. POST-RENDER KITTY INJECTION ────────────────────────────────
        // Fires only once per cover art load (kitty_rendered flag prevents re-firing)
        if app.state == AppState::Dashboard && !app.kitty_rendered {
            if let (Some(ref data), Some(rect)) = (&app.cover_art_data, app.inspector_art_rect) {
                render_kitty_image(data, rect);
                app.kitty_rendered = true;
            }
        }

        // ── 3. CONCURRENT EVENT WAIT ──────────────────────────────────────
        tokio::select! {
            // Background channel events (MPV progress, logs, cover art, etc.)
            Some(bg) = app.bg_rx.recv() => {
                process_bg_event(bg, app);
            }

            // Terminal input (keyboard + mouse) — non-blocking via EventStream
            maybe_event = event_stream.next() => {
                match maybe_event {
                    Some(Ok(Event::Key(key))) => {
                        if key.code == KeyCode::Esc {
                            app.kill_mpv();
                            clear_kitty_images();
                            return Ok(());
                        }
                        match app.state {
                            AppState::Auth      => handle_auth_input(key.code, app),
                            AppState::Dashboard => handle_dashboard_input(key.code, app),
                        }
                    }
                    Some(Ok(Event::Mouse(mouse))) => {
                        if app.state == AppState::Dashboard {
                            handle_mouse_input(mouse, app);
                        }
                    }
                    Some(Ok(Event::Resize(_, _))) => {
                        // Ratatui handles resize automatically on next draw
                    }
                    Some(Err(_)) | None => {
                        // Stream ended / error — exit gracefully
                        return Ok(());
                    }
                    _ => {}
                }
            }

            // Small yield so the loop never spins at 100% when nothing happens
            _ = tokio::time::sleep(std::time::Duration::from_millis(8)) => {}
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// BACKGROUND EVENT PROCESSOR
// ─────────────────────────────────────────────────────────────────────────────
fn process_bg_event(event: BackgroundEvent, app: &mut App) {
    match event {
        BackgroundEvent::Log(msg) => app.push_log(msg),

        BackgroundEvent::StreamReady(url) => {
            app.push_log("[SYS] PRE-SIGNED URL ACQUIRED".into());
            app.spawn_mpv(&url);
            app.start_ipc_observer();
        }

        BackgroundEvent::StreamError(msg) => {
            app.push_log(format!("[ERR] {}", msg));
            app.is_playing = false;
        }

        BackgroundEvent::PlaybackProgress { elapsed, duration } => {
            app.playback_elapsed  = elapsed;
            app.playback_duration = duration;
        }

        BackgroundEvent::PlaybackPaused(paused) => {
            app.is_playing = !paused;
            app.push_log(if paused { "[SYS] STREAM PAUSED".into() } else { "[SYS] STREAM RESUMED".into() });
        }

        BackgroundEvent::PlaybackEnded => {
            app.is_playing        = false;
            app.playback_elapsed  = 0.0;
            app.playback_duration = 0.0;
            app.push_log("[SYS] TRACK ENDED".into());
        }

        BackgroundEvent::CoverArtReady(data) => {
            app.push_log("[SYS] COVER ART LOADED".into());
            app.cover_art_data  = Some(data);
            app.kitty_rendered  = false; // trigger one-shot injection on next frame
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// AUTH SCREEN
// ─────────────────────────────────────────────────────────────────────────────
fn draw_auth(f: &mut ratatui::Frame, app: &App) {
    let size = f.size();

    let vert = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Percentage(25), Constraint::Length(16), Constraint::Percentage(25)])
        .split(size);

    let horiz = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(30), Constraint::Percentage(40), Constraint::Percentage(30)])
        .split(vert[1]);

    let slots = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2), // header
            Constraint::Length(4), // tabs
            Constraint::Length(3), // username
            Constraint::Length(3), // password
            Constraint::Length(4), // footer
        ])
        .split(horiz[1]);

    // Header
    f.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled("TERMINAL_OS ", Style::default().fg(MAUVE).add_modifier(Modifier::BOLD)),
            Span::styled("// AUTH", Style::default().fg(TEXT_MUTED)),
        ])).alignment(Alignment::Center),
        slots[0],
    );

    // Mode tabs
    let tab_style = |active: bool| {
        if active { Style::default().fg(MAUVE_LIGHT) } else { Style::default().fg(TEXT_MUTED) }
    };
    f.render_widget(
        Paragraph::new(vec![
            Line::from(Span::styled(
                if app.auth_mode == AuthMode::Login { "▶ [ LOGIN ]" } else { "  LOGIN" },
                tab_style(app.auth_mode == AuthMode::Login),
            )),
            Line::from(Span::styled(
                if app.auth_mode == AuthMode::CreatorSignup { "▶ [ CREATOR ]" } else { "  CREATOR" },
                tab_style(app.auth_mode == AuthMode::CreatorSignup),
            )),
            Line::from(Span::styled(
                if app.auth_mode == AuthMode::ListenerSignup { "▶ [ LISTENER ]" } else { "  LISTENER" },
                tab_style(app.auth_mode == AuthMode::ListenerSignup),
            )),
        ]).alignment(Alignment::Center),
        slots[1],
    );

    // Inputs
    let masked = "*".repeat(app.password.len());
    let u_cursor = if app.input_mode == InputMode::Username { ">>" } else { "  " };
    let p_cursor = if app.input_mode == InputMode::Password { ">>" } else { "  " };

    f.render_widget(
        Paragraph::new(vec![
            Line::from(Span::styled("IDENTITY", Style::default().fg(TEXT_MUTED))),
            Line::from(vec![
                Span::styled(u_cursor, Style::default().fg(MAUVE_LIGHT)),
                Span::styled(" ", Style::default()),
                Span::styled(&app.username, Style::default().fg(TEXT_ACTIVE)),
                if app.input_mode == InputMode::Username {
                    Span::styled("█", Style::default().fg(MAUVE))
                } else { Span::raw("") },
            ]),
        ]),
        slots[2],
    );

    f.render_widget(
        Paragraph::new(vec![
            Line::from(Span::styled("ACCESS_KEY", Style::default().fg(TEXT_MUTED))),
            Line::from(vec![
                Span::styled(p_cursor, Style::default().fg(MAUVE_LIGHT)),
                Span::styled(" ", Style::default()),
                Span::styled(masked, Style::default().fg(TEXT_ACTIVE)),
                if app.input_mode == InputMode::Password {
                    Span::styled("█", Style::default().fg(MAUVE))
                } else { Span::raw("") },
            ]),
        ]),
        slots[3],
    );

    // Footer / system message
    let sys_color = if app.system_message.starts_with("[ERR]") { Color::Red } else { TEXT_MUTED };
    f.render_widget(
        Paragraph::new(vec![
            Line::from(Span::styled(&app.system_message, Style::default().fg(sys_color))),
            Line::from(Span::styled("[ENTER] Execute  [TAB] Switch mode  [ESC] Quit", Style::default().fg(TEXT_MUTED))),
        ]).alignment(Alignment::Center),
        slots[4],
    );
}

fn handle_auth_input(key: KeyCode, app: &mut App) {
    match key {
        KeyCode::Tab => {
            app.auth_mode = match app.auth_mode {
                AuthMode::Login          => AuthMode::CreatorSignup,
                AuthMode::CreatorSignup  => AuthMode::ListenerSignup,
                AuthMode::ListenerSignup => AuthMode::Login,
            };
        }
        KeyCode::Up | KeyCode::Down => {
            app.input_mode = match app.input_mode {
                InputMode::Username => InputMode::Password,
                InputMode::Password => InputMode::Username,
            };
        }
        KeyCode::Backspace => {
            match app.input_mode {
                InputMode::Username => { app.username.pop(); }
                InputMode::Password => { app.password.pop(); }
            }
        }
        KeyCode::Char(c) => {
            match app.input_mode {
                InputMode::Username => app.username.push(c),
                InputMode::Password => app.password.push(c),
            }
        }
        KeyCode::Enter => {
            app.system_message = String::from("[SYS] AUTHENTICATING...");
            let output = Command::new("python")
                .arg("../backend.py")
                .arg("login")
                .arg(&app.username)
                .arg(&app.password)
                .output();

            match output {
                Ok(out) => {
                    let raw = String::from_utf8_lossy(&out.stdout);
                    match serde_json::from_str::<AuthResponse>(&raw) {
                        Ok(resp) if resp.status == "success" => {
                            app.active_user_name = resp.artist_name.unwrap_or_default();
                            app.state = AppState::Dashboard;
                            app.fetch_catalog();
                        }
                        Ok(resp) => {
                            app.system_message = format!(
                                "[ERR] {}",
                                resp.message.unwrap_or("AUTH FAILED".into()).to_uppercase()
                            );
                        }
                        Err(_) => app.system_message = String::from("[ERR] INVALID JSON RESPONSE"),
                    }
                }
                Err(_) => app.system_message = String::from("[ERR] BROKER FAILED"),
            }
        }
        _ => {}
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// DASHBOARD RENDERER
// ─────────────────────────────────────────────────────────────────────────────
fn draw_dashboard(f: &mut ratatui::Frame, app: &mut App) {
    let size = f.size();

    // Reset clickable regions every frame
    app.clickable.clear();

    // Root layout: top content + bottom statusbar
    let root = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(0), Constraint::Length(2)])
        .split(size);

    // Three column split
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage(20), // sidebar
            Constraint::Percentage(55), // main
            Constraint::Percentage(25), // inspector
        ])
        .split(root[0]);

    // ── SIDEBAR ────────────────────────────────────────────────────────────
    let sidebar = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(4),  // user profile
            Constraint::Length(12), // nav links
            Constraint::Min(0),     // spacer
            Constraint::Length(3),  // action btn
        ])
        .split(cols[0]);

    f.render_widget(
        Paragraph::new(vec![
            Line::from(Span::styled(&app.active_user_name, Style::default().fg(MAUVE).add_modifier(Modifier::BOLD))),
            Line::from(Span::styled("v2.0.4-edge", Style::default().fg(TEXT_MUTED))),
        ]),
        sidebar[0],
    );

    // Nav entries — each gets a separate Rect registered in clickable
    let nav = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2), // HOME
            Constraint::Length(2), // SEARCH
            Constraint::Length(2), // LIBRARY
            Constraint::Min(0),
        ])
        .split(sidebar[1]);

    let nav_items = [
        (ActiveTab::Home,    " ⌂ HOME    [H]"),
        (ActiveTab::Search,  " Q SEARCH  [S]"),
        (ActiveTab::Library, " ≡ LIBRARY [L]"),
    ];
    for (i, (tab, label)) in nav_items.iter().enumerate() {
        let is_active = app.active_tab == *tab;
        let style = if is_active {
            Style::default().fg(MAUVE_LIGHT).bg(SURFACE_HIGH).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(TEXT_MUTED)
        };
        f.render_widget(Paragraph::new(Span::styled(*label, style)), nav[i]);
        // Register clickable region for this sidebar tab
        app.clickable.insert(ClickTarget::SidebarTab(*tab), nav[i]);
    }

    f.render_widget(
        Paragraph::new(Span::styled(
            "    + NEW_BUFFER    ",
            Style::default().fg(MAUVE).bg(SURFACE_LOW),
        )).alignment(Alignment::Center),
        sidebar[3],
    );

    // ── MAIN CONTENT (tab-routed) ──────────────────────────────────────────
    let grid_area = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(4), Constraint::Min(0)])
        .split(cols[1]);

    match app.active_tab {
        ActiveTab::Home => {
            f.render_widget(
                Paragraph::new(vec![
                    Line::from(Span::styled("PROMPT: ./FETCH_TRENDING_DATA", Style::default().fg(TEXT_MUTED))),
                    Line::from(Span::styled("ACTIVE_ALBUMS", Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD))),
                ]),
                grid_area[0],
            );

            let rows = Layout::default()
                .direction(Direction::Vertical)
                .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
                .split(grid_area[1]);

            for r in 0..2 {
                let cells = Layout::default()
                    .direction(Direction::Horizontal)
                    .constraints([
                        Constraint::Percentage(33),
                        Constraint::Percentage(33),
                        Constraint::Percentage(34),
                    ])
                    .split(rows[r]);

                for c in 0..3 {
                    let idx = r * 3 + c;
                    // Register every cell slot so mouse events can reference them
                    app.clickable.insert(ClickTarget::GridTrack(idx), cells[c]);

                    if idx < app.catalog.len() {
                        let track      = &app.catalog[idx];
                        let is_selected = app.selected_track == idx;
                        let is_playing  = Some(idx) == app.active_playing_track;

                        let border_color = if is_playing {
                            GREEN
                        } else if is_selected {
                            MAUVE
                        } else {
                            SURFACE_HIGH
                        };

                        let cell_layout = Layout::default()
                            .direction(Direction::Vertical)
                            .constraints([Constraint::Length(11), Constraint::Length(4)])
                            .split(inset(cells[c], 1, 1));

                        // Art placeholder with colored border for selected/playing state
                        f.render_widget(
                            Block::default()
                                .style(Style::default().bg(SURFACE_LOW))
                                .border_style(Style::default().fg(border_color)),
                            cell_layout[0],
                        );

                        let title_style = if is_playing {
                            Style::default().fg(GREEN).add_modifier(Modifier::BOLD)
                        } else if is_selected {
                            Style::default().fg(MAUVE).add_modifier(Modifier::BOLD)
                        } else {
                            Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD)
                        };

                        f.render_widget(
                            Paragraph::new(vec![
                                Line::from(Span::styled(track.track.to_uppercase(), title_style)),
                                Line::from(Span::styled(
                                    track.artist.to_uppercase(),
                                    Style::default().fg(TEXT_MUTED),
                                )),
                            ]),
                            cell_layout[1],
                        );
                    }
                }
            }
        }

        ActiveTab::Search => {
            f.render_widget(
                Paragraph::new(vec![
                    Line::from(Span::styled("PROMPT: ./SEARCH_BUFFER", Style::default().fg(TEXT_MUTED))),
                    Line::from(Span::styled("SEARCH BUFFER", Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD))),
                ]),
                grid_area[0],
            );
            f.render_widget(
                Paragraph::new(vec![
                    Line::from(""),
                    Line::from(Span::styled(
                        "  ◆ SEARCH BUFFER ACTIVE",
                        Style::default().fg(MAUVE).add_modifier(Modifier::BOLD),
                    )),
                    Line::from(""),
                    Line::from(Span::styled(
                        "  Full-text fuzzy search is incoming.",
                        Style::default().fg(TEXT_MUTED).add_modifier(Modifier::ITALIC),
                    )),
                    Line::from(Span::styled(
                        "  Press [H] to return to the catalog.",
                        Style::default().fg(TEXT_MUTED),
                    )),
                ]).alignment(Alignment::Left),
                grid_area[1],
            );
        }

        ActiveTab::Library => {
            f.render_widget(
                Paragraph::new(vec![
                    Line::from(Span::styled("PROMPT: ./FETCH_LIBRARY", Style::default().fg(TEXT_MUTED))),
                    Line::from(Span::styled("LIBRARY", Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD))),
                ]),
                grid_area[0],
            );
            f.render_widget(
                Paragraph::new(vec![
                    Line::from(""),
                    Line::from(Span::styled(
                        "  ◆ LIBRARY [UNDER CONSTRUCTION]",
                        Style::default().fg(MAUVE).add_modifier(Modifier::BOLD),
                    )),
                    Line::from(""),
                    Line::from(Span::styled(
                        "  Personal playlists incoming.",
                        Style::default().fg(TEXT_MUTED).add_modifier(Modifier::ITALIC),
                    )),
                ]).alignment(Alignment::Left),
                grid_area[1],
            );
        }
    }

    // ── INSPECTOR ─────────────────────────────────────────────────────────
    let inspector = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2),   // [0] status
            Constraint::Length(16),  // [1] art box
            Constraint::Length(4),   // [2] metadata
            Constraint::Length(2),   // [3] progress bar
            Constraint::Length(3),   // [4] controls
            Constraint::Min(0),      // [5] spacer
            Constraint::Length(5),   // [6] system logs
        ])
        .split(cols[2]);

    // Status line
    let (status_text, status_color) = if app.is_playing {
        ("● NOW PLAYING", MAUVE)
    } else if app.active_playing_track.is_some() {
        ("⏸ PAUSED", TEXT_MUTED)
    } else {
        ("● STANDBY", TEXT_MUTED)
    };
    f.render_widget(
        Paragraph::new(Span::styled(status_text, Style::default().fg(status_color).add_modifier(Modifier::BOLD))),
        inspector[0],
    );

    // Art box — background block written here; Kitty image is injected after draw()
    let art_rect = inset(inspector[1], 0, 1);
    f.render_widget(Block::default().style(Style::default().bg(SURFACE_LOW)), art_rect);
    app.inspector_art_rect = Some(art_rect);

    // Track metadata in inspector
    let display_idx = app.active_playing_track.unwrap_or(app.selected_track);
    if !app.catalog.is_empty() {
        let t = &app.catalog[display_idx];
        f.render_widget(
            Paragraph::new(vec![
                Line::from(Span::styled(
                    t.track.to_uppercase(),
                    Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD),
                )),
                Line::from(Span::styled(t.artist.to_uppercase(), Style::default().fg(MAUVE))),
            ]),
            inspector[2],
        );

        // Progress bar
        let area = inspector[3];
        let bar_w = area.width.saturating_sub(14) as usize;

        if app.playback_duration > 0.0 {
            let pct    = (app.playback_elapsed / app.playback_duration).clamp(0.0, 1.0);
            let filled = (pct * bar_w as f64) as usize;
            let empty  = bar_w.saturating_sub(filled);

            let em = (app.playback_elapsed as u64) / 60;
            let es = (app.playback_elapsed as u64) % 60;
            let dm = (app.playback_duration as u64) / 60;
            let ds = (app.playback_duration as u64) % 60;

            f.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::raw(" "),
                    Span::styled("█".repeat(filled), Style::default().fg(MAUVE)),
                    Span::styled("░".repeat(empty),  Style::default().fg(SURFACE_HIGH)),
                    Span::styled(format!(" {:02}:{:02}/{:02}:{:02}", em, es, dm, ds), Style::default().fg(TEXT_MUTED)),
                ])),
                area,
            );
        } else if app.playback_elapsed > 0.0 {
            // Duration unknown — pulsing indeterminate bar
            let em      = (app.playback_elapsed as u64) / 60;
            let es      = (app.playback_elapsed as u64) % 60;
            let pulse   = ((app.playback_elapsed as usize) * 2) % bar_w.max(1);
            let mut bar: Vec<Span> = vec![Span::raw(" ")];
            for i in 0..bar_w {
                if i >= pulse && i < pulse + 4 {
                    bar.push(Span::styled("█", Style::default().fg(MAUVE)));
                } else {
                    bar.push(Span::styled("░", Style::default().fg(SURFACE_HIGH)));
                }
            }
            bar.push(Span::styled(format!(" {:02}:{:02}/??:??", em, es), Style::default().fg(TEXT_MUTED)));
            f.render_widget(Paragraph::new(Line::from(bar)), area);
        } else {
            f.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::raw(" "),
                    Span::styled("░".repeat(bar_w), Style::default().fg(SURFACE_HIGH)),
                    Span::styled(" --:--/--:--", Style::default().fg(TEXT_MUTED)),
                ])),
                area,
            );
        }

        // Controls — register play/pause button rect
        let play_icon = if app.is_playing { "⏸" } else { "▶" };
        let ctrl_line = format!("🔀  ⏮  {}  ⏭  🔁", play_icon);
        f.render_widget(
            Paragraph::new(Span::styled(&ctrl_line, Style::default().fg(TEXT_ACTIVE))),
            inspector[4],
        );
        app.clickable.insert(ClickTarget::PlayPause, inspector[4]);
    }

    // System logs
    f.render_widget(
        Paragraph::new(
            app.system_logs.iter()
                .map(|l| Line::from(Span::styled(l.as_str(), Style::default().fg(TEXT_MUTED))))
                .collect::<Vec<_>>()
        ),
        inspector[6],
    );

    // ── STATUSBAR ─────────────────────────────────────────────────────────
    let statusbar = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(60), Constraint::Percentage(40)])
        .split(root[1]);

    let now_playing = if !app.catalog.is_empty() {
        format!(" ▶  {}  ·  {}", app.catalog[app.selected_track].track.to_uppercase(), app.catalog[app.selected_track].artist.to_uppercase())
    } else {
        " ▶  AWAITING STREAM".to_string()
    };

    f.render_widget(
        Paragraph::new(Span::styled(now_playing, Style::default().fg(TEXT_ACTIVE).bg(SURFACE_HIGH))),
        inset(statusbar[0], 0, 0),
    );
    f.render_widget(
        Paragraph::new(Span::styled(
            " [H] Home  [S] Search  [L] Library  [SPC] Pause  [ESC] Quit",
            Style::default().fg(TEXT_MUTED),
        )).alignment(Alignment::Right),
        statusbar[1],
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// HELPER
// ─────────────────────────────────────────────────────────────────────────────
fn inset(rect: Rect, dx: u16, dy: u16) -> Rect {
    Rect {
        x:      rect.x.saturating_add(dx),
        y:      rect.y.saturating_add(dy),
        width:  rect.width.saturating_sub(dx * 2),
        height: rect.height.saturating_sub(dy * 2),
    }
}

fn rect_contains(rect: Rect, col: u16, row: u16) -> bool {
    col >= rect.x && col < rect.x + rect.width
        && row >= rect.y && row < rect.y + rect.height
}

// ─────────────────────────────────────────────────────────────────────────────
// SHARED TRACK PLAY LOGIC (keyboard Enter + mouse click both route here)
// ─────────────────────────────────────────────────────────────────────────────
fn handle_track_play(idx: usize, app: &mut App) {
    if idx >= app.catalog.len() { return; }

    app.kill_mpv();
    clear_kitty_images();
    app.cover_art_data  = None;
    app.kitty_rendered  = false;
    app.selected_track  = idx;
    app.active_playing_track = Some(idx);
    app.is_playing      = false;

    let name      = app.catalog[idx].track.clone();
    let tenant    = app.catalog[idx].tenant.clone();
    let file_key  = app.catalog[idx].file_key.clone();
    let cover_key = app.catalog[idx].cover_key.clone();

    app.push_log(format!("[SYS] LOADING: {}", name.to_uppercase()));

    // ── Audio stream task ─────────────────────────────────────────────────
    let tx = app.bg_tx.clone();
    let t2 = tenant.clone();
    tokio::spawn(async move {
        let _ = tx.send(BackgroundEvent::Log("[SYS] BROKER HANDSHAKE...".into())).await;
        let out = tokio::process::Command::new("python")
            .args(["../backend.py", "stream", &t2, &file_key])
            .output().await;

        match out {
            Ok(o) => {
                let raw = String::from_utf8_lossy(&o.stdout);
                match serde_json::from_str::<StreamResponse>(&raw) {
                    Ok(r) if r.status == "success" => {
                        if let Some(url) = r.url {
                            let _ = tx.send(BackgroundEvent::StreamReady(url)).await;
                        } else {
                            let _ = tx.send(BackgroundEvent::StreamError("NO URL IN RESPONSE".into())).await;
                        }
                    }
                    Ok(r) => {
                        let _ = tx.send(BackgroundEvent::StreamError(
                            r.message.unwrap_or("BROKER ERROR".into()).to_uppercase()
                        )).await;
                    }
                    Err(_) => {
                        let _ = tx.send(BackgroundEvent::StreamError("INVALID JSON FROM BROKER".into())).await;
                    }
                }
            }
            Err(e) => {
                let _ = tx.send(BackgroundEvent::StreamError(format!("BROKER EXEC FAILED: {}", e))).await;
            }
        }
    });

    // ── Cover art task ────────────────────────────────────────────────────
    if !cover_key.is_empty() && cover_key != "NONE" {
        let tx2 = app.bg_tx.clone();
        tokio::spawn(async move {
            let _ = tx2.send(BackgroundEvent::Log("[SYS] FETCHING COVER ART...".into())).await;
            let out = tokio::process::Command::new("python")
                .args(["../backend.py", "stream", &tenant, &cover_key])
                .output().await;

            if let Ok(o) = out {
                let raw = String::from_utf8_lossy(&o.stdout);
                if let Ok(r) = serde_json::from_str::<StreamResponse>(&raw) {
                    if r.status == "success" {
                        if let Some(url) = r.url {
                            match reqwest::get(&url).await {
                                Ok(resp) => {
                                    match resp.bytes().await {
                                        Ok(bytes) => {
                                            let _ = tx2.send(BackgroundEvent::CoverArtReady(bytes.to_vec())).await;
                                        }
                                        Err(e) => {
                                            let _ = tx2.send(BackgroundEvent::Log(
                                                format!("[ERR] COVER BYTES: {}", e)
                                            )).await;
                                        }
                                    }
                                }
                                Err(e) => {
                                    let _ = tx2.send(BackgroundEvent::Log(
                                        format!("[ERR] COVER GET: {}", e)
                                    )).await;
                                }
                            }
                        }
                    }
                }
            }
        });
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// INPUT HANDLERS
// ─────────────────────────────────────────────────────────────────────────────
fn handle_dashboard_input(key: KeyCode, app: &mut App) {
    match key {
        KeyCode::Char('h') | KeyCode::Char('H') => app.active_tab = ActiveTab::Home,
        KeyCode::Char('s') | KeyCode::Char('S') => app.active_tab = ActiveTab::Search,
        KeyCode::Char('l') | KeyCode::Char('L') => app.active_tab = ActiveTab::Library,

        KeyCode::Left  => { if app.selected_track > 0 { app.selected_track -= 1; } }
        KeyCode::Right => { if app.selected_track + 1 < app.catalog.len() { app.selected_track += 1; } }
        KeyCode::Up    => { if app.selected_track >= 3 { app.selected_track -= 3; } }
        KeyCode::Down  => { if app.selected_track + 3 < app.catalog.len() { app.selected_track += 3; } }

        KeyCode::Enter => {
            let idx = app.selected_track;
            handle_track_play(idx, app);
        }

        KeyCode::Char(' ') => {
            if app.mpv_child.is_some() {
                tokio::spawn(async {
                    if let Ok(mut sock) = UnixStream::connect("/tmp/termstream_mpv.sock").await {
                        let _ = sock.write_all(b"{\"command\":[\"cycle\",\"pause\"]}\n").await;
                    }
                });
            }
        }

        _ => {}
    }
}

fn handle_mouse_input(mouse: MouseEvent, app: &mut App) {
    if mouse.kind != MouseEventKind::Down(MouseButton::Left) {
        return;
    }
    let (col, row) = (mouse.column, mouse.row);

    // Clone the targets we might need so we don't borrow app immutably and mutably
    let mut tab_hit:   Option<ActiveTab> = None;
    let mut track_hit: Option<usize>     = None;
    let mut pause_hit: bool              = false;

    for (target, rect) in &app.clickable {
        if rect_contains(*rect, col, row) {
            match target {
                ClickTarget::SidebarTab(tab)  => tab_hit   = Some(*tab),
                ClickTarget::GridTrack(idx)   => track_hit = Some(*idx),
                ClickTarget::PlayPause        => pause_hit = true,
            }
            break;
        }
    }

    if let Some(tab) = tab_hit {
        app.active_tab = tab;
    } else if let Some(idx) = track_hit {
        handle_track_play(idx, app);
    } else if pause_hit && app.mpv_child.is_some() {
        tokio::spawn(async {
            if let Ok(mut sock) = UnixStream::connect("/tmp/termstream_mpv.sock").await {
                let _ = sock.write_all(b"{\"command\":[\"cycle\",\"pause\"]}\n").await;
            }
        });
    }
}