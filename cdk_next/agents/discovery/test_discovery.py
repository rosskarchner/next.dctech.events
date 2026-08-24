"""Tests for discovery agent plumbing."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import main  # noqa: E402


class FakeTool:
    def __init__(self, name):
        self.tool_name = name


ALL_TOOLS = [FakeTool(n) for n in (
    'list_groups', 'list_categories', 'get_events', 'verify_ical_feed',
    'list_discovery_proposals', 'propose_group', 'propose_event',
    'approve_submission', 'delete_single_event',
)]


def _names(tools):
    return {main._tool_name(t) for t in tools}


def test_discovery_gets_only_expected_tools():
    assert _names(main._select(ALL_TOOLS, main._DISCOVERY_TOOLS, dry_run=False)) == \
        main._DISCOVERY_TOOLS


def test_discovery_dry_run_withholds_writes():
    selected = _names(main._select(ALL_TOOLS, main._DISCOVERY_TOOLS, dry_run=True))
    assert selected == {'list_groups', 'list_categories', 'get_events',
                        'verify_ical_feed', 'list_discovery_proposals'}
    assert not selected & main._WRITE_TOOLS


def test_extract_json_handles_fenced_payload():
    text = 'ok ```json\n{"groups":[{"draft_id":"d1"}]}\n```'
    assert main._extract_json(text)['groups'][0]['draft_id'] == 'd1'


def test_extract_json_returns_empty_on_noise():
    assert main._extract_json('nothing parseable') == {}


def test_tool_result_list_shape():
    result = {'content': [{'text': '{"id":"d1"}'}, {'text': '{"id":"d2"}'}],
              'status': 'success', 'toolUseId': 'x'}
    rows = main._tool_result(result, expect_list=True)
    assert [r['id'] for r in rows] == ['d1', 'd2']


def test_tool_result_error_raises():
    result = {'content': [{'text': 'boom'}], 'status': 'error', 'toolUseId': 'x'}
    with pytest.raises(RuntimeError, match='boom'):
        main._tool_result(result)


def test_should_serve_defaults_to_runtime_when_available():
    args = type('Args', (), {'local': False})()
    assert main.should_serve(args, app_available=True) is True


def test_load_search_key_uses_secret_manager(monkeypatch):
    monkeypatch.setenv('SEARCH_SECRET_ARN', 'arn:aws:secretsmanager:...')
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)

    class FakeClient:
        @staticmethod
        def get_secret_value(SecretId):
            return {'SecretString': 'tvly-test'}

    class FakeBoto3:
        @staticmethod
        def client(name):
            assert name == 'secretsmanager'
            return FakeClient()

    monkeypatch.setitem(sys.modules, 'boto3', FakeBoto3)
    main._load_search_key()
    assert os.environ['TAVILY_API_KEY'] == 'tvly-test'
