use crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::{Backend, CrosstermBackend},
    layout::{Alignment, Constraint, Direction, Layout},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Paragraph},
    Terminal,
};
use std::{error::Error, io};
use serde::Deserialize;
use std::process::Command;

// Catppuccin Mauve Dark Palette mapped to RGB
const MAUVE: Color = Color::Rgb(203, 166, 247);
const MAUVE_LIGHT: Color = Color::Rgb(226, 199, 255);
const TEXT_MUTED: Color = Color::Rgb(205, 195, 209);
const TEXT_ACTIVE: Color = Color::Rgb(227, 224, 247);

#[derive(PartialEq)]
enum AuthMode {
    Login,
    CreatorSignup,
    ListenerSignup,
}

#[derive(Deserialize, Debug)]
struct AuthResponse {
    status: String,
    message: Option<String>,
    username: Option<String>,
    artist_name: Option<String>,
    role: Option<String>,
    token: Option<String>,
}

#[derive(PartialEq)]
enum InputMode {
    Username,
    Password,
}

struct App {
    mode: AuthMode,
    input_mode: InputMode,
    username: String,
    password: String,
    system_message: String,
}

impl App {
    fn new() -> App {
        App {
            mode: AuthMode::Login,
            input_mode: InputMode::Username,
            username: String::new(),
            password: String::new(),
            system_message: String::from("[SYS] WAITING FOR CREDENTIALS..."),
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
    execute!(
        terminal.backend_mut(),
        LeaveAlternateScreen,
        DisableMouseCapture
    )?;
    terminal.show_cursor()?;

    if let Err(err) = res {
        println!("{:?}", err);
    }

    Ok(())
}

async fn run_app<B: Backend>(terminal: &mut Terminal<B>, app: &mut App) -> io::Result<()> {
    loop {
        terminal.draw(|f| {
            // 1. We use a transparent background (Color::Reset) 
            let size = f.size();
            let block = Block::default().style(Style::default().bg(Color::Reset));
            f.render_widget(block, size);

            // 2. Centering the UI using intentional void space
            let vertical_chunks = Layout::default()
                .direction(Direction::Vertical)
                .constraints([
                    Constraint::Percentage(30), // Top Void
                    Constraint::Length(15),     // UI Box
                    Constraint::Percentage(30), // Bottom Void
                ])
                .split(size);

            let horizontal_chunks = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([
                    Constraint::Percentage(30),
                    Constraint::Percentage(40), // Center column
                    Constraint::Percentage(30),
                ])
                .split(vertical_chunks[1]);

            let center_area = horizontal_chunks[1];

            let ui_chunks = Layout::default()
                .direction(Direction::Vertical)
                .constraints([
                    Constraint::Length(2), // Header
                    Constraint::Length(4), // Tabs
                    Constraint::Length(3), // Username
                    Constraint::Length(3), // Password
                    Constraint::Length(2), // Footer
                ])
                .split(center_area);

            // --- HEADER ---
            let header = Paragraph::new(Line::from(vec![
                Span::styled("TERMINAL_OS ", Style::default().fg(MAUVE).add_modifier(Modifier::BOLD)),
                Span::styled("// AUTHENTICATION", Style::default().fg(TEXT_MUTED)),
            ])).alignment(Alignment::Center);
            f.render_widget(header, ui_chunks[0]);

            // --- TABS (The Command Selection) ---
            let tabs_text = vec![
                Line::from(Span::styled(
                    if app.mode == AuthMode::Login { "▶ [ LOGIN ]" } else { "  LOGIN" },
                    Style::default().fg(if app.mode == AuthMode::Login { MAUVE_LIGHT } else { TEXT_MUTED }),
                )),
                Line::from(Span::styled(
                    if app.mode == AuthMode::CreatorSignup { "▶ [ CREATOR SIGN UP ]" } else { "  CREATOR SIGN UP" },
                    Style::default().fg(if app.mode == AuthMode::CreatorSignup { MAUVE_LIGHT } else { TEXT_MUTED }),
                )),
                Line::from(Span::styled(
                    if app.mode == AuthMode::ListenerSignup { "▶ [ LISTENER SIGN UP ]" } else { "  LISTENER SIGN UP" },
                    Style::default().fg(if app.mode == AuthMode::ListenerSignup { MAUVE_LIGHT } else { TEXT_MUTED }),
                )),
            ];
            let tabs = Paragraph::new(tabs_text).alignment(Alignment::Center);
            f.render_widget(tabs, ui_chunks[1]);

            // --- INPUT FIELDS (Monolithic Typography) ---
            
            // Username Input
            let user_prefix = if app.input_mode == InputMode::Username { ">> " } else { "   " };
            let user_color = if app.input_mode == InputMode::Username { MAUVE } else { TEXT_MUTED };
            
            let username_p = Paragraph::new(vec![
                Line::from(Span::styled("IDENTITY_ID", Style::default().fg(TEXT_MUTED))),
                Line::from(vec![
                    Span::styled(user_prefix, Style::default().fg(MAUVE_LIGHT).add_modifier(Modifier::BOLD)),
                    Span::styled(&app.username, Style::default().fg(TEXT_ACTIVE)),
                    if app.input_mode == InputMode::Username { Span::styled("█", Style::default().fg(MAUVE)) } else { Span::raw("") },
                ]),
            ]);
            f.render_widget(username_p, ui_chunks[2]);

            // Password Input (Masked)
            let pass_prefix = if app.input_mode == InputMode::Password { ">> " } else { "   " };
            let pass_color = if app.input_mode == InputMode::Password { MAUVE } else { TEXT_MUTED };
            let masked_pass = "*".repeat(app.password.len());

            let password_p = Paragraph::new(vec![
                Line::from(Span::styled("ACCESS_KEY", Style::default().fg(TEXT_MUTED))),
                Line::from(vec![
                    Span::styled(pass_prefix, Style::default().fg(MAUVE_LIGHT).add_modifier(Modifier::BOLD)),
                    Span::styled(masked_pass, Style::default().fg(TEXT_ACTIVE)),
                    if app.input_mode == InputMode::Password { Span::styled("█", Style::default().fg(MAUVE)) } else { Span::raw("") },
                ]),
            ]);
            f.render_widget(password_p, ui_chunks[3]);

            // --- FOOTER ---
            // --- SYSTEM MESSAGE ---
            let sys_color = if app.system_message.starts_with("[ERR]") { Color::Red } else { TEXT_MUTED };
            
            // Render it in the footer chunk, or create a new chunk if you prefer.
            // For now, let's just overwrite the footer chunk's first line so we don't have to rewrite the layout array
            let footer = Paragraph::new(vec![
                Line::from(Span::styled(&app.system_message, Style::default().fg(sys_color))),
                Line::from(""),
                Line::from(Span::styled(
                    "[TAB] Switch Mode   [UP/DOWN] Select Field   [ENTER] Execute   [ESC] Quit",
                    Style::default().fg(TEXT_MUTED),
                ))
            ]).alignment(Alignment::Center);
            f.render_widget(footer, ui_chunks[4]);

        })?;

        if event::poll(std::time::Duration::from_millis(16))? {
            if let Event::Key(key) = event::read()? {
                match key.code {
                    KeyCode::Esc => return Ok(()),
                    KeyCode::Tab => {
                        // Cycle through the Auth Modes
                        app.mode = match app.mode {
                            AuthMode::Login => AuthMode::CreatorSignup,
                            AuthMode::CreatorSignup => AuthMode::ListenerSignup,
                            AuthMode::ListenerSignup => AuthMode::Login,
                        };
                    }
                    KeyCode::Up | KeyCode::Down => {
                        // Switch between Username and Password fields
                        app.input_mode = match app.input_mode {
                            InputMode::Username => InputMode::Password,
                            InputMode::Password => InputMode::Username,
                        };
                    }
                    KeyCode::Enter => {
                        app.system_message = String::from("[SYS] AUTHENTICATING...");
                        
                        // Spawn the Python headless broker
                        let output = Command::new("python")
                            .arg("../backend.py")
                            .arg("login")
                            .arg(&app.username)
                            .arg(&app.password)
                            .output();

                        match output {
                            Ok(output) => {
                                // Capture the stdout from Python
                                let stdout = String::from_utf8_lossy(&output.stdout);
                                
                                // Try to parse the JSON
                                if let Ok(response) = serde_json::from_str::<AuthResponse>(&stdout) {
                                    if response.status == "success" {
                                        let role = response.role.unwrap_or_default();
                                        let name = response.artist_name.unwrap_or_default();
                                        app.system_message = format!("[SYS] ACCESS GRANTED: {} ({})", name, role);
                                        
                                        // TODO: Here is where we will route to the Main App UI next!
                                    } else {
                                        app.system_message = format!("[ERR] {}", response.message.unwrap_or("AUTH FAILED".to_string()).to_uppercase());
                                    }
                                } else {
                                    app.system_message = String::from("[ERR] INVALID BROKER RESPONSE");
                                }
                            }
                            Err(_) => {
                                app.system_message = String::from("[ERR] FAILED TO EXECUTE DATA BROKER");
                            }
                        }
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
                    _ => {}
                }
            }
        }
    }
}