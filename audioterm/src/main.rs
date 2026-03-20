use crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, MouseButton, MouseEvent, MouseEventKind},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::{Backend, CrosstermBackend},
    layout::{Alignment, Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Paragraph},
    Terminal,
};
use serde::Deserialize;
use std::{error::Error, io, io::Write, process::Command};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::sync::{mpsc, watch};
use base64::Engine;

// --- BACKGROUND EVENT SYSTEM ---
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

// --- CATPPUCCIN PALETTE ---
const MAUVE: Color = Color::Rgb(203, 166, 247);
const MAUVE_LIGHT: Color = Color::Rgb(226, 199, 255);
const TEXT_ACTIVE: Color = Color::Rgb(205, 214, 244);
const TEXT_MUTED: Color = Color::Rgb(166, 173, 200);
const SURFACE_LOW: Color = Color::Rgb(30, 30, 46);
const SURFACE_HIGH: Color = Color::Rgb(49, 50, 68);

// --- DATA STRUCTURES ---
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

// --- STATE MACHINE ---
#[derive(PartialEq)]
enum AppState { Auth, Dashboard }
#[derive(PartialEq)]
enum AuthMode { Login, CreatorSignup, ListenerSignup }
#[derive(PartialEq)]
enum InputMode { Username, Password }

// --- SIDEBAR TAB STATE ---
#[derive(PartialEq)]
enum SidebarTab { Home, Search, Library }

struct App {
    state: AppState,
    auth_mode: AuthMode,
    input_mode: InputMode,
    username: String,
    password: String,
    system_message: String,

    catalog: Vec<Track>,
    selected_track: usize,
    active_user_name: String,

    // --- SIDEBAR STATE ---
    active_tab: SidebarTab,

    // --- DYNAMIC PLAYBACK STATE ---
    active_playing_track: Option<usize>,
    is_playing: bool,
    system_logs: Vec<String>,

    // --- MPV DAEMON & ASYNC CHANNELS ---
    mpv_child: Option<std::process::Child>,
    playback_elapsed: f64,
    playback_duration: f64,
    bg_rx: mpsc::Receiver<BackgroundEvent>,
    bg_tx: mpsc::Sender<BackgroundEvent>,
    ipc_cancel: Option<watch::Sender<bool>>,

    // --- COVER ART (Kitty Graphics) ---
    cover_art_data: Option<Vec<u8>>,
    /// Cached rect of the inspector art box for post-render Kitty injection.
    inspector_art_rect: Option<Rect>,
    /// True once Kitty transmission has fired for the current cover art load.
    kitty_rendered: bool,

    // --- MOUSE HIT CACHE ---
    /// Bounding boxes of each grid track cell, populated during draw.
    grid_cell_rects: Vec<Rect>,
}

