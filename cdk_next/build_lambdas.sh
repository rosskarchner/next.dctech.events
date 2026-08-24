#!/usr/bin/env bash
# Build Lambda deployment asset directories under cdk_next/build/.
# Run before `cdk deploy`. Uses uv to cross-install manylinux wheels so
# compiled deps (pydantic-core, python-crfsuite, ...) match the Lambda
# runtime (python3.12 / x86_64).
set -euo pipefail
cd "$(dirname "$0")"

# calgen lives in this repo (packages/calgen) and is maintained here — there is
# no upstream to pull from, so no external checkout is needed or possible.
CALGEN="$(cd ../packages/calgen && pwd)"
# calgen's version never changes, so uv happily reuses a cached wheel built
# from older source and silently ships stale code. Always rebuild it.
CALGEN_FRESH=(--refresh-package calgen --reinstall-package calgen)

PY_VERSION=3.12
PLATFORM=x86_64-manylinux_2_17
UV_ARGS=(--python-platform "$PLATFORM" --python-version "$PY_VERSION" --link-mode=copy --only-binary python-crfsuite --only-binary pydantic-core --only-binary regex)

rm -rf build
mkdir -p build

# ── api ──────────────────────────────────────────────────────────────
mkdir -p build/api
cp -r lambda_src/api/. build/api/
# Tests live next to the handlers; they have no business in the bundle.
rm -rf build/api/test_*.py build/api/__pycache__ build/api/routes/__pycache__
uv pip install "${UV_ARGS[@]}" --target build/api jinja2 pytz

# ── mcp ──────────────────────────────────────────────────────────────
mkdir -p build/mcp
cp -r lambda_src/mcp/. build/mcp/
cp lambda_src/api/db.py lambda_src/api/event_utils.py build/mcp/
# Tests live next to the server; they have no business in the bundle.
rm -rf build/mcp/test_*.py build/mcp/__pycache__
uv pip install "${UV_ARGS[@]}" --target build/mcp "mcp>=1.9,<2" mangum requests

# ── ical_aggregator ─────────────────────────────────────────────────
if [ -d lambda_src/ical_aggregator ] && [ -e lambda_src/ical_aggregator/handler.py ]; then
  mkdir -p build/ical_aggregator
  cp -r lambda_src/ical_aggregator/. build/ical_aggregator/
  cp lambda_src/api/db.py lambda_src/api/event_utils.py build/ical_aggregator/
  # calgen imported directly from packages/calgen (not re-implemented) + deps
  uv pip install "${UV_ARGS[@]}" --target build/ical_aggregator \
    "${CALGEN_FRESH[@]}" "$CALGEN" requests icalendar pytz recurring-ical-events \
    beautifulsoup4 pyyaml python-dateutil dateparser usaddress
fi

# ── newsletter ──────────────────────────────────────────────────────
if [ -d lambda_src/newsletter ] && [ -e lambda_src/newsletter/app.py ]; then
  mkdir -p build/newsletter
  cp -r lambda_src/newsletter/. build/newsletter/
  cp lambda_src/api/db.py build/newsletter/
  # render.py rebuilds the calgen site in /tmp from DynamoDB
  cp lambda_src/site_generator/export_dynamo_to_calgen.py build/newsletter/
  cp -r ../site build/newsletter/site
  uv pip install "${UV_ARGS[@]}" "${CALGEN_FRESH[@]}" --target build/newsletter "$CALGEN"
fi

# ── ops (scheduled maintenance: cognito pruning, queue email) ───────
if [ -d lambda_src/ops ]; then
  mkdir -p build/ops
  cp -r lambda_src/ops/. build/ops/
  cp lambda_src/api/db.py build/ops/
fi

# ── updates_publisher (weekly /updates post; stdlib + boto3 only) ───
if [ -e lambda_src/updates_publisher/app.py ]; then
  mkdir -p build/updates_publisher
  cp -r lambda_src/updates_publisher/. build/updates_publisher/
  # Tests live next to the handler; they have no business in the bundle.
  rm -rf build/updates_publisher/test_*.py build/updates_publisher/__pycache__
