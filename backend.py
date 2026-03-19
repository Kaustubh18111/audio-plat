import boto3
import json
import sys

dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')

def get_table():
    client = boto3.client('dynamodb', region_name='ap-south-1')
    for t in client.list_tables()['TableNames']:
        if 'AudioMetadataTable' in t: return dynamodb.Table(t)
    return None

def fetch_catalog():
    table = get_table()
    if not table:
        print(json.dumps({"error": "Database not found"}))
        sys.exit(1)

    items = table.scan().get('Items', [])
    
    # Filter only the clean V4 data
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
            
    # Print as a strict JSON string so Rust can parse it
    print(json.dumps(catalog))

if __name__ == "__main__":
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "catalog":
            fetch_catalog()