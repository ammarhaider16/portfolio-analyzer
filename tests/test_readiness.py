"""FR-15 model readiness probe and endpoint checks; faked chat model, no network."""
import asyncio
import os
import re

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from portfolio_analyzer.secrets import SecretStore
from portfolio_analyzer.service import probe_model_readiness, readiness_ok
from portfolio_analyzer.web import create_app


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    values = {}
    for key in list(os.environ):
        if key.startswith('PORTFOLIO_') or key in {'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'LITELLM_API_KEY', 'TAVILY_API_KEY'}:
            monkeypatch.delenv(key)
    monkeypatch.setattr(SecretStore, 'get', lambda self, name: os.environ.get('PORTFOLIO_SECRET_' + name.upper(), values.get(name)))
    monkeypatch.setattr(SecretStore, 'set', lambda self, name, value: values.__setitem__(name, value))
    monkeypatch.setattr(SecretStore, 'delete', lambda self, name: values.pop(name, None))
    return values


class FakeChatModel(BaseChatModel):
    """Chat-model double returning canned tool calls, canned text, or errors."""
    script: list
    seen: list = Field(default_factory=list)
    tool_schemas: list = Field(default_factory=list)
    cursor: int = 0

    @property
    def _llm_type(self):
        return "fake-readiness-model"

    def bind_tools(self, tools, **kwargs):
        self.tool_schemas.append(list(tools))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        step = self.script[self.cursor]
        self.cursor += 1
        if step.get("error") is not None:
            raise step["error"]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=step.get("content", ""), tool_calls=step.get("tool_calls") or []))])


def tool_call():
    return [{"name": "record_probe_note", "args": {"note": "ready"}, "id": "probe-1", "type": "tool_call"}]


def run_probe(script):
    model = FakeChatModel(script=script)
    return asyncio.run(probe_model_readiness(None, None, model=model)), model


def csrf(client):
    response = client.get('/')
    assert response.status_code == 200
    return re.search(r'name="csrf-token" content="([^"]+)"', response.text).group(1)


def post(client, token, path, body):
    return client.post(path, json=body, headers={'x-csrf-token': token})


def final_call(summary="ready"):
    return [{"name": "ResearchNarrative", "args": {"executive_summary": summary,
             "sections": [], "suggestions": []}, "id": "final", "type": "tool_call"}]


def test_readiness_uses_actual_briefing_schema_and_tool_result_round_trip():
    result, model = run_probe([{"tool_calls": tool_call()}, {"tool_calls": final_call()}])
    assert result["ok"] is True
    assert result["tool_calling"] == result["structured_output"] == "ok"
    assert model.cursor == 2
    assert model.tool_schemas[0][0].name == "record_probe_note"
    assert model.tool_schemas[1][0].__name__ == "ResearchNarrative"
    assert any(message.type == "tool" and message.tool_call_id == "probe-1" for message in model.seen[1])
    assert all("AAPL" not in str(messages) for messages in model.seen)


def test_readiness_rejects_json_text_that_never_calls_finalize_tool():
    result, _ = run_probe([{"tool_calls": tool_call()}, {"content": '{"summary":"ready","ok":true}'}])
    assert result["tool_calling"] == "ok"
    assert result["structured_output"] == "fail"
    assert result["ok"] is False


@pytest.mark.parametrize("calls", [[], [{"name":"unrelated","args":{},"id":"bad","type":"tool_call"}]])
def test_readiness_does_not_accept_an_unrelated_tool_call(calls):
    result, _ = run_probe([{"tool_calls": calls}, {"tool_calls": final_call()}])
    assert result["tool_calling"] == "weak"


def test_readiness_rejects_invalid_briefing_shape():
    invalid = [{"name":"ResearchNarrative","args":{"findings":"wrong shape"},"id":"bad","type":"tool_call"}]
    result, _ = run_probe([{"tool_calls": tool_call()}, {"tool_calls": invalid}])
    assert not result["ok"]
    assert result["structured_output"] == "fail"


def test_readiness_errors_do_not_expose_provider_messages():
    result, model = run_probe([{"error":Exception("bad key secret-abc-123")}] * 2)
    assert not result["ok"] and model.cursor == 2
    assert "secret-abc-123" not in result["detail"]
    assert readiness_ok("ok", "ok") and not readiness_ok("ok", "weak") and not readiness_ok("ok", "fail")


def test_fr15_endpoint_contract(tmp_path, monkeypatch):
    from portfolio_analyzer import service as service_module
    fake = FakeChatModel(script=[{"tool_calls": tool_call()}, {"tool_calls": final_call()}])
    monkeypatch.setattr(service_module, 'create_model', lambda *args, **kwargs: fake)
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        token = csrf(client)
        # Conventions match neighboring endpoints: CSRF for owner browser writes.
        assert client.post('/api/model/readiness', json={}).status_code == 403
        response = post(client, token, '/api/model/readiness', {})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert set(payload) == {'ok', 'tool_calling', 'structured_output', 'detail'}
        assert payload['ok'] is True and payload['tool_calling'] == 'ok' and payload['structured_output'] == 'ok'
        assert isinstance(payload['detail'], str) and payload['detail']


def test_fr15_endpoint_without_key_is_an_actionable_400(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        response = post(client, csrf(client), '/api/model/readiness', {})
        assert response.status_code == 400
        assert 'API key' in response.json()['detail']
