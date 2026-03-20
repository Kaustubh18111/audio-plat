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
use std::{error::Error, io, process::Command};

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
    
    // --- NEW: DYNAMIC PLAYBACK STATE ---
    active_playing_track: Option<usize>, // Tracks what is actually playing vs just hovered
    is_playing: bool,
    system_logs: Vec<String>,            // Dynamic log queue
}

impl App {
    fn new() -> App {
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
            
            // Initialize new state
            active_playing_track: None,
            is_playing: false,
            system_logs: vec![
                String::from("[SYS] KERNEL BOOT..."),
                String::from("[SYS] AWAITING AUTHENTICATION"),
            ],
        }
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

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let mut app = App::new();
    let res = run_app(&mut terminal, &mut app).await;

    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen, DisableMouseCapture)?;
    terminal.show_cursor()?;
    if let Err(err) = res { println!("{:?}", err); }
    Ok(())
}

async fn run_app<B: Backend>(terminal: &mut Terminal<B>, app: &mut App) -> io::Result<()> {
    loop {
        terminal.draw(|f| {
            // Root transparent block
            f.render_widget(Block::default().style(Style::default().bg(Color::Reset)), f.size());
            match app.state {
                AppState::Auth => draw_auth(f, app),
                AppState::Dashboard => draw_dashboard(f, app),
            }
        })?;

        if event::poll(std::time::Duration::from_millis(16))? {
            if let Event::Key(key) = event::read()? {
                if key.code == KeyCode::Esc { return Ok(()); }
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
fn draw_dashboard(f: &mut ratatui::Frame, app: &App) {
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
            Constraint::Length(2),  
            Constraint::Length(16), 
            Constraint::Length(4),  
            Constraint::Length(2),  
            Constraint::Length(3),  
            Constraint::Min(0),     
            Constraint::Length(5),  
        ]).split(columns[2]);

    // Dynamic Header
    let status_text = if app.is_playing { "● NOW_PLAYING" } else if app.active_playing_track.is_some() { "⏸ PAUSED" } else { "● STANDBY" };
    let status_color = if app.is_playing { MAUVE } else { TEXT_MUTED };
    f.render_widget(Paragraph::new(Span::styled(status_text, Style::default().fg(status_color))), inspector_layout[0]);
    
    // Art Box
    f.render_widget(Block::default().style(Style::default().bg(SURFACE_LOW)), inset(inspector_layout[1], 0, 1));

    // Dynamic Metadata based on ACTIVE track (not just selected)
    let display_idx = app.active_playing_track.unwrap_or(app.selected_track);
    if !app.catalog.is_empty() {
        let t = &app.catalog[display_idx];
        let meta_large = Paragraph::new(vec![
            Line::from(Span::styled(t.track.to_uppercase(), Style::default().fg(TEXT_ACTIVE).add_modifier(Modifier::BOLD))),
            Line::from(Span::styled(t.artist.to_uppercase(), Style::default().fg(MAUVE))),
        ]);
        f.render_widget(meta_large, inspector_layout[2]);

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
        
        // --- NEW: DYNAMIC CONTROLS ---
        KeyCode::Enter => {
            app.active_playing_track = Some(app.selected_track);
            app.is_playing = true;
            let track_name = &app.catalog[app.selected_track].track;
            app.push_log(format!("[SYS] FETCHING PRE-SIGNED URL FOR: {}", track_name.to_uppercase()));
            app.push_log(format!("[SYS] INITIALIZING MPV DAEMON..."));
        }
        KeyCode::Char(' ') => {
            if app.active_playing_track.is_some() {
                app.is_playing = !app.is_playing;
                let status = if app.is_playing { "RESUMED" } else { "PAUSED" };
                app.push_log(format!("[SYS] STREAM {}", status));
            }
        }
        _ => {}
    }
}