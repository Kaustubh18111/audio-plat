import boto3
import sys

BUCKET_NAME = "audioplatformstack-audiostoragebucketd8d3b0dc-qfiv3hvchgq4"

# Initialize AWS SDKs
s3 = boto3.resource('s3', region_name='ap-south-1')
dynamo_client = boto3.client('dynamodb', region_name='ap-south-1')
dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
cognito_client = boto3.client('cognito-idp', region_name='ap-south-1')

def get_table_name():
    for t in dynamo_client.list_tables()['TableNames']:
        if 'AudioMetadataTable' in t: 
            return t
    return None

def get_user_pool_id():
    # Auto-detect the Cognito User Pool ID from your stack
    pools = cognito_client.list_user_pools(MaxResults=50).get('UserPools', [])
    for pool in pools:
        # Match based on standard CDK/CloudFormation naming conventions
        if 'audioplatform' in pool['Name'].lower() or len(pools) == 1:
            return pool['Id']
    return None

print("="*60)
print(" ☢️  INITIATING TOTAL CLOUD PURGE (S3, DYNAMODB, COGNITO) ☢️ ")
print("="*60)

try:
    # 1. Purge S3 Vault
    print(f"[*] Emptying S3 Vault: {BUCKET_NAME}...")
    bucket = s3.Bucket(BUCKET_NAME)
    bucket.objects.all().delete()
    print("[+] S3 Vault successfully purged.")

    # 2. Wipe DynamoDB Table
    table_name = get_table_name()
    if table_name:
        print(f"\n[*] Wiping DynamoDB Table: {table_name}...")
        table = dynamodb.Table(table_name)
        
        # Dynamically grab primary keys so we don't have to guess the schema
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

    # 3. Nuke Cognito User Pool
    pool_id = get_user_pool_id()
    if pool_id:
        print(f"\n[*] Erasing Cognito User Pool: {pool_id}...")
        
        # Use a paginator in case you have more than 60 test users
        paginator = cognito_client.get_paginator('list_users')
        user_count = 0
        
        for page in paginator.paginate(UserPoolId=pool_id):
            for user in page.get('Users', []):
                username = user['Username']
                # Forcefully delete the user as an Admin
                cognito_client.admin_delete_user(
                    UserPoolId=pool_id,
                    Username=username
                )
                user_count += 1
                
        print(f"[+] Obliterated {user_count} user accounts from Cognito.")
    else:
        print("[-] Cognito User Pool not found or auto-detect failed.")

    print("\n" + "="*60)
    print(" 🟢 CLOUD RESET COMPLETE. READY FOR V3 INGESTION. 🟢 ")
    print("="*60)

except Exception as e:
    print(f"\n[-] Purge failed: {e}")