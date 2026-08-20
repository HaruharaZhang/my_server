import os
import sys
import types
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).parents[1]))
if "httpx" not in sys.modules:
    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=mock.Mock())
if "openai" not in sys.modules:
    sys.modules["openai"] = types.SimpleNamespace(AsyncOpenAI=mock.Mock())

import llm


class ModelNetworkTests(TestCase):
    def test_model_client_explicitly_ignores_proxy_environment(self):
        cfg = {"api": {"api_key_env": "TEST_KEY", "base_url": "https://model.test/v1"}}
        with mock.patch.dict(os.environ, {
            "TEST_KEY": "masked-test-value", "HTTP_PROXY": "http://bad:1",
            "HTTPS_PROXY": "http://bad:2", "ALL_PROXY": "http://bad:3",
        }):
            with mock.patch("llm.httpx.AsyncClient") as http_client, mock.patch("llm.AsyncOpenAI") as openai:
                llm.make_client(cfg)
        http_client.assert_called_once_with(trust_env=False)
        self.assertIs(openai.call_args.kwargs["http_client"], http_client.return_value)
