"""NextHostingStack — ACM cert, S3 bucket, CloudFront, Route53 for next.dctech.events.

Adapted from the (uninstantiated) infrastructure/lib/frontend-stack.ts pattern:
OAC-scoped bucket, directory-index CloudFront Function, HTTP2+3, IPv6,
TLS 1.2 2021 minimum. One bucket/distribution serves both the calgen-built
site and /edit/*.
"""
import aws_cdk as cdk
from aws_cdk import (
    aws_apigateway as apigateway,
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
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        newsletter_api: apigateway.RestApi,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "HostedZone",
            hosted_zone_id=config.HOSTED_ZONE_ID,
            zone_name=config.ZONE_NAME,
        )

        # Our own cert for the apex + www — deliberately not the TS-owned
        # *.dctech.events wildcard, so the two apps' lifecycles stay decoupled.
        self.certificate = acm.Certificate(
            self,
            "NextSiteCertificate",
            domain_name=config.DOMAIN,
            subject_alternative_names=[config.WWW_DOMAIN],
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
            comment="Rewrite directory URLs to index.html for dctech.events",
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
            domain_names=[config.DOMAIN, config.WWW_DOMAIN],
            certificate=self.certificate,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            enable_ipv6=True,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )

        # Apex and www both alias to the distribution, matching how GitHub
        # Pages served them before the cutover. These records carried
        # `delete_existing=True` to clear the old GitHub Pages A/AAAA records
        # on the 2026-08-01 cutover; that is done, this stack owns them now,
        # and CFN writes them with UPSERT anyway — so the flag is dropped
        # rather than carried as a live custom resource that could delete
        # production DNS if a later deploy fails.
        # (The pre-existing old.dctech.events records still point at GitHub
        # Pages, so the previous site stays reachable there.)
        alias_target = route53.RecordTarget.from_alias(
            route53_targets.CloudFrontTarget(self.distribution)
        )
        for label, domain in (("Apex", config.DOMAIN), ("Www", config.WWW_DOMAIN)):
            route53.ARecord(
                self,
                f"SiteARecord{label}",
                zone=hosted_zone,
                record_name=domain,
                target=alias_target,
                comment=f"IPv4 record for {domain} pointing to CloudFront",
            )
            route53.AaaaRecord(
                self,
                f"SiteAAAARecord{label}",
                zone=hosted_zone,
                record_name=domain,
                target=alias_target,
                comment=f"IPv6 record for {domain} pointing to CloudFront",
            )

        # Serve the newsletter signup/confirm app under /newsletter from the
        # same origin as the site, so the homepage's HTMX "Subscribe today"
        # swap and the emailed confirmation links both use dctech.events.
        # No directory-index function here — these are API paths, not objects.
        #
        # Deliberately two exact-ish patterns rather than "/newsletter*": the
        # wildcard form also captures calgen's generated /newsletter.html and
        # /newsletter.txt, which must keep coming from S3.
        newsletter_origin = origins.HttpOrigin(
            f"{newsletter_api.rest_api_id}.execute-api.{self.region}.amazonaws.com",
            origin_path=f"/{newsletter_api.deployment_stage.stage_name}",
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        )
        for pattern in ("/newsletter", "/newsletter/*"):
            self.distribution.add_behavior(
                pattern,
                newsletter_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                # Forwards everything except Host — API Gateway must see its
                # own hostname or it can't match the request to a stage.
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            )

        cdk.CfnOutput(self, "NextSiteBucketName", value=self.bucket.bucket_name)
        cdk.CfnOutput(self, "NextDistributionId", value=self.distribution.distribution_id)
        cdk.CfnOutput(
            self, "NextDistributionDomain", value=self.distribution.domain_name
        )
