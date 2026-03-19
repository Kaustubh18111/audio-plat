import boto3
import json
import sys

# --- CONFIGURATION ---
REGION = 'ap-south-1'
# IMPORTANT: Copy your Cognito Client ID from client.py and paste it here:
CLIENT_ID = "1ate091qv7ibstkvo0il3lsbrv" 

dynamodb = boto3.resource('dynamodb', region_name=REGION)
cognito_client = boto3.client('cognito-idp', region_name=REGION)

def get_table():
    client = boto3.client('dynamodb', region_name=REGION)
    for t in client.list_tables()['TableNames']:
        if 'AudioMetadataTable' in t: return dynamodb.Table(t)
    return None

def headless_login(username, password):
    table = get_table()
    if not table:
        print(json.dumps({"status": "error", "message": "Database not found"}))
        return

    try:
        # 1. Authenticate with Cognito
        response = cognito_client.initiate_auth(
            ClientId=CLIENT_ID,
            AuthFlow='USER_PASSWORD_AUTH',
            AuthParameters={'USERNAME': username, 'PASSWORD': password}
        )
        
        # 2. Fetch Profile from DynamoDB to determine Role (Creator vs Listener)
        profile_res = table.get_item(Key={'TenantID': username, 'SongID': 'PROFILE_DATA'})
        
        if 'Item' in profile_res:
            item = profile_res['Item']
            role = item.get('Schema', 'ListenerProfile') # Default to listener if undefined
            artist_name = item.get('ArtistName', username)
            print(json.dumps({
                "status": "success", 
                "username": username, 
                "artist_name": artist_name, 
                "role": role,
                "token": response['AuthenticationResult']['IdToken'][:20] + "..." # Just returning a snippet for logs
            }))
        else:
            # If they are in Cognito but not DynamoDB, default to a listener
            print(json.dumps({"status": "success", "username": username, "artist_name": username, "role": "ListenerProfile"}))
            
    except cognito_client.exceptions.NotAuthorizedException:
         print(json.dumps({"status": "error", "message": "Invalid password."}))
    except cognito_client.exceptions.UserNotFoundException:
         print(json.dumps({"status": "error", "message": "User does not exist."}))
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}))

def fetch_catalog():
    table = get_table()
    if not table:
        print(json.dumps([]))
        return

    items = table.scan().get('Items', [])
    catalog = []
    for item in items:
        if item.get('Schema') == 'V4':
            catalog.append({
                "id": item.get('SongID', ''),
                "track": item.get('TrackName', 'Unknown'),
                "artist": item.get('Artist', 'Unknown'),
                "release": item.get('ReleaseName', 'Unknown'),
                "tenant": item.get('TenantID', ''),
                "file_key": item.get('FileName', ''),
                "cover_key": item.get('CoverKey', 'NONE')
            })
    print(json.dumps(catalog))

if __name__ == "__main__":
    if len(sys.argv) > 1:
        command = sys.argv[1]
        
        if command == "catalog":
            fetch_catalog()
            
        elif command == "login":
            if len(sys.argv) >= 4:
                headless_login(sys.argv[2], sys.argv[3])
            else:
                print(json.dumps({"status": "error", "message": "Missing credentials"}))