fi

# ── social_publisher (cross-posts /updates; stdlib + boto3 only) ────
if [ -e lambda_src/social_publisher/app.py ]; then
  mkdir -p build/social_publisher
  cp -r lambda_src/social_publisher/. build/social_publisher/
  # Tests live next to the handler; they have no business in the bundle.
  rm -rf build/social_publisher/test_*.py build/social_publisher/__pycache__
fi

# ── qa_trigger (invokes the AgentCore runtime; boto3 only) ──────────
if [ -e lambda_src/qa_trigger/handler.py ]; then
  mkdir -p build/qa_trigger
  cp -r lambda_src/qa_trigger/. build/qa_trigger/
  # Tests live next to the handler; they have no business in the bundle.
  rm -rf build/qa_trigger/test_*.py build/qa_trigger/__pycache__
fi

# ── discovery_trigger (invokes the discovery AgentCore runtime) ──────
if [ -e lambda_src/discovery_trigger/handler.py ]; then
  mkdir -p build/discovery_trigger
  cp -r lambda_src/discovery_trigger/. build/discovery_trigger/
  rm -rf build/discovery_trigger/test_*.py build/discovery_trigger/__pycache__
fi

# ── calendar_qc (AgentCore Runtime asset) ───────────────────────────
# NOT a Lambda: AgentCore Runtime is Graviton-only, so this one target builds
# aarch64 wheels. Installing the x86_64 set used everywhere else above yields
# a runtime that fails at cold start with an ELF class error.
if [ -d agents/calendar_qc ]; then
  mkdir -p build/calendar_qc
  cp -r agents/calendar_qc/. build/calendar_qc/
  rm -rf build/calendar_qc/test_*.py build/calendar_qc/__pycache__
  # --only-binary :all: is not just speed here. A source build would compile
  # against this machine's architecture and silently ship x86_64 objects to a
  # Graviton runtime; failing the build is the correct outcome instead.
  #
  # This lands around 280M unpacked / 92M zipped, mostly playwright's bundled
  # node driver — which the browser tool needs even though the browser itself
  # runs remotely. AgentCore direct-code limits are 250M zipped / 750M
  # unpacked, so there is room, but check both if dependencies grow.
  uv pip install --python-platform aarch64-manylinux_2_17 \
    --python-version "$PY_VERSION" --link-mode=copy --only-binary :all: \
    --target build/calendar_qc \
    strands-agents "strands-agents-tools[agent_core_browser]" \
    bedrock-agentcore "mcp>=1.9,<2" httpx
fi

# ── discovery (AgentCore Runtime asset) ──────────────────────────────
if [ -d agents/discovery ]; then
  mkdir -p build/discovery
  cp -r agents/discovery/. build/discovery/
  rm -rf build/discovery/test_*.py build/discovery/__pycache__
  uv pip install --python-platform aarch64-manylinux_2_17 \
    --python-version "$PY_VERSION" --link-mode=copy --only-binary :all: \
    --target build/discovery \
    strands-agents "strands-agents-tools[agent_core_browser]" \
    bedrock-agentcore "mcp>=1.9,<2" httpx aiohttp
fi

# ── site_generator trigger (pure stdlib + boto3) ────────────────────
if [ -e lambda_src/site_generator/trigger/handler.py ]; then
  mkdir -p build/site_generator_trigger
  cp -r lambda_src/site_generator/trigger/. build/site_generator_trigger/
fi

# ── site_generator CodeBuild source (site files + export + calgen wheel) ──
mkdir -p build/site_src/wheels
cp -r ../site build/site_src/site
cp lambda_src/site_generator/export_dynamo_to_calgen.py build/site_src/
uv build --quiet --no-cache --wheel "$CALGEN" --out-dir build/site_src/wheels

echo "Lambda assets built under cdk_next/build/"
