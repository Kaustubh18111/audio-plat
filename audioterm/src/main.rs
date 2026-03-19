use crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::{Backend, CrosstermBackend},
    layout::{Constraint, Direction, Layout},
    style::{Color, Modifier, Style},
    widgets::{Block, Borders, List, ListItem, ListState},
    Terminal,
};
use serde::Deserialize;
use std::{error::Error, io, process::Command};

// 1. We strictly type the JSON coming from Python
#[derive(Deserialize, Clone, Debug)]
struct Track {
    id: String,
    track: String,
    artist: String,
    release: String,
    tenant: String,
    file_key: String,
    cover_key: String,
}

// 2. The function that secretly runs your Python backend
fn fetch_catalog_from_python() -> Vec<Track> {
    // We use your exact virtual environment so boto3 works
    let python_bin = "/home/kaustubh/audio-platform/.venv/bin/python";
    let script_path = "/home/kaustubh/audio-platform/backend.py";

    let output = Command::new(python_bin)
        .arg(script_path)
        .arg("catalog")
        .output()
        .expect("Failed to execute Python backend");

    // Catch the JSON and parse it into our strict Rust structs
    let json_str = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(&json_str).unwrap_or_else(|_| vec![])
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    // Boot up sequence: Fetch the catalog before we draw the UI
    let tracks = fetch_catalog_from_python();
    
    // Set up the selection state (so we can scroll up and down)
    let mut list_state = ListState::default();
    if !tracks.is_empty() {
        list_state.select(Some(0)); // Highlight the first track
    }

    let res = run_app(&mut terminal, tracks, list_state).await;

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

async fn run_app<B: Backend>(
    terminal: &mut Terminal<B>,
    tracks: Vec<Track>,
    mut list_state: ListState,
) -> io::Result<()> {
    loop {
        terminal.draw(|f| {
            let chunks = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Percentage(40), Constraint::Percentage(60)].as_ref())
                .split(f.size());

            // 3. Build the interactive list for the Left Pane
            let items: Vec<ListItem> = tracks
                .iter()
                .map(|t| {
                    ListItem::new(format!("🎵 {} - {}", t.artist, t.track))
                })
                .collect();

            let catalog_list = List::new(items)
                .block(
                    Block::default()
                        .title(" 🌐 GLOBAL CATALOG ")
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(Color::Magenta)),
                )
                .highlight_style(
                    Style::default()
                        .bg(Color::Cyan)
                        .fg(Color::Black)
                        .add_modifier(Modifier::BOLD),
                )
                .highlight_symbol(">> ");

            f.render_stateful_widget(catalog_list, chunks[0], &mut list_state);

            // Right Pane (Placeholder for now)
            let right_pane = Block::default()
                .title(" ▶ NOW PLAYING ")
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Cyan));
            f.render_widget(right_pane, chunks[1]);
        })?;

        if event::poll(std::time::Duration::from_millis(16))? {
            if let Event::Key(key) = event::read()? {
                match key.code {
                    KeyCode::Char('q') => return Ok(()),
                    KeyCode::Down => {
                        if !tracks.is_empty() {
                            let i = match list_state.selected() {
                                Some(i) => {
                                    if i >= tracks.len() - 1 { 0 } else { i + 1 }
                                }
                                None => 0,
                            };
                            list_state.select(Some(i));
                        }
                    }
                    KeyCode::Up => {
                        if !tracks.is_empty() {
                            let i = match list_state.selected() {
                                Some(i) => {
                                    if i == 0 { tracks.len() - 1 } else { i - 1 }
                                }
                                None => 0,
                            };
                            list_state.select(Some(i));
                        }
                    }
                    _ => {}
                }
            }
        }
    }
}