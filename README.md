Audio Platform: V4 Edge Architecture
System Overview

This project is a high-performance, multi-tenant audio streaming platform designed entirely for the terminal. It leverages a custom "Headless CLI" architecture, decoupling a highly optimized Rust presentation layer from a Python-based AWS microservice backend.

The system relies on Amazon Web Services (AWS) for core infrastructure, utilizing Cognito for identity management, DynamoDB for NoSQL metadata tracking, and S3 for encrypted audio and image storage.
Architectural Updates & Process Flow (V4 Pivot)

The platform has recently undergone a major architectural refactor to bypass terminal 60FPS DOM limitations and improve the ingestion pipeline.
1. The Headless CLI Architecture

To achieve native GPU hardware acceleration for album artwork without sacrificing interactive UI elements, the system is split into two distinct binaries:

    The Presentation Layer (Rust/Ratatui): A lightweight, memory-safe compiled binary. Its sole responsibility is drawing the split-pane grid, managing the ratatui state, and pushing Kitty Graphics Protocol byte streams directly to the terminal emulator. It contains no cloud logic.

    The Data Broker (Python): A backend microservice (backend.py) that handles all boto3 AWS SDK interactions. The Rust frontend executes this Python script invisibly, captures its standard output as a strict JSON string, and deserializes it into Rust structs for UI rendering.

2. Streamlined V4 Ingestion Engine

The client.py producer portal has been rewritten to eliminate redundant data entry:

    Persistent User Profiles: Artist bio and stage names are requested only once during initial Cognito registration. This data is stored as a UserProfile schema in DynamoDB using TenantID as the Partition Key and SongID as the Sort Key.

    Automated ID3 Extraction: The ingestion loop now utilizes tinytag to read embedded ID3 metadata directly from .wav, .mp3, and .flac files. Track names, release titles, and featured artist credits are automatically parsed and staged for DynamoDB upload, requiring only a single keystroke confirmation from the user.

Tech Stack

    Cloud Infrastructure: AWS S3, AWS DynamoDB, AWS Cognito

    Backend / Ingestion: Python 3.x, boto3, tinytag

    Frontend / TUI: Rust, ratatui, tokio, crossterm

    Media Handling: mpv (Headless audio streaming), Kitty Graphics Protocol (GPU Image Rendering)

Prerequisites

To run this project locally, your environment must have the following installed:

    Python 3.10+

    Rust (Cargo)

    AWS CLI (Configured with ap-south-1 region credentials)

    mpv media player installed on the system PATH

    A terminal emulator that supports the Kitty Graphics Protocol (e.g., Ghostty, Kitty)

Installation & Setup

    Clone and Configure the Python Environment:

Bash

# Navigate to the Python backend directory
cd audio-platform

# Create and activate the virtual environment
python -m venv .venv
source .venv/bin/activate

# Install required Python dependencies
pip install boto3 tinytag

    Compile the Rust Frontend:

Bash

# Navigate to the Rust TUI directory
cd audioterm

# Build the release binary
cargo build --release

System Modules & Execution
1. Producer Portal (client.py)

Used by artists to authenticate and upload music to the platform.

    Executes Cognito registration and login flows.

    Scans local directories for audio files.

    Extracts metadata via tinytag and uploads assets to S3.

    Commits V4 schema records to DynamoDB.

    Usage: python client.py

2. The Data Broker (backend.py)

A headless microservice. It is not meant to be interacted with directly by the user. It is called internally by the Rust binary to fetch the global catalog from DynamoDB and return it as a JSON payload.

    Usage: python backend.py catalog

3. Listener Portal (audioterm)

The main interactive terminal user interface. It renders the global catalog, listens for keyboard events, and orchestrates the S3 stream and native GPU artwork rendering.

    Usage: cargo run (Development) or ./target/release/audioterm (Production)

4. System Purge (reset.py)

A developer utility to completely format the cloud environment. It iterates through the Cognito User Pool, drops all registered users via admin overrides, deletes all objects in the S3 bucket, and purges all records in the DynamoDB table.

    Usage: python reset.py

    Warning: This action is irreversible.

Database Schema (DynamoDB)

The AudioMetadataTable utilizes a single-table design pattern.

    Partition Key: TenantID (String) - Maps to the Cognito Username.

    Sort Key: SongID (String) - Maps to either the unique track filename or standard identifiers like PROFILE_DATA.