impl App {
    fn new() -> App {
        let (bg_tx, bg_rx) = mpsc::channel::<BackgroundEvent>(32);
        App {
            state: AppState::Auth,
            auth_mode: AuthMode::Login,
            input_mode: InputMode::Username,
            username: String::new(),
            password: String::new(),
            system_message: String::from("[SYS] AWAITING CREDENTIALS..."),
            catalog: Vec::new(),
            selected_track: 0,
            active_user_name: String::new(),

            active_tab: SidebarTab::Home,

            active_playing_track: None,
            is_playing: false,
            system_logs: vec![
                String::from("[SYS] KERNEL BOOT..."),
                String::from("[SYS] AWAITING AUTHENTICATION"),
            ],

            mpv_child: None,
            playback_elapsed: 0.0,
            playback_duration: 0.0,
            bg_rx,
            bg_tx,
            ipc_cancel: None,

            cover_art_data: None,
            inspector_art_rect: None,
            kitty_rendered: false,

            grid_cell_rects: Vec::new(),
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
        self.mpv_child = None;
        self.playback_elapsed = 0.0;
        self.playback_duration = 0.0;
        self.kitty_rendered = false;
    }

    fn spawn_mpv(&mut self, url: &str) {
        self.kill_mpv();
        let _ = std::fs::remove_file("/tmp/termstream_mpv.sock");
        match std::process::Command::new("mpv")
            .arg("--no-video")
            .arg("--msg-level=all=no")
            .arg("--input-ipc-server=/tmp/termstream_mpv.sock")
            .arg(url)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
        {
            Ok(child) => {
                self.mpv_child = Some(child);
                self.is_playing = true;
                self.push_log("[SYS] MPV DAEMON ONLINE".to_string());
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
                    let _ = tx.send(BackgroundEvent::Log("[ERR] IPC SOCKET TIMEOUT".to_string())).await;
                    return;
                }
            };

            let _ = tx.send(BackgroundEvent::Log("[SYS] IPC PIPE CONNECTED".to_string())).await;

            let (reader, mut writer) = stream.into_split();
            let mut buf_reader = BufReader::new(reader);
            let mut cancel_rx = cancel_rx;

            let observe_cmds = [
                "{\"command\":[\"observe_property\",1,\"time-pos\"]}\n",
                "{\"command\":[\"observe_property\",2,\"duration\"]}\n",
                "{\"command\":[\"observe_property\",3,\"pause\"]}\n",
            ];
            for cmd in &observe_cmds {
                if writer.write_all(cmd.as_bytes()).await.is_err() {
                    let _ = tx.send(BackgroundEvent::Log("[ERR] IPC WRITE FAILED".to_string())).await;
                    return;
                }
            }
            let _ = tx.send(BackgroundEvent::Log("[SYS] PROPERTY OBSERVERS REGISTERED".to_string())).await;

            let mut last_elapsed: f64 = 0.0;
            let mut last_duration: f64 = 0.0;

            loop {
                let mut line = String::new();

                tokio::select! {
                    result = buf_reader.read_line(&mut line) => {
                        match result {
                            Ok(0) => {
                                let _ = tx.send(BackgroundEvent::PlaybackEnded).await;
                                return;
                            }
                            Ok(_) => {
                                if let Ok(val) = serde_json::from_str::<serde_json::Value>(&line) {
                                    if val.get("event").and_then(|v| v.as_str()) == Some("property-change") {
                                        let id = val.get("id").and_then(|v| v.as_u64()).unwrap_or(0);
                                        match id {
                                            1 => {
                                                if let Some(data) = val.get("data").and_then(|v| v.as_f64()) {
                                                    last_elapsed = data;
                                                    let _ = tx.send(BackgroundEvent::PlaybackProgress {
                                                        elapsed: last_elapsed,
                                                        duration: last_duration,
                                                    }).await;
                                                }
                                            }
                                            2 => {
                                                if let Some(data) = val.get("data").and_then(|v| v.as_f64()) {
                                                    last_duration = data;
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
                                    } else if val.get("event").and_then(|v| v.as_str()) == Some("end-file") {
                                        let _ = tx.send(BackgroundEvent::PlaybackEnded).await;
                                        return;
                                    }
                                }
                            }
                            Err(_) => {
                                let _ = tx.send(BackgroundEvent::PlaybackEnded).await;
                                return;
                            }
                        }
                    }
                    _ = cancel_rx.changed() => {
                        break;
                    }
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
        let output = Command::new("python").arg("../backend.py").arg("catalog").output();
        if let Ok(out) = output {
            let stdout = String::from_utf8_lossy(&out.stdout);
            if let Ok(tracks) = serde_json::from_str::<Vec<Track>>(&stdout) {
                self.catalog = tracks;
                self.system_message = String::from("[SYS] CATALOG SYNC COMPLETE");
            } else {
                self.system_message = String::from("[ERR] FAILED TO PARSE CATALOG");
            }
        } else {
            self.system_message = String::from("[ERR] BROKER CONNECTION FAILED");
        }
    }
}

// --- KITTY GRAPHICS PROTOCOL ---
/// Transmit image bytes to the terminal at `(col, row)` (1-based), constrained to `cols x rows` cells.
/// Uses `s=auto,v=auto` for automatic scaling within the cell bounds.
fn render_kitty_image(image_bytes: &[u8], col: u16, row: u16, cols: u16, rows: u16) {
    let b64 = base64::engine::general_purpose::STANDARD.encode(image_bytes);
    let chunk_size = 4096;
    let mut stdout = io::stdout();

    let _ = write!(stdout, "\x1b[{};{}H", row, col);

    for (i, chunk) in b64.as_bytes().chunks(chunk_size).enumerate() {
        let chunk_str = std::str::from_utf8(chunk).unwrap_or("");
        let more = if (i + 1) * chunk_size < b64.len() { 1 } else { 0 };

        if i == 0 {
            let _ = write!(
                stdout,
                "\x1b_Ga=T,f=100,c={},r={},s=auto,v=auto,q=2,m={};{}\x1b\\",
                cols, rows, more, chunk_str
            );
        } else {
            let _ = write!(stdout, "\x1b_Gm={};{}\x1b\\", more, chunk_str);
        }
    }

    let _ = stdout.flush();
}

fn clear_kitty_images() {
    let mut stdout = io::stdout();
    let _ = write!(stdout, "\x1b_Ga=d,d=a\x1b\\");
    let _ = stdout.flush();
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let mut app = App::new();
    let res = run_app(&mut terminal, &mut app).await;

    clear_kitty_images();

    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen, DisableMouseCapture)?;
    terminal.show_cursor()?;
    if let Err(err) = res { println!("{:?}", err); }
    Ok(())
}

async fn run_app<B: Backend>(terminal: &mut Terminal<B>, app: &mut App) -> io::Result<()> {
    loop {
        // --- POLL BACKGROUND EVENTS (non-blocking) ---
        while let Ok(event) = app.bg_rx.try_recv() {
            match event {
                BackgroundEvent::Log(msg) => app.push_log(msg),
                BackgroundEvent::StreamReady(url) => {
                    app.push_log("[SYS] PRE-SIGNED URL ACQUIRED".to_string());
                    app.spawn_mpv(&url);
                    app.start_ipc_observer();
                }
                BackgroundEvent::StreamError(msg) => {
                    app.push_log(format!("[ERR] {}", msg));
                    app.is_playing = false;
                }
                BackgroundEvent::PlaybackProgress { elapsed, duration } => {
                    app.playback_elapsed = elapsed;
                    app.playback_duration = duration;
                }
                BackgroundEvent::PlaybackPaused(paused) => {
                    app.is_playing = !paused;
                    if paused {
                        app.push_log("[SYS] STREAM PAUSED".to_string());
                    } else {
                        app.push_log("[SYS] STREAM RESUMED".to_string());
                    }
                }
                BackgroundEvent::PlaybackEnded => {
                    app.is_playing = false;
                    app.playback_elapsed = 0.0;
                    app.playback_duration = 0.0;
                    app.push_log("[SYS] TRACK ENDED".to_string());
                }
                BackgroundEvent::CoverArtReady(data) => {
                    app.push_log("[SYS] COVER ART LOADED".to_string());
                    app.cover_art_data = Some(data);
                    // Reset so the new art gets rendered on next frame
                    app.kitty_rendered = false;
                }
            }
        }

        terminal.draw(|f| {
            f.render_widget(Block::default().style(Style::default().bg(Color::Reset)), f.size());
            match app.state {
                AppState::Auth => draw_auth(f, app),
                AppState::Dashboard => draw_dashboard(f, app),
            }
        })?;

        // --- POST-RENDER KITTY INJECTION ---
        // Only fires once per new cover art load (guarded by kitty_rendered flag)
        if app.state == AppState::Dashboard && !app.kitty_rendered {
            if let (Some(ref data), Some(rect)) = (&app.cover_art_data, app.inspector_art_rect) {
                if rect.width > 0 && rect.height > 0 {
                    render_kitty_image(
                        data,
                        rect.x + 1, // convert from 0-based Ratatui to 1-based terminal
                        rect.y + 1,
                        rect.width,
                        rect.height,
                    );
                    app.kitty_rendered = true;
                }
            }
        }

        if event::poll(std::time::Duration::from_millis(16))? {
            match event::read()? {
                Event::Key(key) => {
                    if key.code == KeyCode::Esc {
                        app.kill_mpv();
                        clear_kitty_images();
                        return Ok(());
                    }
                    match app.state {
                        AppState::Auth => handle_auth_input(key.code, app),
                        AppState::Dashboard => handle_dashboard_input(key.code, app),
                    }
                }
                Event::Mouse(mouse) => {
                    if app.state == AppState::Dashboard {
                        handle_mouse_input(mouse, app);
                    }
                }
                _ => {}
            }
        }
    }
}

// --- AUTH RENDERER ---
fn draw_auth(f: &mut ratatui::Frame, app: &App) {
    let size = f.size();
    let vertical_chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Percentage(30), Constraint::Length(15), Constraint::Percentage(30)])
        .split(size);

    let horizontal_chunks = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(30), Constraint::Percentage(40), Constraint::Percentage(30)])
        .split(vertical_chunks[1]);

    let ui_chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2),
            Constraint::Length(4),
            Constraint::Length(3),
            Constraint::Length(3),
            Constraint::Length(3),
        ])
        .split(horizontal_chunks[1]);

    let header = Paragraph::new(Line::from(vec![
        Span::styled("TERMINAL_OS ", Style::default().fg(MAUVE).add_modifier(Modifier::BOLD)),
        Span::styled("// AUTH", Style::default().fg(TEXT_MUTED)),
    ])).alignment(Alignment::Center);
    f.render_widget(header, ui_chunks[0]);

    let tabs = Paragraph::new(vec![
        Line::from(Span::styled(if app.auth_mode == AuthMode::Login { "▶ [ LOGIN ]" } else { "  LOGIN" }, Style::default().fg(if app.auth_mode == AuthMode::Login { MAUVE_LIGHT } else { TEXT_MUTED }))),
        Line::from(Span::styled(if app.auth_mode == AuthMode::CreatorSignup { "▶ [ CREATOR ]" } else { "  CREATOR" }, Style::default().fg(if app.auth_mode == AuthMode::CreatorSignup { MAUVE_LIGHT } else { TEXT_MUTED }))),
        Line::from(Span::styled(if app.auth_mode == AuthMode::ListenerSignup { "▶ [ LISTENER ]" } else { "  LISTENER" }, Style::default().fg(if app.auth_mode == AuthMode::ListenerSignup { MAUVE_LIGHT } else { TEXT_MUTED }))),
    ]).alignment(Alignment::Center);
    f.render_widget(tabs, ui_chunks[1]);

    let u_prefix = if app.input_mode == InputMode::Username { ">> " } else { "   " };
    let p_prefix = if app.input_mode == InputMode::Password { ">> " } else { "   " };
    let masked_pass = "*".repeat(app.password.len());

    let user_p = Paragraph::new(vec![
        Line::from(Span::styled("IDENTITY", Style::default().fg(TEXT_MUTED))),
        Line::from(vec![
            Span::styled(u_prefix, Style::default().fg(MAUVE_LIGHT)),
            Span::styled(&app.username, Style::default().fg(TEXT_ACTIVE)),
            if app.input_mode == InputMode::Username { Span::styled("█", Style::default().fg(MAUVE)) } else { Span::raw("") },
        ]),
    ]);
    f.render_widget(user_p, ui_chunks[2]);

    let pass_p = Paragraph::new(vec![
        Line::from(Span::styled("ACCESS_KEY", Style::default().fg(TEXT_MUTED))),
        Line::from(vec![
            Span::styled(p_prefix, Style::default().fg(MAUVE_LIGHT)),
            Span::styled(masked_pass, Style::default().fg(TEXT_ACTIVE)),
            if app.input_mode == InputMode::Password { Span::styled("█", Style::default().fg(MAUVE)) } else { Span::raw("") },
        ]),
    ]);
    f.render_widget(pass_p, ui_chunks[3]);

    let sys_color = if app.system_message.starts_with("[ERR]") { Color::Red } else { TEXT_MUTED };
    let footer = Paragraph::new(vec![
        Line::from(Span::styled(&app.system_message, Style::default().fg(sys_color))),
        Line::from(Span::styled("[ENTER] Execute   [ESC] Quit", Style::default().fg(TEXT_MUTED))),
    ]).alignment(Alignment::Center);
    f.render_widget(footer, ui_chunks[4]);
}

