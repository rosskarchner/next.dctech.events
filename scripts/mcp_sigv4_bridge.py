#!/usr/bin/env python3
"""stdio ↔ SigV4-signed HTTP bridge for the dctech-events MCP server.

The server is deployed behind API Gateway with an AWS_IAM authorizer, so every
request must be SigV4-signed. MCP clients speak stdio and do not sign requests,
hence this shim: it reads newline-delimited JSON-RPC on stdin, signs and POSTs
each message to the endpoint, and writes replies back to stdout.

Kept deliberately thin. The remote server runs stateless with json_response=True
(see lambda_src/mcp/handler.py), so there is no SSE stream to demultiplex and no
Mcp-Session-Id to carry — each POST returns one complete JSON reply. If that
ever changes, this bridge has to grow an SSE parser.

Credentials come from the ordinary boto3 chain (profile, env, SSO), so this
inherits whatever the shell already uses for `aws`.

Usage: mcp_sigv4_bridge.py [--url URL] [--region REGION] [--profile PROFILE]
"""
import argparse
import json
import os
import sys

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

DEFAULT_URL = os.environ.get(
    "DCTECH_MCP_URL",
    "https://3hfrxitpjb.execute-api.us-east-1.amazonaws.com/prod/mcp",
)
DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")
SERVICE = "execute-api"


def log(message):
    # stdout is the JSON-RPC channel and must carry nothing else.
    print(f"[mcp-bridge] {message}", file=sys.stderr, flush=True)


class Bridge:
    def __init__(self, url, region, profile=None):
        self.url = url
        self.region = region
        # An empty AWS_PROFILE is not "no profile" to botocore — it looks up a
        # profile named "" and raises. Normalize it away before it does.
        profile = (profile or "").strip() or None
        if not os.environ.get("AWS_PROFILE", "").strip():
            os.environ.pop("AWS_PROFILE", None)

        try:
            session = boto3.Session(profile_name=profile) if profile else boto3.Session()
            self._credentials = session.get_credentials()
        except Exception as exc:
            # A traceback here surfaces in an MCP client as an opaque startup
            # failure, so fail with something the reader can act on.
            raise SystemExit(f"Could not load AWS credentials: {exc}") from None

        self._session = session
        if self._credentials is None:
            raise SystemExit(
                "No AWS credentials found. Run `aws sso login` (or set "
                "AWS_PROFILE) and restart the MCP client."
            )
        self.http = requests.Session()

    def post(self, payload):
        body = json.dumps(payload)
        headers = {
            "Content-Type": "application/json",
            # The server may reply with either; advertising both keeps it
            # working if json_response is ever turned off upstream.
            "Accept": "application/json, text/event-stream",
        }
        # Re-freeze per request: SSO/STS credentials expire mid-session and
        # boto3 refreshes them behind this call.
        frozen = self._credentials.get_frozen_credentials()
        signed = AWSRequest(method="POST", url=self.url, data=body, headers=headers)
        SigV4Auth(frozen, SERVICE, self.region).add_auth(signed)
        return self.http.post(
            self.url, data=body, headers=dict(signed.headers), timeout=60
        )

    @staticmethod
    def _extract(response):
        """Pull a JSON-RPC object out of a JSON or SSE response body."""
        text = response.text or ""
        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            for line in text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return None
        return json.loads(text) if text.strip() else None

    def handle(self, message):
        is_notification = "id" not in message
        try:
            response = self.post(message)
        except Exception as exc:
            log(f"transport error: {exc}")
            return self._error(message, -32000, f"Transport error: {exc}")

        if response.status_code in (401, 403):
            log(f"auth rejected ({response.status_code}): {response.text[:200]}")
            return self._error(
                message, -32001,
                "AWS rejected the signed request. Check credentials with "
                "`aws sts get-caller-identity`.",
            )

        if response.status_code >= 400:
            log(f"HTTP {response.status_code}: {response.text[:200]}")
            return self._error(
                message, -32000, f"Server returned HTTP {response.status_code}")

        # 202 with an empty body is the normal reply to a notification.
        try:
            parsed = self._extract(response)
        except json.JSONDecodeError as exc:
            log(f"unparseable response: {exc}: {response.text[:200]}")
            return self._error(message, -32700, "Malformed response from server")

        if is_notification:
            return None
        return parsed

    @staticmethod
    def _error(message, code, text):
        if "id" not in message:
            return None  # Notifications get no reply, even on failure.
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": code, "message": text},
        }

    def run(self):
        log(f"bridging stdio → {self.url} (region {self.region})")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                log(f"ignoring unparseable stdin line: {exc}")
                continue

            # A client may batch messages in a JSON array.
            batch = message if isinstance(message, list) else [message]
            replies = [r for r in (self.handle(m) for m in batch) if r is not None]
            if not replies:
                continue

            out = replies if isinstance(message, list) else replies[0]
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    args = parser.parse_args()

    Bridge(args.url, args.region, args.profile).run()


if __name__ == "__main__":
    main()
