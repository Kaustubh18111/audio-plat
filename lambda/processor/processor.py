import json
import boto3
import os
import urllib.parse
import uuid

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ['TABLE_NAME']
table = dynamodb.Table(TABLE_NAME)

def handler(event, context):
    # SQS can send multiple records in batches
    for record in event['Records']:
        # The SQS message body contains the S3 event notification
        sqs_body = json.loads(record['body'])
        
        if 'Records' not in sqs_body:
            continue
            
        for s3_record in sqs_body['Records']:
            bucket = s3_record['s3']['bucket']['name']
            # Decode the object key (handles spaces and special characters)
            key = urllib.parse.unquote_plus(s3_record['s3']['object']['key'])
            size = s3_record['s3']['object']['size']
            
            # Extract Multi-Tenant metadata from the S3 path (tenant_id/filename.wav)
            parts = key.split('/')
            tenant_id = parts[0] if len(parts) > 1 else 'unknown-tenant'
            filename = parts[-1]
            
            # Write to DynamoDB
            table.put_item(
                Item={
                    'TenantID': tenant_id,
                    'SongID': str(uuid.uuid4()), # Unique ID for the database
                    'FileName': filename,
                    'FileSizeBytes': size,
                    'BucketLocation': bucket,
                    'Status': 'PROCESSED'
                }
            )
            print(f"Successfully processed {filename} for {tenant_id}")