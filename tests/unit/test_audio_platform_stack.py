import aws_cdk as core
import aws_cdk.assertions as assertions

from audio_platform.audio_platform_stack import AudioPlatformStack

# example tests. To run these tests, uncomment this file along with the example
# resource in audio_platform/audio_platform_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = AudioPlatformStack(app, "audio-platform")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