fn handle_auth_input(key: KeyCode, app: &mut App) {
    match key {
        KeyCode::Tab => {
            app.auth_mode = match app.auth_mode {
                AuthMode::Login => AuthMode::CreatorSignup,
                AuthMode::CreatorSignup => AuthMode::ListenerSignup,
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
                InputMode::Username => { app.username.push(c); }
                InputMode::Password => { app.password.push(c); }
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

            if let Ok(out) = output {
                let stdout = String::from_utf8_lossy(&out.stdout);
                if let Ok(response) = serde_json::from_str::<AuthResponse>(&stdout) {
                    if response.status == "success" {
                        app.active_user_name = response.artist_name.unwrap_or_default();
                        app.state = AppState::Dashboard;
                        app.fetch_catalog();
                    } else {
                        app.system_message = format!("[ERR] {}", response.message.unwrap_or("AUTH FAILED".to_string()).to_uppercase());
                    }
                } else {
                    app.system_message = String::from("[ERR] INVALID JSON RESPONSE");
                }
            } else {
                app.system_message = String::from("[ERR] BROKER FAILED");
            }
        }
        _ => {}
    }
}

// --- DASHBOARD RENDERER ---
fn draw_dashboard(f: &mut ratatui::Frame, app: &mut App) {
    let size = f.size();

    let main_layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(0), Constraint::Length(3)])
        .split(size);

    let columns = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage(20), // Sidebar
            Constraint::Percentage(55), // Main Grid
            Constraint::Percentage(25), // Inspector
        ])
        .split(main_layout[0]);

    // 1. SIDEBAR
    let sidebar_layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(4),
            Constraint::Length(12),
            Constraint::Min(0),
            Constraint::Length(3),
        ]).split(columns[0]);

    let user_profile = Paragraph::new(vec![
        Line::from(Span::styled(&app.active_user_name, Style::default().fg(MAUVE).add_modifier(Modifier::BOLD))),
        Line::from(Span::styled("v2.0.4-edge", Style::default().fg(TEXT_MUTED))),
    ]);
    f.render_widget(user_profile, sidebar_layout[0]);

    // Dynamic sidebar: only active tab gets SURFACE_HIGH bg + MAUVE_LIGHT fg
    let home_style = if app.active_tab == SidebarTab::Home {
        Style::default().fg(MAUVE_LIGHT).bg(SURFACE_HIGH)
    } else {
        Style::default().fg(TEXT_MUTED)
    };
    let search_style = if app.active_tab == SidebarTab::Search {
        Style::default().fg(MAUVE_LIGHT).bg(SURFACE_HIGH)
    } else {
        Style::default().fg(TEXT_MUTED)
    };
    let library_style = if app.active_tab == SidebarTab::Library {
        Style::default().fg(MAUVE_LIGHT).bg(SURFACE_HIGH)
    } else {
        Style::default().fg(TEXT_MUTED)
    };

    let nav_links = Paragraph::new(vec![
        Line::from(""),
        Line::from(Span::styled(" ⌂ HOME [H]", home_style)),
        Line::from(""),
        Line::from(Span::styled(" Q SEARCH [S]", search_style)),
        Line::from(""),
        Line::from(Span::styled(" ≡ LIBRARY [L]", library_style)),
    ]);
    f.render_widget(nav_links, sidebar_layout[1]);

    let new_buffer_btn = Paragraph::new(Span::styled(
        "    + NEW_BUFFER    ",
        Style::default().fg(MAUVE).bg(SURFACE_LOW)
    )).alignment(Alignment::Center);
    f.render_widget(new_buffer_btn, sidebar_layout[3]);

    // 2. MAIN GRID — conditional based on active_tab
    let grid_layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(4), Constraint::Min(0)])
        .split(columns[1]);

    match app.active_tab {
        SidebarTab::Home => {
            let grid_header = Paragraph::new(vec![
                Line::from(Span::styled("PROMPT: ./FETCH_TRENDING_DATA", Style::default().fg(TEXT_MUTED))),
                Line::from(Span::styled("ACTIVE_ALBUMS", Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD))),
            ]);
            f.render_widget(grid_header, grid_layout[0]);

            // Clear the cached rects before re-populating
            app.grid_cell_rects.clear();

            let rows = Layout::default()
                .direction(Direction::Vertical)
                .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
                .split(grid_layout[1]);

            for r in 0..2 {
                let cols = Layout::default()
                    .direction(Direction::Horizontal)
                    .constraints([Constraint::Percentage(33), Constraint::Percentage(33), Constraint::Percentage(33)])
                    .split(rows[r]);
                for c in 0..3 {
                    let idx = (r * 3) + c;
                    // Always push the cell rect (even for empty slots) so index alignment is preserved
                    app.grid_cell_rects.push(cols[c]);

                    if idx < app.catalog.len() {
                        let track = &app.catalog[idx];
                        let is_selected = app.selected_track == idx;

                        let title_style = if is_selected {
                            Style::default().fg(MAUVE).add_modifier(Modifier::BOLD)
                        } else {
                            Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD)
                        };

                        let item_layout = Layout::default()
                            .direction(Direction::Vertical)
                            .constraints([Constraint::Length(12), Constraint::Length(3)])
                            .split(cols[c]);

                        let art_box = Block::default().style(Style::default().bg(SURFACE_LOW));
                        f.render_widget(art_box, inset(item_layout[0], 1, 1));

                        let meta = Paragraph::new(vec![
                            Line::from(Span::styled(track.track.to_uppercase(), title_style)),
                            Line::from(Span::styled(track.artist.to_uppercase(), Style::default().fg(TEXT_MUTED))),
                        ]);
                        f.render_widget(meta, inset(item_layout[1], 1, 0));
                    }
                }
            }
        }
        SidebarTab::Search => {
            let search_header = Paragraph::new(vec![
                Line::from(Span::styled("PROMPT: ./SEARCH_BUFFER", Style::default().fg(TEXT_MUTED))),
                Line::from(Span::styled("SEARCH BUFFER", Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD))),
            ]);
            f.render_widget(search_header, grid_layout[0]);

            let placeholder = Paragraph::new(vec![
                Line::from(""),
                Line::from(Span::styled(
                    "  SEARCH BUFFER [UNDER CONSTRUCTION]",
                    Style::default().fg(TEXT_MUTED).add_modifier(Modifier::ITALIC),
                )),
                Line::from(""),
                Line::from(Span::styled(
                    "  Full-text fuzzy search across catalog",
                    Style::default().fg(SURFACE_HIGH),
                )),
                Line::from(Span::styled(
                    "  incoming in the next release.",
                    Style::default().fg(SURFACE_HIGH),
                )),
            ]);
            f.render_widget(placeholder, grid_layout[1]);
        }
        SidebarTab::Library => {
            let library_header = Paragraph::new(vec![
                Line::from(Span::styled("PROMPT: ./FETCH_LIBRARY", Style::default().fg(TEXT_MUTED))),
                Line::from(Span::styled("LIBRARY", Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD))),
            ]);
            f.render_widget(library_header, grid_layout[0]);

            let placeholder = Paragraph::new(vec![
                Line::from(""),
                Line::from(Span::styled(
                    "  LIBRARY [UNDER CONSTRUCTION]",
                    Style::default().fg(TEXT_MUTED).add_modifier(Modifier::ITALIC),
                )),
                Line::from(""),
                Line::from(Span::styled(
                    "  Personal playlist and saved tracks",
                    Style::default().fg(SURFACE_HIGH),
                )),
                Line::from(Span::styled(
                    "  incoming in the next release.",
                    Style::default().fg(SURFACE_HIGH),
                )),
            ]);
            f.render_widget(placeholder, grid_layout[1]);
        }
    }

    // 3. INSPECTOR
    let inspector_layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2),   // [0] Status header
            Constraint::Length(16),  // [1] Art box
            Constraint::Length(4),   // [2] Track metadata
            Constraint::Length(2),   // [3] Progress bar
            Constraint::Length(3),   // [4] Controls
            Constraint::Min(0),      // [5] Spacer
            Constraint::Length(5),   // [6] System logs
        ]).split(columns[2]);

    let status_text = if app.is_playing { "● NOW_PLAYING" } else if app.active_playing_track.is_some() { "⏸ PAUSED" } else { "● STANDBY" };
    let status_color = if app.is_playing { MAUVE } else { TEXT_MUTED };
    f.render_widget(Paragraph::new(Span::styled(status_text, Style::default().fg(status_color))), inspector_layout[0]);

    // Store the art rect for post-render Kitty injection
    let art_rect = inset(inspector_layout[1], 0, 1);
    f.render_widget(Block::default().style(Style::default().bg(SURFACE_LOW)), art_rect);
    app.inspector_art_rect = Some(art_rect);

    let display_idx = app.active_playing_track.unwrap_or(app.selected_track);
    if !app.catalog.is_empty() {
        let t = &app.catalog[display_idx];
        let meta_large = Paragraph::new(vec![
            Line::from(Span::styled(t.track.to_uppercase(), Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD))),
            Line::from(Span::styled(t.artist.to_uppercase(), Style::default().fg(MAUVE))),
        ]);
        f.render_widget(meta_large, inspector_layout[2]);

        // Progress bar
        let progress_area = inspector_layout[3];
        if app.playback_duration > 0.0 {
            let pct = (app.playback_elapsed / app.playback_duration).clamp(0.0, 1.0);
            let bar_width = progress_area.width.saturating_sub(14) as usize;
            let filled = (pct * bar_width as f64) as usize;
            let empty = bar_width.saturating_sub(filled);

            let elapsed_min = (app.playback_elapsed as u64) / 60;
            let elapsed_sec = (app.playback_elapsed as u64) % 60;
            let dur_min = (app.playback_duration as u64) / 60;
            let dur_sec = (app.playback_duration as u64) % 60;

            let bar_line = Line::from(vec![
                Span::styled(" ", Style::default()),
                Span::styled("█".repeat(filled), Style::default().fg(MAUVE)),
                Span::styled("░".repeat(empty), Style::default().fg(SURFACE_HIGH)),
                Span::styled(
                    format!(" {:02}:{:02}/{:02}:{:02}", elapsed_min, elapsed_sec, dur_min, dur_sec),
                    Style::default().fg(TEXT_MUTED),
                ),
            ]);
            f.render_widget(Paragraph::new(bar_line), progress_area);
        } else if app.playback_elapsed > 0.0 {
            // Duration not yet known — show pulsing elapsed
            let bar_width = progress_area.width.saturating_sub(14) as usize;
            let elapsed_min = (app.playback_elapsed as u64) / 60;
            let elapsed_sec = (app.playback_elapsed as u64) % 60;
            let pulse_pos = ((app.playback_elapsed as usize) * 2) % bar_width.max(1);

            let mut bar_chars: Vec<Span> = vec![Span::styled(" ", Style::default())];
            for i in 0..bar_width {
                if i >= pulse_pos && i < pulse_pos + 3 {
                    bar_chars.push(Span::styled("█", Style::default().fg(MAUVE)));
                } else {
                    bar_chars.push(Span::styled("░", Style::default().fg(SURFACE_HIGH)));
                }
            }
            bar_chars.push(Span::styled(
                format!(" {:02}:{:02}/??:??", elapsed_min, elapsed_sec),
                Style::default().fg(TEXT_MUTED),
            ));
            f.render_widget(Paragraph::new(Line::from(bar_chars)), progress_area);
        } else {
            let bar_width = progress_area.width.saturating_sub(14) as usize;
            let bar_line = Line::from(vec![
                Span::styled(" ", Style::default()),
                Span::styled("░".repeat(bar_width), Style::default().fg(SURFACE_HIGH)),
                Span::styled(" --:--/--:--", Style::default().fg(TEXT_MUTED)),
            ]);
            f.render_widget(Paragraph::new(bar_line), progress_area);
        }

        let play_icon = if app.is_playing { "⏸" } else { "▶" };
        let controls = Paragraph::new(Span::styled(
            format!("🔀   ⏮    {}    ⏭    🔁", play_icon),
            Style::default().fg(TEXT_ACTIVE),
        ));
        f.render_widget(controls, inspector_layout[4]);
    }

    let mut log_lines = Vec::new();
    for log in &app.system_logs {
        log_lines.push(Line::from(Span::styled(log, Style::default().fg(TEXT_MUTED))));
    }
    f.render_widget(Paragraph::new(log_lines), inspector_layout[6]);

    // 4. BOTTOM BAR
    let bot_layout = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
        .split(main_layout[1]);

    let play_status = if !app.catalog.is_empty() {
        format!(" ▶ PLAYING: {}.WAV ", app.catalog[app.selected_track].track.to_uppercase())
    } else {
        " ▶ AWAITING_STREAM ".to_string()
    };

    f.render_widget(Paragraph::new(Span::styled(play_status, Style::default().fg(TEXT_ACTIVE).bg(SURFACE_HIGH))), inset(bot_layout[0], 0, 1));
    f.render_widget(Paragraph::new(Span::styled("⌨ COMMAND_PROMPT █", Style::default().fg(TEXT_ACTIVE))).alignment(Alignment::Right), inset(bot_layout[1], 0, 1));
}

