"""Regression checks for API tester URL handling and model parsing."""
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
