"""VPC Endpoints — added as a separate file to keep vpc_stack.py concise.

Call add_vpc_endpoints(vpc_stack) after the VPC stack is created.
These endpoints keep traffic on the AWS private network.
"""

from aws_cdk import aws_ec2 as ec2


def add_vpc_endpoints(vpc: ec2.Vpc) -> None:
    """Add gateway and interface VPC endpoints used by AgentCore."""

    # Gateway endpoints (free)
    vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)
    vpc.add_gateway_endpoint(
        "DynamoEndpoint", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB
    )

    # Interface endpoints (billed per AZ-hour)
    interface_services = [
        ("StsEndpoint", ec2.InterfaceVpcEndpointAwsService.STS),
        ("SecretsEndpoint", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
        ("KmsEndpoint", ec2.InterfaceVpcEndpointAwsService.KMS),
        ("LogsEndpoint", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
        ("BedrockEndpoint", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
    ]

    for endpoint_id, service in interface_services:
        vpc.add_interface_endpoint(endpoint_id, service=service)
