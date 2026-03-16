import json
import boto3
import os
import uuid

s3_client = boto3.client('s3')
BUCKET_NAME = os.environ['BUCKET_NAME']

def handler(event, context):
    try:
        # Parse the incoming request body
        body = json.loads(event.get('body', '{}'))
        tenant_id = body.get('tenant_id', 'default-tenant')
        file_name = body.get('file_name', f'{uuid.uuid4()}.wav')

        # Multi-tenant isolation: Force the file into a specific tenant's folder
        object_key = f"{tenant_id}/{file_name}"

        # Generate the Presigned URL (valid for 5 minutes)
        presigned_url = s3_client.generate_presigned_url(
            'put_object',
            Params={'Bucket': BUCKET_NAME, 'Key': object_key, 'ContentType': 'audio/wav'},
            ExpiresIn=300
        )

        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'upload_url': presigned_url, 'object_key': object_key})
        }
    except Exception as e:
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}