fn inset(rect: Rect, dx: u16, dy: u16) -> Rect {
    Rect {
        x: rect.x.saturating_add(dx),
        y: rect.y.saturating_add(dy),
        width: rect.width.saturating_sub(dx * 2),
        height: rect.height.saturating_sub(dy * 2),
    }
}

/// Shared play logic — triggered by both keyboard Enter and mouse click.
fn handle_track_play(idx: usize, app: &mut App) {
    if idx >= app.catalog.len() { return; }

    app.kill_mpv();
    clear_kitty_images();
    app.cover_art_data = None;
    app.kitty_rendered = false;

    app.selected_track = idx;
    app.active_playing_track = Some(idx);
    app.is_playing = false;

    let track_name = app.catalog[idx].track.clone();
    let tenant = app.catalog[idx].tenant.clone();
    let file_key = app.catalog[idx].file_key.clone();
    let cover_key = app.catalog[idx].cover_key.clone();

    app.push_log(format!("[SYS] FETCHING PRE-SIGNED URL FOR: {}", track_name.to_uppercase()));

    // Fetch audio stream URL
    let tx = app.bg_tx.clone();
    let tenant_for_stream = tenant.clone();
    tokio::spawn(async move {
        let _ = tx.send(BackgroundEvent::Log("[SYS] BROKER HANDSHAKE...".to_string())).await;
        let output = tokio::process::Command::new("python")
            .arg("../backend.py")
            .arg("stream")
            .arg(&tenant_for_stream)
            .arg(&file_key)
            .output()
            .await;

        match output {
            Ok(out) => {
                let stdout = String::from_utf8_lossy(&out.stdout);
                match serde_json::from_str::<StreamResponse>(&stdout) {
                    Ok(resp) if resp.status == "success" => {
                        if let Some(url) = resp.url {
                            let _ = tx.send(BackgroundEvent::StreamReady(url)).await;
                        } else {
                            let _ = tx.send(BackgroundEvent::StreamError("NO URL IN RESPONSE".to_string())).await;
                        }
                    }
                    Ok(resp) => {
                        let msg = resp.message.unwrap_or("BROKER RETURNED ERROR".to_string());
                        let _ = tx.send(BackgroundEvent::StreamError(msg.to_uppercase())).await;
                    }
                    Err(_) => {
                        let _ = tx.send(BackgroundEvent::StreamError("INVALID JSON FROM BROKER".to_string())).await;
                    }
                }
            }
            Err(e) => {
                let _ = tx.send(BackgroundEvent::StreamError(format!("BROKER EXEC FAILED: {}", e))).await;
            }
        }
    });

    // Fetch cover art
    if cover_key != "NONE" && !cover_key.is_empty() {
        let tx2 = app.bg_tx.clone();
        let tenant2 = tenant.clone();
        let cover_key2 = cover_key.clone();
        tokio::spawn(async move {
            let _ = tx2.send(BackgroundEvent::Log("[SYS] FETCHING COVER ART...".to_string())).await;
            let output = tokio::process::Command::new("python")
                .arg("../backend.py")
                .arg("stream")
                .arg(&tenant2)
                .arg(&cover_key2)
                .output()
                .await;

            match output {
                Ok(out) => {
                    let stdout = String::from_utf8_lossy(&out.stdout);
                    if let Ok(resp) = serde_json::from_str::<StreamResponse>(&stdout) {
                        if resp.status == "success" {
                            if let Some(url) = resp.url {
                                match reqwest::get(&url).await {
                                    Ok(response) => {
                                        match response.bytes().await {
                                            Ok(bytes) => {
                                                let _ = tx2.send(BackgroundEvent::CoverArtReady(bytes.to_vec())).await;
                                            }
                                            Err(e) => {
                                                let _ = tx2.send(BackgroundEvent::Log(format!("[ERR] COVER DOWNLOAD FAILED: {}", e))).await;
                                            }
                                        }
                                    }
                                    Err(e) => {
                                        let _ = tx2.send(BackgroundEvent::Log(format!("[ERR] COVER FETCH FAILED: {}", e))).await;
                                    }
                                }
                            }
                        }
                    }
                }
                Err(e) => {
                    let _ = tx2.send(BackgroundEvent::Log(format!("[ERR] COVER BROKER FAILED: {}", e))).await;
                }
            }
        });
    }
}

