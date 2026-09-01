"""Shared factory for the Python Lambda boilerplate repeated across stacks:
runtime, handler path, code asset, and an explicit-retention log group.

A plain function, not a Construct subclass, on purpose (next_dctech_events-1q0
— investigated first, not squeezed into a batch). A Construct subclass would
insert its own construct-path segment between `scope` and the Lambda
resource, changing the CloudFormation logical ID CDK derives from that path
— which CloudFormation reads as delete-old/create-new for every converted
site, including live production Lambdas some of them are (NextApiFunction,
CalendarQcRole's runtime, ...). Calling `lambda_.Function(scope,
construct_id, ...)` directly here, with no extra nesting, keeps the logical
ID byte-identical to what today's inline call produces, so converting a call
site to this is a pure refactor — verify with `cdk diff` on every stack you
touch and confirm it shows no replacement before deploying, the same way
this was verified when ops_stack.py was first converted.

log_group_id has no default derived from construct_id: existing log group
ids across stacks don't follow the function's own construct_id (e.g.
NextCleanupUnconfirmedUsers's log group is "NextCleanupLogGroup", not
"NextCleanupUnconfirmedUsersLogGroup") — pass the exact existing string when
converting a call site, or a new one of your choosing for a new Lambda.
"""
import os

import aws_cdk as cdk
from aws_cdk import (
    aws_lambda as lambda_,
    aws_logs as logs,
)


def python_lambda(
    scope,
    construct_id,
    *,
    function_name,
    handler,
    code_dir,
    build_dir,
    log_group_id,
    timeout,
    environment=None,
    memory_size=None,
    architecture=lambda_.Architecture.X86_64,
    log_retention=logs.RetentionDays.ONE_WEEK,
    log_removal_policy=cdk.RemovalPolicy.DESTROY,
):
    """Build a Python 3.12 Lambda with an explicit-retention log group.

    `code_dir` is a subdirectory of `build_dir` (the per-stack BUILD_DIR
    constant every stack already defines) — matches
    `lambda_.Code.from_asset(os.path.join(BUILD_DIR, "some_dir"))`, the
    pattern every call site already uses.
    """
    kwargs = dict(
        function_name=function_name,
        runtime=lambda_.Runtime.PYTHON_3_12,
        architecture=architecture,
        handler=handler,
        code=lambda_.Code.from_asset(os.path.join(build_dir, code_dir)),
        timeout=timeout,
        environment=environment or {},
        log_group=logs.LogGroup(
            scope,
            log_group_id,
            retention=log_retention,
            removal_policy=log_removal_policy,
        ),
    )
    if memory_size is not None:
        kwargs["memory_size"] = memory_size
    return lambda_.Function(scope, construct_id, **kwargs)
