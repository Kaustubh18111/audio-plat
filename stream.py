import boto3
import subprocess
import sys
import os
import requests
from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Tree, Static, Label, ProgressBar
from textual.containers import Horizontal, Vertical
from term_image.image import from_file

BUCKET_NAME = "audioplatformstack-audiostoragebucketd8d3b0dc-qfiv3hvchgq4"
s3 = boto3.client('s3', region_name='ap-south-1')
dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')

def get_table():
    client = boto3.client('dynamodb', region_name='ap-south-1')
    for t in client.list_tables()['TableNames']:
        if 'AudioMetadataTable' in t: return dynamodb.Table(t)
    return None

class ArtDisplay(Static):
    def update_art(self, local_path):
        if not os.path.exists(local_path):
            self.update("[dim]No Art Available[/dim]")
            return
        try:
            img = from_file(local_path, width=45)
            self.update(str(img))
        except Exception as e:
            self.update(f"[red]Render Error: {e}[/red]")

class AudioPlatformTUI(App):
    CSS = """
    #left-pane { width: 40%; border-right: solid magenta; padding: 1; }
    #right-pane { width: 60%; padding: 2; align: center top; }
    ArtDisplay { height: 25; margin-bottom: 1; }
    .title { text-style: bold; color: cyan; margin-bottom: 1; }
    #now-playing-info { margin-bottom: 1; }
    #track-progress { width: 100%; margin-top: 1; }
    """
    BINDINGS = [("q", "quit", "Quit Application")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left-pane"):
                yield Label("🌐 GLOBAL CATALOG", classes="title")
                yield Tree("Library", id="catalog-tree")
            with Vertical(id="right-pane"):
                yield ArtDisplay("Select a track to load S3 objects...", id="art-panel")
                yield Label("▶ NOW PLAYING", classes="title")
                yield Label("", id="now-playing-info")
                yield ProgressBar(id="track-progress", show_eta=True) 
        yield Footer()

    def on_mount(self) -> None:
        self.table = get_table()
        if not self.table: self.exit("Database not found")
        self.load_catalog()
        self.player_process = None
        self.progress_timer = None
        
        bar = self.query_one("#track-progress", ProgressBar)
        bar.progress = 0

    def load_catalog(self):
        tree = self.query_one("#catalog-tree", Tree)
        items = self.table.scan().get('Items', [])
        
        catalog = {}
        for item in items:
            if item.get('Schema') != 'V4': continue
            artist = item.get('Artist', 'Unknown')
            release = item.get('ReleaseName', 'Unknown')
            if artist not in catalog: catalog[artist] = {}
            if release not in catalog[artist]: catalog[artist][release] = []
            catalog[artist][release].append(item)

        for artist, releases in catalog.items():
            artist_node = tree.root.add(f"🎤 {artist}", expand=True)
            for release, tracks in releases.items():
                rel_node = artist_node.add(f"💿 {release}")
                for t in sorted(tracks, key=lambda x: x.get('TrackNumber', '99')):
                    rel_node.add_leaf(f"🎵 {t['TrackNumber']} - {t['TrackName']}", data=t)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if not event.node.allow_expand: 
            self.play_track(event.node.data)

    def play_track(self, data):
        info_label = self.query_one("#now-playing-info", Label)
        art_panel = self.query_one("#art-panel", ArtDisplay)
        bar = self.query_one("#track-progress", ProgressBar)
        
        info_label.update(f"[bold white]{data['TrackName']}[/bold white]\n[cyan]{data['Artist']}[/cyan]\n[dim]Buffering...[/dim]")
        
        # Reset and pulse the bar only while fetching duration
        if self.progress_timer: self.progress_timer.stop()
        bar.progress = 0
        bar.update(total=None)
        
        # 1. Fetch & Render Artwork (With Cache Healing)
        cover_key = data.get('CoverKey')
        if cover_key and cover_key != "NONE":
            img_path = f"/tmp/{cover_key}"
            
            # Delete the file if it's suspiciously small (broken XML cache)
            if os.path.exists(img_path) and os.path.getsize(img_path) < 1000:
                os.remove(img_path)
                
            if not os.path.exists(img_path):
                url = s3.generate_presigned_url('get_object', Params={'Bucket': BUCKET_NAME, 'Key': f"{data['TenantID']}/{cover_key}"})
                res = requests.get(url)
                if res.status_code == 200:
                    with open(img_path, 'wb') as f: f.write(res.content)
                    art_panel.update_art(img_path)
                else:
                    art_panel.update(f"[yellow]⚠️ S3 Error: Image dropped during ingestion.[/yellow]")
            else:
                art_panel.update_art(img_path)
        else:
            art_panel.update("[dim]No Cover Art Registered[/dim]")

        # 2. Play Audio
        if self.player_process: self.player_process.kill()
        audio_url = s3.generate_presigned_url('get_object', Params={'Bucket': BUCKET_NAME, 'Key': f"{data['TenantID']}/{data['FileName']}"})
        self.player_process = subprocess.Popen(['mpv', '--no-video', '--msg-level=all=no', audio_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        info_label.update(f"[bold green]{data['TrackName']}[/bold green]\n[cyan]{data['Artist']}[/cyan]\n[dim]Streaming live from AWS Edge Node[/dim]")

        # 3. Fire the background thread to build the real progress bar
        self.start_progress_tracker(audio_url)

    @work(thread=True)
    def start_progress_tracker(self, audio_url):
        """Runs in the background to probe the stream without freezing the UI"""
        try:
            cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_url]
            duration = float(subprocess.check_output(cmd, text=True).strip())
            # Safely tell the main UI thread to start the timer
            self.call_from_thread(self.init_progress_bar, duration)
        except Exception:
            pass # Failsafe: leaves the bar in indeterminate pulsing mode

    def init_progress_bar(self, duration):
        """Sets the exact track length and starts the 1-second tick"""
        bar = self.query_one("#track-progress", ProgressBar)
        bar.update(total=duration)
        if self.progress_timer: self.progress_timer.stop()
        self.progress_timer = self.set_interval(1.0, self.tick_progress)

    def tick_progress(self):
        """Advances the bar by 1 second if the audio player is still running"""
        bar = self.query_one("#track-progress", ProgressBar)
        if self.player_process and self.player_process.poll() is None:
            bar.advance(1)
        else:
            if self.progress_timer: self.progress_timer.stop()

    def on_unmount(self) -> None:
        if self.player_process: self.player_process.kill()

if __name__ == "__main__":
    app = AudioPlatformTUI()
    app.run()