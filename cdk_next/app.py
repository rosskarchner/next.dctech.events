#!/usr/bin/env python3
"""CDK app for the parallel next.dctech.events stack.

Fully decoupled from the production TypeScript CDK app: all resources use
Next*/dctech-events-next* naming and existing resources are referenced by
literal ID/ARN only (no CloudFormation cross-stack imports/exports).
"""
import os

import aws_cdk as cdk

import config
from stacks.hosting_stack import NextHostingStack
from stacks.dynamodb_stack import NextDynamoDBStack
from stacks.cognito_stack import NextCognitoStack
from stacks.cognito_client_stack import NextCognitoClientStack
from stacks.api_stack import NextApiStack
from stacks.site_generator_stack import NextSiteGeneratorStack
from stacks.ical_aggregator_stack import NextIcalAggregatorStack
from stacks.newsletter_stack import NextNewsletterStack
from stacks.qa_agent_stack import NextQaAgentStack
from stacks.discovery_agent_stack import NextDiscoveryAgentStack
from stacks.cicd_stack import NextCicdStack

app = cdk.App()
env = cdk.Environment(account=config.ACCOUNT, region=config.REGION)

db = NextDynamoDBStack(app, "NextDynamoDBStack", env=env)

# Newsletter is built before hosting: the CloudFront distribution proxies
# /newsletter* to this API so the signup app is served same-origin.
newsletter = NextNewsletterStack(
    app, "NextNewsletterStack", table=db.table, env=env
)
hosting = NextHostingStack(
    app, "NextHostingStack", newsletter_api=newsletter.api, env=env
)
cognito = NextCognitoStack(app, "NextCognitoStack", env=env)
cognito_client = NextCognitoClientStack(app, "NextCognitoClientStack", env=env)
cognito_client.add_dependency(cognito)
api = NextApiStack(
    app,
    "NextApiStack",
    table=db.table,
    user_pool=cognito_client.user_pool,
    user_pool_client=cognito_client.user_pool_client,
    env=env,
)

site_generator = NextSiteGeneratorStack(
    app,
    "NextSiteGeneratorStack",
    table=db.table,
    site_bucket=hosting.bucket,
    distribution=hosting.distribution,
    env=env,
)

ical_aggregator = NextIcalAggregatorStack(
    app, "NextIcalAggregatorStack", table=db.table, env=env
)

qa_agent = NextQaAgentStack(app, "NextQaAgentStack", table=db.table, env=env)
discovery_agent = NextDiscoveryAgentStack(
    app, "NextDiscoveryAgentStack", table=db.table, env=env
)

cicd = NextCicdStack(app, "NextCicdStack", env=env)

# Tags cannot be added during `cdk import`; NextCognitoStack picks them up
# on the ordinary deploy that follows.
_import_mode = os.environ.get("CDK_IMPORT_MODE") == "1"
for _stack in app.node.children:
    if not isinstance(_stack, cdk.Stack):
        continue
    if _import_mode and _stack.stack_name == "NextCognitoStack":
        continue
    for key, value in config.TAGS.items():
        cdk.Tags.of(_stack).add(key, value)

app.synth()
