from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput, # <--- ADD THIS
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
    aws_sqs as sqs,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_s3_notifications as s3n,
    aws_lambda_event_sources as event_sources,
    aws_cognito as cognito, # <--- ADD THIS
)
from constructs import Construct

class AudioPlatformStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- NEW: AMAZON COGNITO IDENTITY PROVIDER ---
        user_pool = cognito.UserPool(self, "AudioAppUsers",
            user_pool_name="AudioPlatformUsers",
            self_sign_up_enabled=True,  
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            removal_policy=RemovalPolicy.DESTROY
        )

        user_pool_client = user_pool.add_client("AudioAppClient",
            auth_flows=cognito.AuthFlow(user_password=True),
            generate_secret=False # We are a public CLI client, no secrets allowed
        )

        # Output the IDs so we can plug them into our Python script
        CfnOutput(self, "CognitoUserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "CognitoClientId", value=user_pool_client.user_pool_client_id)

        # 1. STORAGE: Multi-Tenant S3 Bucket
        # We use standard tier, destroying it on teardown since it's an academic project.
        audio_bucket = s3.Bucket(self, "AudioStorageBucket",
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            cors=[s3.CorsRule(
                allowed_methods=[s3.HttpMethods.PUT],
                allowed_origins=["*"],
                allowed_headers=["*"]
            )]
        )

        # 2. DATABASE: DynamoDB for Metadata
        # Partition Key is TenantID (String), Sort Key is SongID (String)
        metadata_table = dynamodb.Table(self, "AudioMetadataTable",
            partition_key=dynamodb.Attribute(name="TenantID", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SongID", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY
        )

        # 3. BUFFER: SQS Queue (Event-Driven Core)
        # Catches S3 upload events so we don't overwhelm the processing layer
        upload_queue = sqs.Queue(self, "UploadQueue")

        # Wire S3 to send an event to SQS whenever a .wav file is created
        audio_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.SqsDestination(upload_queue),
            s3.NotificationKeyFilter(suffix=".wav")
        )

        # 4. COMPUTE: API Lambda (Generates Presigned URLs)
        api_lambda = _lambda.Function(self, "ApiHandlerLambda",
            runtime=_lambda.Runtime.PYTHON_3_9,
            handler="api_handler.handler",
            code=_lambda.Code.from_asset("lambda/api_handler"),
            environment={"BUCKET_NAME": audio_bucket.bucket_name}
        )
        # Grant least privilege: API Lambda ONLY needs to write to S3
        audio_bucket.grant_put(api_lambda)

        # 5. COMPUTE: Processor Lambda (Reads SQS, Writes to DynamoDB)
        processor_lambda = _lambda.Function(self, "ProcessorLambda",
            runtime=_lambda.Runtime.PYTHON_3_9,
            handler="processor.handler",
            code=_lambda.Code.from_asset("lambda/processor"),
            environment={"TABLE_NAME": metadata_table.table_name}
        )
        # Grant least privilege: Processor needs to read from SQS and write to DynamoDB
        metadata_table.grant_write_data(processor_lambda)
        processor_lambda.add_event_source(event_sources.SqsEventSource(upload_queue))

        # 6. NETWORKING: API Gateway
        api = apigw.RestApi(self, "AudioIngestionApi",
            rest_api_name="Audio Ingestion Service",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS
            )
        )
        
        # Create endpoint: POST /upload
        upload_resource = api.root.add_resource("upload")
        upload_resource.add_method("POST", apigw.LambdaIntegration(api_lambda))