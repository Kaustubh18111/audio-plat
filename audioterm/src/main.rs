use crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode},
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
// Events sent from async tasks back to the UI thread via mpsc channel.
enum BackgroundEvent {
    Log(String),
    StreamReady(String),   // Pre-signed URL
    StreamError(String),
    PlaybackProgress { elapsed: f64, duration: f64 },
    PlaybackPaused(bool),  // true = paused, false = playing
    PlaybackEnded,
    CoverArtReady(Vec<u8>), // Raw image bytes from S3
}

#[derive(Deserialize)]
struct StreamResponse {
    status: String,
    url: Option<String>,
    message: Option<String>,
}

// --- CATPPUCCIN PALETTE (Strict Match to DESIGN.md) ---
const MAUVE: Color = Color::Rgb(203, 166, 247);
const MAUVE_LIGHT: Color = Color::Rgb(226, 199, 255);
const TEXT_ACTIVE: Color = Color::Rgb(205, 214, 244);
const TEXT_MUTED: Color = Color::Rgb(166, 173, 200);
const SURFACE_LOW: Color = Color::Rgb(30, 30, 46);   // Darker background elements
const SURFACE_HIGH: Color = Color::Rgb(49, 50, 68);  // Active state backgrounds

// --- DATA STRUCTURES ---
#[derive(Deserialize, Debug)]
struct AuthResponse {
    status: String,
    message: Option<String>,
    artist_name: Option<String>,
    role: Option<String>,
}

