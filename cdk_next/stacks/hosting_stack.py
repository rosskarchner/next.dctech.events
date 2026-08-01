"""NextHostingStack — ACM cert, S3 bucket, CloudFront, Route53 for next.dctech.events.

Adapted from the (uninstantiated) infrastructure/lib/frontend-stack.ts pattern:
OAC-scoped bucket, directory-index CloudFront Function, HTTP2+3, IPv6,
TLS 1.2 2021 minimum. One bucket/distribution serves both the calgen-built
site and /edit/*.
"""
import aws_cdk as cdk
from aws_cdk import (
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_s3 as s3,
)
from constructs import Construct

import config

DIRECTORY_INDEX_FN = """\
function handler(event) {
  var request = event.request;
  var uri = request.uri;

  // If URI ends with '/', append 'index.html'
  if (uri.endsWith('/')) {
    request.uri += 'index.html';
  }
  // If URI has no extension, append '/index.html'
  else if (!uri.includes('.')) {
    request.uri += '/index.html';
  }

  return request;
}
"""


class NextHostingStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "HostedZone",
            hosted_zone_id=config.HOSTED_ZONE_ID,
            zone_name=config.ZONE_NAME,
        )

        # New cert for next.dctech.events only — deliberately not the TS-owned
        # *.dctech.events wildcard, so the two apps' lifecycles stay decoupled.
        self.certificate = acm.Certificate(
            self,
            "NextCertificate",
            domain_name=config.DOMAIN,
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        self.bucket = s3.Bucket(
            self,
            "NextSiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=False,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        directory_index_function = cloudfront.Function(
            self,
            "NextDirectoryIndexFunction",
            code=cloudfront.FunctionCode.from_inline(DIRECTORY_INDEX_FN),
            comment="Rewrite directory URLs to index.html for next.dctech.events",
            function_name=f"{config.PREFIX}-directory-index",
        )

        self.distribution = cloudfront.Distribution(
            self,
            "NextDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                compress=True,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=directory_index_function,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            default_root_object="index.html",
            domain_names=[config.DOMAIN],
            certificate=self.certificate,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            enable_ipv6=True,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )

        route53.ARecord(
            self,
            "NextARecord",
            zone=hosted_zone,
            record_name=config.DOMAIN,
            target=route53.RecordTarget.from_alias(
                route53_targets.CloudFrontTarget(self.distribution)
            ),
            comment="IPv4 record for next.dctech.events pointing to CloudFront",
        )

        route53.AaaaRecord(
            self,
            "NextAAAARecord",
            zone=hosted_zone,
            record_name=config.DOMAIN,
            target=route53.RecordTarget.from_alias(
                route53_targets.CloudFrontTarget(self.distribution)
            ),
            comment="IPv6 record for next.dctech.events pointing to CloudFront",
        )

        cdk.CfnOutput(self, "NextSiteBucketName", value=self.bucket.bucket_name)
        cdk.CfnOutput(self, "NextDistributionId", value=self.distribution.distribution_id)
        cdk.CfnOutput(
            self, "NextDistributionDomain", value=self.distribution.domain_name
        )
