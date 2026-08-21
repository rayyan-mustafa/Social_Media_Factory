import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.demo_chat_proxy as demo_chat_proxy


def test_forward_openrouter_uses_openrouter_free_and_reasoning():
    demo_chat_proxy.OPENROUTER = "test-key"

    fake_response = Mock()
    fake_response.__enter__ = Mock(return_value=fake_response)
    fake_response.__exit__ = Mock(return_value=None)
    fake_response.read.return_value = json.dumps({
        "id": "abc",
        "choices": [{"message": {"content": "hello"}}],
    }).encode("utf-8")

    with patch.object(demo_chat_proxy.request, "Request") as request_cls, patch.object(
        demo_chat_proxy.request, "urlopen", return_value=fake_response
    ) as urlopen_mock:
        request_cls.return_value = object()

        result = demo_chat_proxy.forward_openrouter({
            "model": "openrouter/free",
            "max_tokens": 250,
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "Hi"}],
        })

    assert result["content"][0]["text"] == "hello"
    body = json.loads(request_cls.call_args.kwargs["data"].decode("utf-8"))
    assert body["model"] == "openrouter/free"
    assert body["reasoning"] == {"enabled": True}
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    urlopen_mock.assert_called_once()
