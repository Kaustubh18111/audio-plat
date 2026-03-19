import boto3
import subprocess
import sys
import os
import requests
import time
import base64
from thefuzz import process
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

# --- CLOUD CONFIGURATION ---
BUCKET_NAME = "audioplatformstack-audiostoragebucketd8d3b0dc-qfiv3hvchgq4"
s3 = boto3.client('s3', region_name='ap-south-1')
dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
console = Console()

def get_table():
    client = boto3.client('dynamodb', region_name='ap-south-1')
    for t in client.list_tables()['TableNames']:
        if 'AudioMetadataTable' in t: return dynamodb.Table(t)
    console.print("[red][-] Database not found![/red]")
    sys.exit(1)

table = get_table()

def fetch_duration(audio_url):
    """Uses ffprobe to grab the exact millisecond duration of the stream"""
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_url]
        return float(subprocess.check_output(cmd, text=True).strip())
    except Exception:
        return 0

def render_ghostty_gpu(img_path):
    """
    Forces Ghostty to render graphics by converting S3 images into 
    pristine, chunked PNG payloads using the Kitty Protocol.
    """
    import base64
    import io
    from PIL import Image

    try:
        # 1. Intercept the JPEG and forcefully convert it to PNG in RAM
        with Image.open(img_path) as img:
            # Resize to a sane resolution so we don't crash the terminal with a 4K payload
            img.thumbnail((400, 400)) 
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            png_data = buffer.getvalue()
            
        b64 = base64.standard_b64encode(png_data).decode('ascii')
        chunk_size = 4096
        
        sys.stdout.write("\n") # Breathing room top
        
        # 2. Blast the PNG binary chunks to Ghostty
        for i in range(0, len(b64), chunk_size):
            chunk = b64[i:i+chunk_size]
            m = 1 if i + chunk_size < len(b64) else 0
            
            if i == 0:
                # a=T (Transmit) | f=100 (PNG) | r=20 (Force 20 rows tall) | q=2 (Quiet mode)
                sys.stdout.write(f"\033_Ga=T,f=100,r=20,q=2,m={m};{chunk}\033\\")
            else:
                sys.stdout.write(f"\033_Gm={m};{chunk}\033\\")
        
        # 3. Move the text cursor DOWN so the Rich panel doesn't overwrite the GPU image
        sys.stdout.write("\n" * 21) 
        sys.stdout.flush()
        
    except Exception as e:
        console.print(f"[red]GPU Memory Error: {e}[/red]")

def stream_audio(track_data):
    os.system('clear' if os.name == 'posix' else 'cls')
    
    # 1. Image Download & Raw GPU Render
    cover_key = track_data.get('CoverKey')
    if cover_key and cover_key != "NONE":
        img_path = f"/tmp/{cover_key}"
        if os.path.exists(img_path) and os.path.getsize(img_path) < 1000:
            os.remove(img_path) 
            
        if not os.path.exists(img_path):
            with console.status("[bold cyan]Downloading High-Res Artwork from S3..."):
                url = s3.generate_presigned_url('get_object', Params={'Bucket': BUCKET_NAME, 'Key': f"{track_data['TenantID']}/{cover_key}"})
                res = requests.get(url)
                if res.status_code == 200:
                    with open(img_path, 'wb') as f: f.write(res.content)
        
        if os.path.exists(img_path):
            try:
                # Fire the custom GPU protocol payload
                render_ghostty_gpu(img_path)
            except Exception as e:
                console.print(f"[red]Raw GPU Render Error: {e}[/red]")
    
    # 2. Track Metadata Panel
    console.print(Panel.fit(
        f"[bold white]{track_data['TrackName']}[/bold white]\n"
        f"[cyan]{track_data['Artist']}[/cyan] | [yellow]{track_data['ReleaseName']}[/yellow]\n"
        f"[dim]Streaming live from AWS Edge Node[/dim]",
        border_style="magenta"
    ))

    # 3. Stream & Track Progress
    audio_url = s3.generate_presigned_url('get_object', Params={'Bucket': BUCKET_NAME, 'Key': f"{track_data['TenantID']}/{track_data['FileName']}"})
    
    with console.status("[bold green]Probing AWS stream for duration..."):
        duration = fetch_duration(audio_url)

    mpv_process = subprocess.Popen(['mpv', '--no-video', '--msg-level=all=no', audio_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        if duration > 0:
            with Progress(
                TextColumn("[cyan]▶ NOW PLAYING"),
                BarColumn(bar_width=40, style="magenta", complete_style="cyan"),
                TimeElapsedColumn(),
                TextColumn("/"),
                TimeRemainingColumn(),
                console=console
            ) as progress:
                task = progress.add_task("Playing", total=duration)
                while mpv_process.poll() is None:
                    time.sleep(1)
                    progress.advance(task, 1)
        else:
            console.print("[yellow]Playing (Live Stream - Unknown Duration)... Press Ctrl+C to stop.[/yellow]")
            mpv_process.wait()
            
    except KeyboardInterrupt:
        mpv_process.kill()
        console.print("\n[yellow]Playback stopped.[/yellow]")
        time.sleep(1)

def display_library():
    with console.status("[bold yellow]Hydrating V4 Global Catalog from DynamoDB..."):
        items = table.scan().get('Items', [])
        
    search_index = {}
    for item in items:
        if item.get('Schema') != 'V4': continue
        track, artist, release = item.get('TrackName'), item.get('Artist'), item.get('ReleaseName')
        search_index[f"{track} {artist} {release}"] = item

    if not search_index:
        console.print("[red]No V4 audio tracks found in the database.[/red]")
        sys.exit(0)

    while True:
        os.system('clear' if os.name == 'posix' else 'cls')
        console.print(Panel.fit("[bold magenta]🌐 GLOBAL MUSIC BROWSER[/bold magenta]"))
        
        query = Prompt.ask("\n[bold cyan]Search Artist, Album, or Track (or 'q' to quit)[/bold cyan]")
        if query.lower() == 'q': sys.exit(0)
            
        results = process.extract(query, search_index.keys(), limit=10)
        
        ui_table = Table(title="Search Results", show_header=True, header_style="bold magenta")
        ui_table.add_column("ID", style="dim", width=4)
        ui_table.add_column("Track", style="bold white")
        ui_table.add_column("Artist", style="cyan")
        ui_table.add_column("Release", style="yellow")
        ui_table.add_column("Match %", justify="right", style="green")

        match_list = []
        for idx, (match_string, score) in enumerate(results):
            data = search_index[match_string]
            match_list.append(data)
            ui_table.add_row(str(idx + 1), data['TrackName'], data['Artist'], data['ReleaseName'], f"{score}%")

        console.print(ui_table)
        
        choice = Prompt.ask("\n[bold cyan]Select Track ID to Stream[/bold cyan] (hit Enter to search again)", default="")
        if not choice: continue
            
        try:
            selected = match_list[int(choice) - 1]
            stream_audio(selected)
        except (ValueError, IndexError):
            pass

if __name__ == "__main__":
    display_library()