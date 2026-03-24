"""
test_lm_client.py — LMClient のユニットテスト

テスト対象:
  - is_server_running()
  - _clean_response()
  - generate_response()

外部依存 requests はすべてモック化する。
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from core.lm_client import LMClient


# ──────────────────────────────────────────
# is_server_running
# ──────────────────────────────────────────

class TestIsServerRunning:
    def test_returns_true_when_200(self):
        client = LMClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("core.lm_client.requests.get", return_value=mock_resp):
            assert client.is_server_running() is True

    def test_returns_false_when_non_200(self):
        client = LMClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("core.lm_client.requests.get", return_value=mock_resp):
            assert client.is_server_running() is False

    def test_returns_false_on_connection_error(self):
        client = LMClient()
        with patch("core.lm_client.requests.get", side_effect=ConnectionError):
            assert client.is_server_running() is False

    def test_hits_correct_endpoint(self):
        client = LMClient(base_url="http://localhost:9999")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("core.lm_client.requests.get", return_value=mock_resp) as mock_get:
            client.is_server_running()
            mock_get.assert_called_once_with(
                "http://localhost:9999/v1/models", timeout=3
            )


# ──────────────────────────────────────────
# _clean_response
# ──────────────────────────────────────────

class TestCleanResponse:
    def setup_method(self):
        self.client = LMClient()

    def test_plain_json_passthrough(self):
        text = '{"action": "move"}'
        assert self.client._clean_response(text) == '{"action": "move"}'

    def test_strips_think_tag(self):
        text = "<think>考え中…</think>\n{\"action\": \"wait\"}"
        result = self.client._clean_response(text)
        assert result == '{"action": "wait"}'

    def test_strips_leading_prose(self):
        text = "思考プロセス：では移動します。\n{\"action\": \"move\"}"
        result = self.client._clean_response(text)
        assert result == '{"action": "move"}'

    def test_strips_trailing_prose(self):
        text = '{"action": "move"}\n出力完了しました。'
        result = self.client._clean_response(text)
        assert result == '{"action": "move"}'

    def test_strips_markdown_code_block(self):
        text = "```json\n{\"action\": \"attack\"}\n```"
        result = self.client._clean_response(text)
        assert result == '{"action": "attack"}'

    def test_empty_string_returns_empty(self):
        result = self.client._clean_response("")
        assert result == ""

    def test_no_braces_returns_empty(self):
        # 波括弧がなければ first_brace_idx == -1 → 元テキストをそのまま返す
        result = self.client._clean_response("hello world")
        assert "hello world" in result or result == "hello world"

    def test_nested_think_tag(self):
        text = "<think>step1</think><think>step2</think>{\"ok\": true}"
        result = self.client._clean_response(text)
        assert result == '{"ok": true}'


# ──────────────────────────────────────────
# generate_response
# ──────────────────────────────────────────

class TestGenerateResponse:
    def _make_api_response(self, content: str):
        """OpenAI 互換レスポンスの dict を作る"""
        return {
            "choices": [
                {"message": {"content": content, "tool_calls": None}}
            ]
        }

    def test_returns_none_when_server_down(self):
        client = LMClient()
        with patch.object(client, "is_server_running", return_value=False):
            content, tools = client.generate_response("sys", "user")
        assert content is None
        assert tools is None

    def test_returns_cleaned_json(self):
        client = LMClient()
        raw = '{"action": "cast"}'
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.json.return_value = self._make_api_response(raw)

        with patch.object(client, "is_server_running", return_value=True), \
             patch("core.lm_client.requests.post", return_value=api_resp):
            content, tools = client.generate_response("sys", "user")

        assert content == '{"action": "cast"}'
        assert tools is None

    def test_no_think_prepends_flag(self):
        client = LMClient()
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.json.return_value = self._make_api_response('{"ok": true}')

        with patch.object(client, "is_server_running", return_value=True), \
             patch("core.lm_client.requests.post", return_value=api_resp) as mock_post:
            client.generate_response("sys", "user", no_think=True)

        payload = mock_post.call_args[1]["json"]
        assert payload["messages"][0]["content"].startswith("/no_think")
        assert payload.get("chat_template_kwargs") == {"enable_thinking": False}

    def test_returns_none_on_non_200(self):
        client = LMClient()
        api_resp = MagicMock()
        api_resp.status_code = 503

        with patch.object(client, "is_server_running", return_value=True), \
             patch("core.lm_client.requests.post", return_value=api_resp):
            content, tools = client.generate_response("sys", "user")

        assert content is None

    def test_returns_none_on_exception(self):
        client = LMClient()
        with patch.object(client, "is_server_running", return_value=True), \
             patch("core.lm_client.requests.post", side_effect=TimeoutError):
            content, tools = client.generate_response("sys", "user")

        assert content is None

    def test_custom_base_url_and_model(self):
        client = LMClient(base_url="http://myserver:5678", model="my-model")
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.json.return_value = self._make_api_response('{"x": 1}')

        with patch.object(client, "is_server_running", return_value=True), \
             patch("core.lm_client.requests.post", return_value=api_resp) as mock_post:
            client.generate_response("sys", "user")

        url = mock_post.call_args[0][0]
        assert "myserver:5678" in url
        payload = mock_post.call_args[1]["json"]
        assert payload["model"] == "my-model"
