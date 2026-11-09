import os

import pytest

os.environ["PAYQUILL_TESTING"] = "1"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    for key in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "ANTHROPIC_API_KEY", "PAYPAL_MODE", "AI_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    from payquill.app import build_service, create_app
    from payquill.config import load_settings

    settings = load_settings(db_path=str(tmp_path / "test.db"), samples_dir=str(tmp_path / "samples"),
                             paypal_mode="mock", ai_provider="offline")
    service = build_service(settings)
    flask_app = create_app(service)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def svc(app):
    return app.config["PAYQUILL"]
