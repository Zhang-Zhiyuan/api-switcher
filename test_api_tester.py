"""Regression checks for API tester URL handling and model parsing."""
import io
import json
import urllib.error
import urllib.request

from core.api_tester import APITester


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


class _FakeHTMLResponse:
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b"<!doctype html><html><body>not an api</body></html>"

    def getcode(self):
        return 200


class _FakeJSONResponse:
    headers = {"Content-Type": "application/json"}

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def getcode(self):
        return self.status


class _FakeStreamResponse:
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, body: str, status=200):
        self.body = body
        self.status = status
        self._lines = iter(body.encode("utf-8").splitlines(keepends=True))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body.encode("utf-8")

    def readline(self):
        return next(self._lines, b"")

    def getcode(self):
        return self.status


class _CompletionThenBlockingStream(_FakeStreamResponse):
    def __init__(self):
        super().__init__("event: response.completed\n")
        self._read_count = 0

    def readline(self):
        self._read_count += 1
        if self._read_count > 1:
            raise AssertionError("stream reader should return as soon as completion is seen")
        return b"event: response.completed\n"


def test_request_json_rejects_html_success_response(monkeypatch):
    def fake_urlopen(_request, timeout):
        return _FakeHTMLResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ok, data, result = APITester._request_json(
        "https://example.com/anthropic/v1/messages",
        headers={"Accept": "application/json"},
    )

    assert ok is False
    assert data is None
    assert result.status_code == 200
    assert "JSON" in result.message
    assert "text/html" in result.error_details


def test_request_json_reports_wrapped_timeout_as_timeout(monkeypatch):
    def fake_urlopen(_request, timeout):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ok, data, result = APITester._request_json(
        "https://relay.example.com/v1/models",
        headers={"Accept": "application/json"},
        timeout=99,
    )

    assert ok is False
    assert data is None
    assert result.message == "连接超时，超过 30 秒"


