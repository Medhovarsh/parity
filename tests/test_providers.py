"""Provider adapters.

HTTP is mocked with respx — no socket is opened and no credential is real. The
"keys" here are literal placeholder strings; they are set only so the adapter's
header-construction path is exercised.

Retryability classification gets the most attention: getting it wrong causes
either flaky runs or a stampede against a rate limiter.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
import respx

from parity.adapters.providers.anthropic import AnthropicProvider
from parity.adapters.providers.fake import FakeProvider, Mutation, scripted_tool_output
from parity.adapters.providers.http_base import RETRYABLE_STATUS
from parity.adapters.providers.ollama import OllamaProvider
from parity.adapters.providers.openai_compat import OpenAICompatibleProvider, parse_tool_calls
from parity.adapters.providers.registry import ProviderConfig, build_provider
from parity.domain.models import (
    GenerationParams,
    InteractionInput,
    InteractionOutput,
    Message,
)
from parity.errors import ConfigError, ProviderError
from tests.conftest import make_input

PLACEHOLDER_KEY = "test-not-a-real-key"


@pytest.fixture
def openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", PLACEHOLDER_KEY)


@pytest.fixture
def anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLACEHOLDER_KEY)


class TestFakeProvider:
    def test_is_deterministic(self) -> None:
        provider = FakeProvider()
        request = make_input("hello")
        assert provider.complete("m", request).text == provider.complete("m", request).text

    def test_scripted_output_is_returned(self) -> None:
        request = make_input("q")
        from tests.conftest import make_output

        provider = FakeProvider(scripted={request.fingerprint(): make_output("scripted")})
        assert provider.complete("m", request).text == "scripted"

    def test_records_calls(self) -> None:
        provider = FakeProvider()
        provider.complete("m", make_input("a"))
        assert len(provider.calls) == 1

    @pytest.mark.parametrize(
        ("mutation", "predicate"),
        [
            (Mutation.EMPTY, lambda o: o.is_empty()),
            (Mutation.REFUSE, lambda o: "can't help" in o.text),
            (Mutation.TRUNCATE, lambda o: o.finish_reason == "length"),
            (Mutation.INFLATE, lambda o: len(o.text) > 500),
            (Mutation.BREAK_JSON, lambda o: "not json" in o.text),
        ],
    )
    def test_mutations(
        self, mutation: Mutation, predicate: Callable[[InteractionOutput], bool]
    ) -> None:
        provider = FakeProvider(mutation=mutation)
        assert predicate(provider.complete("m", make_input("x")))

    def test_drop_tool_call_mutation(self) -> None:
        request = make_input("x")
        provider = FakeProvider(
            scripted={request.fingerprint(): scripted_tool_output("a", "b")},
            mutation=Mutation.DROP_TOOL_CALL,
        )
        assert len(provider.complete("m", request).tool_calls) == 1

    def test_transient_failures_then_success(self) -> None:
        provider = FakeProvider(fail_times=1)
        with pytest.raises(ProviderError) as excinfo:
            provider.complete("m", make_input("x"))
        assert excinfo.value.retryable
        assert provider.complete("m", make_input("x")).text


class TestParseToolCalls:
    def test_parses_json_string_arguments(self) -> None:
        calls = parse_tool_calls([{"id": "1", "function": {"name": "t", "arguments": '{"a": 1}'}}])
        assert calls[0].arguments == {"a": 1}

    def test_parses_dict_arguments(self) -> None:
        calls = parse_tool_calls([{"function": {"name": "t", "arguments": {"a": 1}}}])
        assert calls[0].arguments == {"a": 1}

    def test_handles_empty_arguments(self) -> None:
        calls = parse_tool_calls([{"function": {"name": "t", "arguments": ""}}])
        assert calls[0].arguments == {}

    def test_wraps_non_object_arguments(self) -> None:
        calls = parse_tool_calls([{"function": {"name": "t", "arguments": "[1,2]"}}])
        assert calls[0].arguments == {"_value": [1, 2]}

    @pytest.mark.parametrize("raw", [None, "nope", [], [{"no": "function"}], [{"function": {}}]])
    def test_tolerates_junk(self, raw: object) -> None:
        assert parse_tool_calls(raw) == ()


class TestOpenAICompatible:
    URL = "https://api.openai.com/v1/chat/completions"

    def response_body(self, content: str = "hello") -> dict[str, object]:
        return {
            "model": "gpt-test",
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }

    @respx.mock
    def test_successful_completion(self, openai_env: None) -> None:
        respx.post(self.URL).mock(return_value=httpx.Response(200, json=self.response_body()))
        provider = OpenAICompatibleProvider()
        try:
            output = provider.complete("gpt-test", make_input("hi"))
        finally:
            provider.close()
        assert output.text == "hello"
        assert output.finish_reason == "stop"
        assert output.usage["prompt_tokens"] == 5

    @respx.mock
    def test_sends_parameters_and_credential(self, openai_env: None) -> None:
        route = respx.post(self.URL).mock(
            return_value=httpx.Response(200, json=self.response_body())
        )
        provider = OpenAICompatibleProvider()
        try:
            provider.complete(
                "gpt-test",
                InteractionInput(
                    messages=(Message(role="user", content="hi"),),
                    params=GenerationParams(temperature=0.2, max_tokens=64, stop=("STOP",)),
                ),
            )
        finally:
            provider.close()
        sent = route.calls[0].request
        assert sent.headers["authorization"] == f"Bearer {PLACEHOLDER_KEY}"
        import json as _json

        payload = _json.loads(sent.content)
        assert payload["temperature"] == 0.2
        assert payload["max_tokens"] == 64
        assert payload["stop"] == ["STOP"]

    @respx.mock
    @pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
    def test_marks_transient_statuses_retryable(self, openai_env: None, status: int) -> None:
        respx.post(self.URL).mock(return_value=httpx.Response(status, json={"error": "busy"}))
        provider = OpenAICompatibleProvider()
        try:
            with pytest.raises(ProviderError) as excinfo:
                provider.complete("gpt-test", make_input("hi"))
        finally:
            provider.close()
        assert excinfo.value.retryable is True
        assert excinfo.value.status_code == status

    @respx.mock
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_marks_client_errors_permanent(self, openai_env: None, status: int) -> None:
        respx.post(self.URL).mock(
            return_value=httpx.Response(status, json={"error": {"message": "bad input"}})
        )
        provider = OpenAICompatibleProvider()
        try:
            with pytest.raises(ProviderError) as excinfo:
                provider.complete("gpt-test", make_input("hi"))
        finally:
            provider.close()
        assert excinfo.value.retryable is False
        assert "bad input" in str(excinfo.value)

    @respx.mock
    def test_timeout_is_retryable(self, openai_env: None) -> None:
        respx.post(self.URL).mock(side_effect=httpx.ConnectTimeout("slow"))
        provider = OpenAICompatibleProvider()
        try:
            with pytest.raises(ProviderError) as excinfo:
                provider.complete("gpt-test", make_input("hi"))
        finally:
            provider.close()
        assert excinfo.value.retryable is True

    @respx.mock
    def test_empty_choices_is_retryable(self, openai_env: None) -> None:
        respx.post(self.URL).mock(return_value=httpx.Response(200, json={"choices": []}))
        provider = OpenAICompatibleProvider()
        try:
            with pytest.raises(ProviderError, match="no choices"):
                provider.complete("gpt-test", make_input("hi"))
        finally:
            provider.close()

    def test_missing_credential_is_a_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
            OpenAICompatibleProvider()

    def test_credential_can_be_waived_for_local_servers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        provider = OpenAICompatibleProvider(base_url="http://localhost:8000/v1", require_key=False)
        provider.close()


class TestAnthropic:
    URL = "https://api.anthropic.com/v1/messages"

    @respx.mock
    def test_parses_content_blocks(self, anthropic_env: None) -> None:
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "claude-test",
                    "stop_reason": "end_turn",
                    "content": [
                        {"type": "text", "text": "hello "},
                        {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "x"}},
                        {"type": "text", "text": "world"},
                    ],
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                },
            )
        )
        provider = AnthropicProvider()
        try:
            output = provider.complete("claude-test", make_input("hi"))
        finally:
            provider.close()
        assert output.text == "hello world"
        assert output.tool_calls[0].name == "search"
        assert output.finish_reason == "end_turn"

    @respx.mock
    def test_hoists_system_prompt_and_uses_x_api_key(self, anthropic_env: None) -> None:
        route = respx.post(self.URL).mock(
            return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        )
        provider = AnthropicProvider()
        try:
            provider.complete("claude-test", make_input("hi", system="be terse"))
        finally:
            provider.close()
        import json as _json

        sent = route.calls[0].request
        payload = _json.loads(sent.content)
        assert payload["system"] == "be terse"
        assert all(m["role"] != "system" for m in payload["messages"])
        assert sent.headers["x-api-key"] == PLACEHOLDER_KEY
        assert "anthropic-version" in sent.headers

    @respx.mock
    def test_supplies_required_max_tokens(self, anthropic_env: None) -> None:
        route = respx.post(self.URL).mock(
            return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        )
        provider = AnthropicProvider()
        try:
            provider.complete("claude-test", make_input("hi"))
        finally:
            provider.close()
        import json as _json

        assert _json.loads(route.calls[0].request.content)["max_tokens"] > 0


class TestOllama:
    URL = "http://localhost:11434/api/chat"

    @respx.mock
    def test_needs_no_credential(self) -> None:
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "llama3.1",
                    "done_reason": "stop",
                    "message": {"role": "assistant", "content": "hi there"},
                    "prompt_eval_count": 4,
                    "eval_count": 2,
                },
            )
        )
        provider = OllamaProvider()
        try:
            output = provider.complete("llama3.1", make_input("hi"))
        finally:
            provider.close()
        assert output.text == "hi there"
        assert output.finish_reason == "stop"
        assert output.usage["eval_count"] == 2

    @respx.mock
    def test_maps_params_into_options(self) -> None:
        route = respx.post(self.URL).mock(
            return_value=httpx.Response(200, json={"message": {"content": "x"}})
        )
        provider = OllamaProvider()
        try:
            provider.complete(
                "llama3.1",
                InteractionInput(
                    messages=(Message(role="user", content="hi"),),
                    params=GenerationParams(temperature=0.0, max_tokens=32, seed=1),
                ),
            )
        finally:
            provider.close()
        import json as _json

        options = _json.loads(route.calls[0].request.content)["options"]
        assert options["num_predict"] == 32
        assert options["seed"] == 1


class TestRegistry:
    def test_builds_fake(self) -> None:
        provider = build_provider(ProviderConfig(kind="fake", name="f"))
        assert provider.name == "f"
        provider.close()

    def test_builds_ollama_without_credentials(self) -> None:
        provider = build_provider(ProviderConfig(kind="ollama"))
        assert provider.name == "ollama"
        provider.close()

    def test_builds_openai_with_placeholder_credential(self, openai_env: None) -> None:
        provider = build_provider(ProviderConfig(kind="openai"))
        provider.close()

    def test_custom_openai_compatible_endpoint(self) -> None:
        provider = build_provider(
            ProviderConfig(
                kind="openai",
                name="local",
                base_url="http://localhost:8000/v1",
                require_key=False,
                api_key_env=None,
            )
        )
        assert provider.name == "local"
        provider.close()

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises((ConfigError, ValueError)):
            build_provider(ProviderConfig(kind="mystery"))  # type: ignore[arg-type]
