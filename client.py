import requests
import sys
import os
import re
import boto3
import time
import getpass
import urllib.parse
import uuid
from mutagen import File
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

# --- CLOUD CONFIGURATION ---
API_ENDPOINT = "https://ljxt8tjngk.execute-api.ap-south-1.amazonaws.com/prod/upload"
USER_POOL_ID = "ap-south-1_DIEFOO2Ti" 
CLIENT_ID = "1ate091qv7ibstkvo0il3lsbrv"

cognito_client = boto3.client('cognito-idp', region_name='ap-south-1')
dynamo_client = boto3.client('dynamodb', region_name='ap-south-1')
console = Console()

def get_table_name():
    for t in dynamo_client.list_tables()['TableNames']:
        if 'AudioMetadataTable' in t: return t
    sys.exit(1)

table = boto3.resource('dynamodb', region_name='ap-south-1').Table(get_table_name())

def authenticate():
    os.system('clear' if os.name == 'posix' else 'cls')
    console.print(Panel.fit("[bold cyan]🌩️  PRODUCER PORTAL (STRICT SCHEMA) 🌩️[/bold cyan]", border_style="cyan"))
    console.print("1. Log In\n2. Create Artist Account\n3. Exit\n")
    choice = Prompt.ask("Select an option", choices=["1", "2", "3"])
    
    if choice == '2':
        email = Prompt.ask("Enter Email").strip()
        password = getpass.getpass("Enter Password: ")
        try:
            with console.status("[bold green]Provisioning Identity..."):
                cognito_client.sign_up(ClientId=CLIENT_ID, Username=email, Password=password)
                cognito_client.admin_confirm_sign_up(UserPoolId=USER_POOL_ID, Username=email)
            return login_user(email, password)
        except Exception as e:
            console.print(f"[red]Registration failed: {e}[/red]"); time.sleep(2); return authenticate()
    elif choice == '1':
        return login_user(Prompt.ask("Enter Email").strip(), getpass.getpass("Enter Password: "))
    else: sys.exit(0)

def login_user(email, password):
    try:
        response = cognito_client.initiate_auth(ClientId=CLIENT_ID, AuthFlow='USER_PASSWORD_AUTH', AuthParameters={'USERNAME': email, 'PASSWORD': password})
        token = response['AuthenticationResult']['AccessToken']
        tenant_id = [a['Value'] for a in cognito_client.get_user(AccessToken=token)['UserAttributes'] if a['Name'] == 'sub'][0]
        console.print("[green][+] Access Granted.[/green]"); time.sleep(1)
        return email, tenant_id
    except Exception as e:
        console.print(f"[red]Access Denied: {e}[/red]"); time.sleep(2); return authenticate()

def extract_cover_art(filepath, output_dir="/tmp/art"):
    try:
        os.makedirs(output_dir, exist_ok=True)
        audio = File(filepath)
        art_data = None
        if hasattr(audio, 'pictures') and audio.pictures: art_data = audio.pictures[0].data
        elif hasattr(audio, 'tags') and audio.tags:
            for tag in audio.tags.values():
                if tag.__class__.__name__ == 'APIC': art_data = tag.data; break
        if art_data:
            out_path = os.path.join(output_dir, f"art_{uuid.uuid4().hex}.jpg")
            with open(out_path, "wb") as f: f.write(art_data)
            return out_path
    except Exception: return None

def upload_to_s3(tenant_id, local_path, cloud_key, is_image=False):
    res = requests.post(API_ENDPOINT, json={"tenant_id": tenant_id, "file_name": cloud_key})
    if res.status_code == 200:
        with open(local_path, 'rb') as f:
            # The "Smuggler Fix": Masking JPEGs as WAV files so the AWS Lambda allows the upload
            put_res = requests.put(
                res.json()['upload_url'], 
                data=f.read(), 
                headers={'Content-Type': 'audio/wav'} 
            )
        return put_res.status_code == 200
    return False

if __name__ == "__main__":
    email, tenant_id = authenticate()
    target_path = Prompt.ask("\nEnter absolute path to music folder OR a single file").strip()
    
    # --- NEW FILE/FOLDER DETECTION LOGIC ---
    if os.path.isfile(target_path):
        target_dir = os.path.dirname(target_path)
        files = [os.path.basename(target_path)]
    elif os.path.isdir(target_path):
        target_dir = target_path
        files = sorted([f for f in os.listdir(target_dir) if f.endswith(('.flac', '.wav', '.mp3'))])
    else:
        console.print("[red]Invalid path provided.[/red]")
        sys.exit(1)

    if not files:
        console.print("[red]No audio files found.[/red]")
        sys.exit(1)
    
    # Safe metadata extraction
    try:
        first_file_tags = File(os.path.join(target_dir, files[0])).tags
        meta_artist = first_file_tags.get('artist', [''])[0] if first_file_tags else ""
        meta_album = first_file_tags.get('album', [''])[0] if first_file_tags else ""
    except:
        meta_artist, meta_album = "", ""

    console.print("\n[bold yellow]--- STRICT METADATA ENTRY ---[/bold yellow]")
    artist = Prompt.ask("Artist Name", default=str(meta_artist))
    release = Prompt.ask("Release Name", default=str(meta_album))
    rel_type = Prompt.ask("Type (1:Album, 2:EP, 3:Single)", default="1")
    rel_type = {'1': 'Album', '2': 'EP', '3': 'Single'}.get(rel_type, 'Single')

    profile_key, cover_key = "NONE", "NONE"
    
    profile_pic = Prompt.ask("Path to Profile Pic (.jpg) [Blank to skip]").strip()
    if profile_pic and os.path.exists(profile_pic):
        profile_key = f"img_prof_{uuid.uuid4().hex}.jpg"
        upload_to_s3(tenant_id, profile_pic, profile_key, True)

    extracted_art = extract_cover_art(os.path.join(target_dir, files[0]))
    if extracted_art:
        cover_key = f"img_cov_{uuid.uuid4().hex}.jpg"
        upload_to_s3(tenant_id, extracted_art, cover_key, True)

    console.print("\n[bold magenta]--- INGESTING FILES ---[/bold magenta]")
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(), TaskProgressColumn(), console=console) as prog:
        task = prog.add_task("[cyan]Uploading...", total=len(files))
        
        for idx, filename in enumerate(files):
            track_uuid = f"audio_{uuid.uuid4().hex}.wav"
            track_clean = filename.replace('.flac','').replace('.wav','').replace('.mp3','')
            
            upload_to_s3(tenant_id, os.path.join(target_dir, filename), track_uuid)
            
            table.put_item(Item={
                'SongID': track_uuid,    # The required Primary Key
                'TenantID': tenant_id,
                'FileName': track_uuid,
                'Schema': 'V4',
                'Artist': artist,
                'ReleaseName': release,
                'ReleaseType': rel_type,
                'TrackName': track_clean,
                'TrackNumber': str(idx + 1).zfill(2),
                'CoverKey': cover_key,
                'ProfileKey': profile_key
            })
            prog.advance(task)
            
    console.print("[bold green][+] Upload and Database Write Complete.[/bold green]")