fn handle_dashboard_input(key: KeyCode, app: &mut App) {
    match key {
        // --- SIDEBAR TAB SWITCHING ---
        KeyCode::Char('h') | KeyCode::Char('H') => { app.active_tab = SidebarTab::Home; }
        KeyCode::Char('s') | KeyCode::Char('S') => { app.active_tab = SidebarTab::Search; }
        KeyCode::Char('l') | KeyCode::Char('L') => { app.active_tab = SidebarTab::Library; }

        _ => {
            if app.catalog.is_empty() { return; }
            match key {
                KeyCode::Left  => { if app.selected_track > 0 { app.selected_track -= 1; } }
                KeyCode::Right => { if app.selected_track < app.catalog.len() - 1 { app.selected_track += 1; } }
                KeyCode::Up    => { if app.selected_track > 2 { app.selected_track -= 3; } }
                KeyCode::Down  => { if app.selected_track + 3 < app.catalog.len() { app.selected_track += 3; } }
                KeyCode::Enter => {
                    let idx = app.selected_track;
                    handle_track_play(idx, app);
                }
                KeyCode::Char(' ') => {
                    // Native MPV IPC pause/resume
                    if app.mpv_child.is_some() {
                        let tx = app.bg_tx.clone();
                        tokio::spawn(async move {
                            match UnixStream::connect("/tmp/termstream_mpv.sock").await {
                                Ok(mut stream) => {
                                    let cmd = "{\"command\":[\"cycle\",\"pause\"]}\n";
                                    let _ = stream.write_all(cmd.as_bytes()).await;
                                }
                                Err(e) => {
                                    let _ = tx.send(BackgroundEvent::Log(
                                        format!("[ERR] PAUSE IPC FAILED: {}", e)
                                    )).await;
                                }
                            }
                        });
                    }
                }
                _ => {}
            }
        }
    }
}

fn handle_mouse_input(mouse: MouseEvent, app: &mut App) {
    if mouse.kind != MouseEventKind::Down(MouseButton::Left) {
        return;
    }

    let col = mouse.column;
    let row = mouse.row;

    // Hit-test against cached grid cell rects (only populated while Home tab is active)
    for (idx, rect) in app.grid_cell_rects.iter().enumerate() {
        if col >= rect.x && col < rect.x + rect.width
            && row >= rect.y && row < rect.y + rect.height
        {
            if idx < app.catalog.len() {
                handle_track_play(idx, app);
            }
            return;
        }
    }
}