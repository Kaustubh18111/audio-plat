import boto3
import sys

BUCKET_NAME = "audioplatformstack-audiostoragebucketd8d3b0dc-qfiv3hvchgq4"

# Initialize AWS SDKs
s3 = boto3.resource('s3', region_name='ap-south-1')
dynamo_client = boto3.client('dynamodb', region_name='ap-south-1')
dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')

def get_table_name():
    for t in dynamo_client.list_tables()['TableNames']:
        if 'AudioMetadataTable' in t: 
            return t
    return None

print("="*60)
print(" ☢️  INITIATING CLOUD PURGE (S3 & DYNAMODB) ☢️ ")
print("="*60)

try:
    # 1. Purge S3 Bucket
    print(f"[*] Emptying S3 Vault: {BUCKET_NAME}...")
    bucket = s3.Bucket(BUCKET_NAME)
    bucket.objects.all().delete()
    print("[+] S3 Vault successfully purged.")

    # 2. Purge DynamoDB Table
    table_name = get_table_name()
    if table_name:
        print(f"[*] Wiping DynamoDB Table: {table_name}...")
        table = dynamodb.Table(table_name)
        
        # Dynamically grab your primary keys so we don't have to guess the schema
        keys = [k['AttributeName'] for k in table.key_schema]
        
        items = table.scan().get('Items', [])
        if items:
            with table.batch_writer() as batch:
                for item in items:
                    key_dict = {k: item[k] for k in keys}
                    batch.delete_item(Key=key_dict)
            print(f"[+] Deleted {len(items)} legacy records from NoSQL database.")
        else:
            print("[+] Database is already empty.")
    else:
        print("[-] DynamoDB table not found.")

    print("="*60)
    print(" 🟢 CLOUD RESET COMPLETE. READY FOR V2 INGESTION. 🟢 ")

except Exception as e:
    print(f"\n[-] Purge failed: {e}")