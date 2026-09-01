"""Tests for handler.py's CORS origin handling.

get_cors_origin() must default-deny for origins outside ALLOWED_ORIGINS
(return None, no header) rather than fall back to a wildcard -- a wildcard
here would let any site's JS read every response, including /api/admin/*
JSON, from a browser holding a leaked bearer token.

Run: DYNAMODB_TABLE_NAME=t python -m pytest test_handler.py
"""
import os

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import handler  # noqa: E402


def _event(origin=None, method='GET', path='/api/events'):
    headers = {'Origin': origin} if origin else {}
    return {'httpMethod': method, 'path': path, 'headers': headers}


def test_allowlisted_origin_is_echoed_back():
    assert handler.get_cors_origin(_event('https://dctech.events')) == 'https://dctech.events'


def test_unrecognized_origin_gets_no_header_not_a_wildcard():
    assert handler.get_cors_origin(_event('https://evil.example')) is None


def test_missing_origin_gets_no_header():
    assert handler.get_cors_origin(_event(None)) is None


def test_add_cors_omits_header_for_unrecognized_origin():
    event = _event('https://evil.example')
    cors_origin = handler.get_cors_origin(event)

    def add_cors(response):
        if cors_origin:
            response.setdefault('headers', {})['Access-Control-Allow-Origin'] = cors_origin
        return response

    response = add_cors({'statusCode': 200, 'body': '{}'})
    assert 'Access-Control-Allow-Origin' not in response.get('headers', {})


def test_options_preflight_omits_origin_header_for_unrecognized_origin():
    response = handler.lambda_handler(_event('https://evil.example', method='OPTIONS'), None)
    assert 'Access-Control-Allow-Origin' not in response['headers']


def test_options_preflight_echoes_allowlisted_origin():
    response = handler.lambda_handler(_event('https://dctech.events', method='OPTIONS'), None)
    assert response['headers']['Access-Control-Allow-Origin'] == 'https://dctech.events'