#[derive(Deserialize, Debug, Clone)]
struct Track {
    id: String,
    track: String,
    artist: String,
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
    /// Cached terminal rect of the inspector art box for Kitty rendering
    inspector_art_rect: Option<Rect>,
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
        }
    }

    /// Kill any running mpv process gracefully.
    fn kill_mpv(&mut self) {
        // Cancel IPC poller first
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
    }

    /// Spawn mpv headlessly with the given pre-signed URL.
    fn spawn_mpv(&mut self, url: &str) {
        self.kill_mpv();
        // Remove stale socket before spawning
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

    /// Spawn the IPC observer task that listens for property-change events from mpv.
    fn start_ipc_observer(&mut self) {
        // Cancel any previous observer
        if let Some(cancel) = self.ipc_cancel.take() {
            let _ = cancel.send(true);
        }

        let (cancel_tx, cancel_rx) = watch::channel(false);
        self.ipc_cancel = Some(cancel_tx);
        let tx = self.bg_tx.clone();

        tokio::spawn(async move {
            // Retry connection — mpv takes a moment to create the socket
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

            // --- REGISTER PROPERTY OBSERVERS ---
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

            // Track the latest known values
            let mut last_elapsed: f64 = 0.0;
            let mut last_duration: f64 = 0.0;

            // --- LISTEN FOR EVENTS ---
            loop {
                let mut line = String::new();

                tokio::select! {
                    result = buf_reader.read_line(&mut line) => {
                        match result {
                            Ok(0) => {
                                // EOF — mpv closed the socket
                                let _ = tx.send(BackgroundEvent::PlaybackEnded).await;
                                return;
                            }
                            Ok(_) => {
                                if let Ok(val) = serde_json::from_str::<serde_json::Value>(&line) {
                                    // Handle property-change events
                                    if val.get("event").and_then(|v| v.as_str()) == Some("property-change") {
                                        let id = val.get("id").and_then(|v| v.as_u64()).unwrap_or(0);
                                        match id {
                                            1 => {
                                                // time-pos
                                                if let Some(data) = val.get("data").and_then(|v| v.as_f64()) {
                                                    last_elapsed = data;
                                                    let _ = tx.send(BackgroundEvent::PlaybackProgress {
                                                        elapsed: last_elapsed,
                                                        duration: last_duration,
                                                    }).await;
                                                }
                                            }
                                            2 => {
                                                // duration
                                                if let Some(data) = val.get("data").and_then(|v| v.as_f64()) {
                                                    last_duration = data;
                                                    // Re-dispatch with updated duration
                                                    let _ = tx.send(BackgroundEvent::PlaybackProgress {
                                                        elapsed: last_elapsed,
                                                        duration: last_duration,
                                                    }).await;
                                                }
                                            }
                                            3 => {
                                                // pause state
                                                if let Some(paused) = val.get("data").and_then(|v| v.as_bool()) {
                                                    let _ = tx.send(BackgroundEvent::PlaybackPaused(paused)).await;
                                                }
                                            }
                                            _ => {}
                                        }
                                    }
                                    // Handle end-file event
                                    else if val.get("event").and_then(|v| v.as_str()) == Some("end-file") {
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

    // Helper to push logs and keep the queue clean (max 5 items)
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
/// Render image bytes directly to the terminal using the Kitty Graphics Protocol.
/// `col` and `row` are the 1-based terminal cell coordinates for placement.
/// `cols` and `rows` constrain the image to that many cells.
fn render_kitty_image(image_bytes: &[u8], col: u16, row: u16, cols: u16, rows: u16) {
    let b64 = base64::engine::general_purpose::STANDARD.encode(image_bytes);
    let chunk_size = 4096;
    let mut stdout = io::stdout();

    // Move cursor to the target cell position
    let _ = write!(stdout, "\x1b[{};{}H", row, col);

    for (i, chunk) in b64.as_bytes().chunks(chunk_size).enumerate() {
        let chunk_str = std::str::from_utf8(chunk).unwrap_or("");
        let more = if (i + 1) * chunk_size < b64.len() { 1 } else { 0 };

        if i == 0 {
            // a=T (Transmit+Display), f=100 (PNG), c=columns, r=rows, q=2 (quiet)
            let _ = write!(
                stdout,
                "\x1b_Ga=T,f=100,c={},r={},q=2,m={};{}\x1b\\",
                cols, rows, more, chunk_str
            );
        } else {
            let _ = write!(stdout, "\x1b_Gm={};{}\x1b\\", more, chunk_str);
        }
    }

    let _ = stdout.flush();
}

/// Clear all Kitty graphics from the terminal.
fn clear_kitty_images() {
    let mut stdout = io::stdout();
    // a=d, d=a — delete all images
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

    // Clean up Kitty images before leaving
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

        // --- RENDER KITTY GRAPHICS AFTER FRAME FLUSH ---
        if app.state == AppState::Dashboard {
            if let (Some(ref data), Some(rect)) = (&app.cover_art_data, app.inspector_art_rect) {
                if rect.width > 0 && rect.height > 0 {
                    render_kitty_image(
                        data,
                        rect.x + 1, // terminal coords are 1-based
                        rect.y + 1,
                        rect.width,
                        rect.height,
                    );
                }
            }
        }

        if event::poll(std::time::Duration::from_millis(16))? {
            if let Event::Key(key) = event::read()? {
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
            Constraint::Length(2), // Header
            Constraint::Length(4), // Tabs
            Constraint::Length(3), // User
            Constraint::Length(3), // Pass
            Constraint::Length(3), // Footer/Msg
        ])
        .split(horizontal_chunks[1]);

    // Header
    let header = Paragraph::new(Line::from(vec![
        Span::styled("TERMINAL_OS ", Style::default().fg(MAUVE).add_modifier(Modifier::BOLD)),
        Span::styled("// AUTH", Style::default().fg(TEXT_MUTED)),
    ])).alignment(Alignment::Center);
    f.render_widget(header, ui_chunks[0]);

    // Tabs
    let tabs = Paragraph::new(vec![
        Line::from(Span::styled(if app.auth_mode == AuthMode::Login { "▶ [ LOGIN ]" } else { "  LOGIN" }, Style::default().fg(if app.auth_mode == AuthMode::Login { MAUVE_LIGHT } else { TEXT_MUTED }))),
        Line::from(Span::styled(if app.auth_mode == AuthMode::CreatorSignup { "▶ [ CREATOR ]" } else { "  CREATOR" }, Style::default().fg(if app.auth_mode == AuthMode::CreatorSignup { MAUVE_LIGHT } else { TEXT_MUTED }))),
        Line::from(Span::styled(if app.auth_mode == AuthMode::ListenerSignup { "▶ [ LISTENER ]" } else { "  LISTENER" }, Style::default().fg(if app.auth_mode == AuthMode::ListenerSignup { MAUVE_LIGHT } else { TEXT_MUTED }))),
    ]).alignment(Alignment::Center);
    f.render_widget(tabs, ui_chunks[1]);

    // Inputs
    let u_prefix = if app.input_mode == InputMode::Username { ">> " } else { "   " };
    let p_prefix = if app.input_mode == InputMode::Password { ">> " } else { "   " };
    let masked_pass = "*".repeat(app.password.len());

    let user_p = Paragraph::new(vec![
        Line::from(Span::styled("IDENTITY", Style::default().fg(TEXT_MUTED))),
        Line::from(vec![Span::styled(u_prefix, Style::default().fg(MAUVE_LIGHT)), Span::styled(&app.username, Style::default().fg(TEXT_ACTIVE)), if app.input_mode == InputMode::Username { Span::styled("█", Style::default().fg(MAUVE)) } else { Span::raw("") }]),
    ]);
    f.render_widget(user_p, ui_chunks[2]);

    let pass_p = Paragraph::new(vec![
        Line::from(Span::styled("ACCESS_KEY", Style::default().fg(TEXT_MUTED))),
        Line::from(vec![Span::styled(p_prefix, Style::default().fg(MAUVE_LIGHT)), Span::styled(masked_pass, Style::default().fg(TEXT_ACTIVE)), if app.input_mode == InputMode::Password { Span::styled("█", Style::default().fg(MAUVE)) } else { Span::raw("") }]),
    ]);
    f.render_widget(pass_p, ui_chunks[3]);

    // System Msg
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
            app.input_mode = match app.input_mode { InputMode::Username => InputMode::Password, InputMode::Password => InputMode::Username };
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
            
            // HEADLESS BROKER EXECUTION
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
                        
                        // TRANSITION STATE!
                        app.state = AppState::Dashboard;
                        app.fetch_catalog(); 
                        
                    } else {
                        app.system_message = format!("[ERR] {}", response.message.unwrap_or("AUTH FAILED".to_string()).to_uppercase());
                    }
                } else { app.system_message = String::from("[ERR] INVALID JSON RESPONSE"); }
            } else { app.system_message = String::from("[ERR] BROKER FAILED"); }
        }
        _ => {}
    }
}

// --- DASHBOARD RENDERER (THE SILENT COMMAND) ---
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

    let nav_links = Paragraph::new(vec![
        Line::from(""),
        Line::from(Span::styled(" ⌂ HOME [H]", Style::default().fg(TEXT_ACTIVE).bg(SURFACE_HIGH))),
        Line::from(""),
        Line::from(Span::styled(" Q SEARCH [S]", Style::default().fg(TEXT_MUTED))),
        Line::from(""),
        Line::from(Span::styled(" ≡ LIBRARY [L]", Style::default().fg(TEXT_MUTED))),
    ]);
    f.render_widget(nav_links, sidebar_layout[1]);

    let new_buffer_btn = Paragraph::new(Span::styled(
        "    + NEW_BUFFER    ", 
        Style::default().fg(MAUVE).bg(SURFACE_LOW)
    )).alignment(Alignment::Center);
    f.render_widget(new_buffer_btn, sidebar_layout[3]);


    // 2. MAIN GRID
    let grid_layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(4), Constraint::Min(0)])
        .split(columns[1]);

    let grid_header = Paragraph::new(vec![
        Line::from(Span::styled("PROMPT: ./FETCH_TRENDING_DATA", Style::default().fg(TEXT_MUTED))),
        Line::from(Span::styled("ACTIVE_ALBUMS", Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD))),
    ]);
    f.render_widget(grid_header, grid_layout[0]);

    let rows = Layout::default().direction(Direction::Vertical).constraints([Constraint::Percentage(50), Constraint::Percentage(50)]).split(grid_layout[1]);
    for r in 0..2 {
        let cols = Layout::default().direction(Direction::Horizontal).constraints([Constraint::Percentage(33), Constraint::Percentage(33), Constraint::Percentage(33)]).split(rows[r]);
        for c in 0..3 {
            let idx = (r * 3) + c;
            if idx < app.catalog.len() {
                let track = &app.catalog[idx];
                let is_selected = app.selected_track == idx;
                
                let title_style = if is_selected { Style::default().fg(MAUVE).add_modifier(Modifier::BOLD) } else { Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD) };
                
                let item_layout = Layout::default().direction(Direction::Vertical).constraints([Constraint::Length(12), Constraint::Length(3)]).split(cols[c]);
                
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

    // 3. INSPECTOR
    let inspector_layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2),   // [0] Status header
            Constraint::Length(16),  // [1] Art box
            Constraint::Length(4),   // [2] Track metadata
            Constraint::Length(2),   // [3] Progress bar
            Constraint::Length(3),   // [4] Controls
            Constraint::Min(0),      // [5] spacer
            Constraint::Length(5),   // [6] System logs
        ]).split(columns[2]);

    // Dynamic Header
    let status_text = if app.is_playing { "● NOW_PLAYING" } else if app.active_playing_track.is_some() { "⏸ PAUSED" } else { "● STANDBY" };
    let status_color = if app.is_playing { MAUVE } else { TEXT_MUTED };
    f.render_widget(Paragraph::new(Span::styled(status_text, Style::default().fg(status_color))), inspector_layout[0]);
    
    // Art Box — render the background block and store rect for Kitty rendering
    let art_rect = inset(inspector_layout[1], 0, 1);
    f.render_widget(Block::default().style(Style::default().bg(SURFACE_LOW)), art_rect);
    app.inspector_art_rect = Some(art_rect);

    // Dynamic Metadata based on ACTIVE track (not just selected)
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
            let bar_width = progress_area.width.saturating_sub(14) as usize; // reserve space for timestamps
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
            // Duration unknown — show elapsed time counting up with pulsing bar
            let bar_width = progress_area.width.saturating_sub(14) as usize;
            let elapsed_min = (app.playback_elapsed as u64) / 60;
            let elapsed_sec = (app.playback_elapsed as u64) % 60;

            // Animate a pulse: a small block that moves across the bar
            let pulse_pos = ((app.playback_elapsed as usize) * 2) % bar_width.max(1);
            let mut bar_chars: Vec<Span> = Vec::new();
            bar_chars.push(Span::styled(" ", Style::default()));
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

        // Controls
        let play_icon = if app.is_playing { "⏸" } else { "▶" };
        let controls = Paragraph::new(Span::styled(format!("🔀   ⏮    {}    ⏭    🔁", play_icon), Style::default().fg(TEXT_ACTIVE)));
        f.render_widget(controls, inspector_layout[4]);
    }

    // Dynamic System Logs
    let mut log_lines = Vec::new();
    for log in &app.system_logs {
        log_lines.push(Line::from(Span::styled(log, Style::default().fg(TEXT_MUTED))));
    }
    let logs_widget = Paragraph::new(log_lines);
    f.render_widget(logs_widget, inspector_layout[6]);

    // 4. BOTTOM BAR
    let bot_layout = Layout::default().direction(Direction::Horizontal).constraints([Constraint::Percentage(50), Constraint::Percentage(50)]).split(main_layout[1]);
    
    let play_status = if !app.catalog.is_empty() {
        format!(" ▶ PLAYING: {}.WAV ", app.catalog[app.selected_track].track.to_uppercase())
    } else { " ▶ AWAITING_STREAM ".to_string() };

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

fn handle_dashboard_input(key: KeyCode, app: &mut App) {
    if app.catalog.is_empty() { return; }
    match key {
        KeyCode::Left =>  { if app.selected_track > 0 { app.selected_track -= 1; } }
        KeyCode::Right => { if app.selected_track < app.catalog.len() - 1 { app.selected_track += 1; } }
        KeyCode::Up =>    { if app.selected_track > 2 { app.selected_track -= 3; } }
        KeyCode::Down =>  { if app.selected_track + 3 < app.catalog.len() { app.selected_track += 3; } }
        
        // --- MPV DAEMON CONTROLS ---
        KeyCode::Enter => {
            // Kill previous stream if any
            app.kill_mpv();
            // Clear previous cover art
            clear_kitty_images();
            app.cover_art_data = None;

            app.active_playing_track = Some(app.selected_track);
            app.is_playing = false; // Will be set to true when mpv actually spawns

            let track = &app.catalog[app.selected_track];
            let track_name = track.track.clone();
            let tenant = track.tenant.clone();
            let file_key = track.file_key.clone();
            let cover_key = track.cover_key.clone();
            // Drop the immutable borrow on `track` before the mutable push_log call
            drop(track);

            app.push_log(format!("[SYS] FETCHING PRE-SIGNED URL FOR: {}", track_name.to_uppercase()));

            // Fire async task to fetch the S3 URL from the Python broker
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

            // --- FETCH COVER ART (Kitty Graphics) ---
            if cover_key != "NONE" && !cover_key.is_empty() {
                let tx2 = app.bg_tx.clone();
                let tenant2 = tenant.clone();
                let cover_key2 = cover_key.clone();
                tokio::spawn(async move {
                    let _ = tx2.send(BackgroundEvent::Log("[SYS] FETCHING COVER ART...".to_string())).await;

                    // Get presigned URL for cover image via backend broker
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
                                        // Download the image bytes
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
        KeyCode::Char(' ') => {
            // --- NATIVE MPV IPC PAUSE/RESUME ---
            if app.mpv_child.is_some() {
                let tx = app.bg_tx.clone();
                tokio::spawn(async move {
                    match UnixStream::connect("/tmp/termstream_mpv.sock").await {
                        Ok(mut stream) => {
                            let cmd = "{\"command\":[\"cycle\",\"pause\"]}\n";
                            let _ = stream.write_all(cmd.as_bytes()).await;
                        }
                        Err(e) => {
                            let _ = tx.send(BackgroundEvent::Log(format!("[ERR] PAUSE IPC FAILED: {}", e))).await;
                        }
                    }
                });
            }
        }
        _ => {}
    }
}