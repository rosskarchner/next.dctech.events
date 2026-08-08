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

# ── qa_agent / discovery_agent (stub scaffolds, no deps) ────────────
for agent in qa_agent discovery_agent; do
  if [ -e "lambda_src/$agent/handler.py" ]; then
    mkdir -p "build/$agent"
    cp -r "lambda_src/$agent/." "build/$agent/"
    cp lambda_src/api/db.py "build/$agent/"
  fi
done

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
