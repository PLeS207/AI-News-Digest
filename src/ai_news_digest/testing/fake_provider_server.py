"""ASGI entry point of the OFFLINE fake model provider (compose.offline.yaml, service fake-llm).

    uvicorn ai_news_digest.testing.fake_provider_server:app --host 0.0.0.0 --port 8000
"""

import os

from ai_news_digest.testing.fakes import fake_provider_app

app = fake_provider_app(os.environ.get("FAKE_LLM_LOG", "/data/fake-llm/requests.jsonl"),
                        os.environ.get("FAKE_FEEDS_DIR", "/app/tests/fixtures"))