def test_openai_blank_model_uses_latest_from_models(monkeypatch):
    seen_payloads = []

    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return _FakeJSONResponse({
                "data": [
                    {"id": "gpt-5.2-pro-2025-12-11"},
                    {"id": "gpt-5.4"},
                    {"id": "gpt-5.5"},
                ]
            })
        seen_payloads.append(json.loads(request.data.decode("utf-8")))
        return _FakeJSONResponse({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = APITester.test_openai_api("sk-test", "https://relay.example.com/v1", "", wire_api="chat")

    assert result.success is True
    assert result.selected_model == "gpt-5.5"
    assert seen_payloads[-1]["model"] == "gpt-5.5"


def test_openai_responses_probe_uses_stream_and_requires_completion(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return _FakeJSONResponse({"data": [{"id": "gpt-5.5"}]})
        seen["url"] = request.full_url
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        seen["accept"] = request.headers.get("Accept")
        return _FakeStreamResponse(
            "event: response.output_text.delta\n"
            'data: {"delta":"OK"}\n\n'
            "event: response.completed\n"
            'data: {"type":"response.completed"}\n\n'
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = APITester.test_openai_api("sk-test", "https://relay.example.com/v1", "", wire_api="responses")

    assert result.success is True
    assert result.selected_model == "gpt-5.5"
    assert seen["url"] == "https://relay.example.com/v1/responses"
    assert seen["payload"]["stream"] is True
    assert seen["payload"]["max_output_tokens"] == 96
    assert seen["accept"] == "text/event-stream"


def test_openai_responses_probe_flags_incomplete_stream(monkeypatch):
    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return _FakeJSONResponse({"data": [{"id": "gpt-5.5"}]})
        return _FakeStreamResponse("event: response.output_text.delta\ndata: {\"delta\":\"OK\"}\n\n")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = APITester.test_openai_api("sk-test", "https://relay.example.com/v1", "", wire_api="responses")

    assert result.success is False
    assert "before completion" in result.message
    assert "text/event-stream" in result.error_details


def test_openai_responses_probe_returns_on_completion_without_waiting_for_eof(monkeypatch):
    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return _FakeJSONResponse({"data": [{"id": "gpt-5.5"}]})
        return _CompletionThenBlockingStream()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = APITester.test_openai_api("sk-test", "https://relay.example.com/v1", "", wire_api="responses")

    assert result.success is True


def test_openai_responses_probe_flags_spaced_error_type(monkeypatch):
    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return _FakeJSONResponse({"data": [{"id": "gpt-5.5"}]})
        return _FakeStreamResponse('event: response.failed\ndata: {"type": "error", "message": "boom"}\n\n')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = APITester.test_openai_api("sk-test", "https://relay.example.com/v1", "", wire_api="responses")

    assert result.success is False
    assert "returned an error" in result.message


def test_benchmark_openai_wire_apis_recommends_stable_chat(monkeypatch):
    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return _FakeJSONResponse({"data": [{"id": "gpt-5.5"}]})
        if request.full_url.endswith("/responses"):
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "bad gateway",
                hdrs={},
                fp=io.BytesIO(b'{"error":{"message":"bad_response_body"}}'),
            )
        return _FakeJSONResponse({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = APITester.benchmark_openai_wire_apis(
        "sk-test",
        "https://relay.example.com/v1",
        "",
        repeat_count=3,
    )

    assert result.success is True
    assert result.selected_model == "gpt-5.5"
    assert result.recommended_wire_api == "chat"
    assert "chat: 3/3" in result.error_details
    assert "responses: 0/3" in result.error_details


def test_benchmark_openai_wire_apis_skips_invalid_candidates(monkeypatch):
    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return _FakeJSONResponse({"data": [{"id": "gpt-5.5"}]})
        raise AssertionError("invalid wire_api candidates should not be probed")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = APITester.benchmark_openai_wire_apis(
        "sk-test",
        "https://relay.example.com/v1",
        "gpt-5.5",
        repeat_count="bad",
        wire_apis=("bad", ""),
    )

    assert result.success is False
    assert result.message == "没有可测试的 wire_api"
    assert "bad: 已跳过" in result.error_details


def test_benchmark_openai_wire_apis_clamps_repeat_count(monkeypatch):
    probe_count = 0

    def fake_urlopen(request, timeout):
        nonlocal probe_count
        if request.full_url.endswith("/models"):
            return _FakeJSONResponse({"data": [{"id": "gpt-5.5"}]})
        probe_count += 1
        return _FakeJSONResponse({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = APITester.benchmark_openai_wire_apis(
        "sk-test",
        "https://relay.example.com/v1",
        "gpt-5.5",
        repeat_count=99,
        wire_apis=("chat",),
    )

    assert result.success is True
    assert probe_count == APITester.MAX_BENCHMARK_REPEAT
    assert "chat: 5/5" in result.error_details


def main():
    assert_equal(
        APITester._openai_url("https://api.openai.com", "models"),
        "https://api.openai.com/v1/models",
        "openai default v1 models url",
    )
    assert_equal(
        APITester._openai_url("https://api.moonshot.ai/v1", "models"),
        "https://api.moonshot.ai/v1/models",
        "kimi v1 models url",
    )
    assert_equal(
        APITester._openai_url("https://api.deepseek.com", "chat/completions"),
        "https://api.deepseek.com/chat/completions",
        "deepseek chat url",
    )
    assert_equal(
        APITester._openai_url("https://open.bigmodel.cn/api/coding/paas/v4", "models"),
        "https://open.bigmodel.cn/api/coding/paas/v4/models",
        "glm v4 models url",
    )
    assert_equal(
        APITester._anthropic_url("https://api.deepseek.com/anthropic", "messages"),
        "https://api.deepseek.com/anthropic/v1/messages",
        "deepseek anthropic messages url",
    )

    models = APITester._extract_model_ids({
        "data": [
            {"id": "b"},
            {"name": "a"},
            "c",
            {"model": "a"},
        ]
    })
    assert_equal(models, ["a", "b", "c"], "model extraction and dedupe")

    empty = APITester.test_openai_api("", "https://api.openai.com/v1", "gpt-5.5")
    assert_equal(empty.success, False, "empty openai key fails")
    assert_equal(empty.message, "API Key 为空", "empty openai key message")

    print("OK API tester regression checks passed")


if __name__ == "__main__":
    main()
