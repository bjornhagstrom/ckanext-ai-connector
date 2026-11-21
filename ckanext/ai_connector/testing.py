"""Test a consumer without a key or a network: a client that answers from a list, and a pytest fixture.

    from ckanext.ai_connector import testing

    def test_summary():
        fake = testing.fake_client(["A short summary."])
        assert my_extension.summarise(fake, "Some text") == "A short summary."
        assert fake.calls[0].kind == "text"

``fake_client`` returns a real :class:`~ckanext.ai_connector.api.AIClient` (the same checks of the answer, the same second attempt
after a transient failure) whose call is answered by ``answers`` instead of a service. Each answer is

* a string: the text of the answer (for a structured call, the JSON text);
* a dict or list: a structured answer (sent as its JSON text);
* an exception instance: raised by the call (give it a ``status_code`` such as 503 to make it transient);
* :class:`Answer`: a text with more detail, such as a page that was not read;
* a callable taking the :class:`~ckanext.ai_connector.client.Call` and returning one of the above.

Every call is recorded in ``client.calls``. The fixture ``ai_connector_fake`` makes ``api.client()`` return such a client.

The settings saved on the AI settings page are read once per web request and kept in ``flask.g``; CKAN's test plugin keeps one
request open across tests, and CKAN applies saved settings when the test app starts. A consumer whose tests save AI settings
calls :func:`clear_saved_settings` around them (``ckanext-ai-connector``'s own tests do it in their conftest).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ckanext.ai_connector.client import AIClient, Call, Reply


@dataclass
class Answer:
    text: str
    page_read: bool | None = None
    usage: dict | None = None


class FakeClient(AIClient):
    calls: list


def fake_client(answers: Any = (), *, model: str = "gemini/fake-model", consumer: str = "test_consumer",
                max_attempts: int = 2, timeout_seconds: float = 30.0, page_read_timeout_seconds: float = 180.0) -> FakeClient:
    """A client answered by ``answers`` (a list used in order, or one callable for every call)."""
    queue = list(answers) if not callable(answers) else None
    calls: list[Call] = []

    def transport(call: Call) -> Reply:
        calls.append(call)
        if queue is None:
            answer = answers(call)
        elif queue:
            answer = queue.pop(0)
        else:
            raise AssertionError("the fake AI client got more calls than answers")
        if callable(answer) and not isinstance(answer, (Answer, BaseException)):
            answer = answer(call)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, Answer):
            return Reply(text=answer.text, page_read=answer.page_read, usage=answer.usage, model=model)
        if isinstance(answer, (dict, list)):
            answer = json.dumps(answer)
        return Reply(text=str(answer), model=model)

    transport.test_double = True   # an AssertionError raised here reaches the test unwrapped
    client = FakeClient(consumer, model, transport, timeout_seconds=timeout_seconds,
                        page_read_timeout_seconds=page_read_timeout_seconds, max_attempts=max_attempts,
                        retry_delay_seconds=0, sleep_fn=lambda seconds: None)
    client.calls = calls
    return client


def clear_saved_settings() -> None:
    """Delete every AI setting and key saved on the settings page, and forget what was read in this request."""
    from ckan import model

    from ckanext.ai_connector import settings

    column = model.SystemInfo.key
    model.Session.query(model.SystemInfo).filter(
        column.like(settings.PREFIX.replace("_", r"\_") + "%", escape="\\")).delete(synchronize_session=False)
    model.Session.commit()
    settings.forget_rows()


def forget_read_settings() -> None:
    """Read the saved settings again at the next read (they are kept for the request)."""
    from ckanext.ai_connector import settings

    settings.forget_rows()


try:
    import pytest
except ImportError:   # pragma: no cover
    pytest = None

if pytest is not None:
    @pytest.fixture
    def ai_connector_fake(monkeypatch):
        """Make the connection look ready and ``api.client()`` return a fake client: ``api.unavailable_reason()`` gives None,
        ``api.is_ready()`` True and ``api.describe()`` the fake model, without the plugin, a model or a key. Set the answers
        with ``ai_connector_fake.answers = [...]`` (or a callable) before the code under test calls; the calls are in
        ``ai_connector_fake.calls``, the clients made in ``ai_connector_fake.clients``. Set ``ai_connector_fake.model`` for another
        model (``"openai/..."`` has no ``page_read``), and ``ai_connector_fake.models = {"purpose": "service/model"}`` for a
        model per purpose (API 2); ``ai_connector_fake.purposes`` lists the purpose each client was made for."""
        from ckanext.ai_connector import api
        from ckanext.ai_connector.client import capabilities_of, service_of

        class Fake:
            answers: Any = ()
            model = "gemini/fake-model"
            models: dict = {}
            calls: list = []
            clients: list = []
            purposes: list = []

        state = Fake()
        state.models, state.calls, state.clients, state.purposes = {}, [], [], []

        def model_for(purpose):
            return state.models.get(purpose) or state.model

        def unavailable_reason(min_api=1, consumer=None, purpose=None):
            return api._too_old(min_api)

        def describe(consumer=None, purpose=None):
            model = model_for(purpose)
            return {"model": model, "service": service_of(model), "capabilities": sorted(capabilities_of(model)),
                    "timeout_seconds": 30.0, "page_read_timeout_seconds": 180.0,
                    "source": "purpose" if purpose in state.models else "main"}

        monkeypatch.setattr(api, "unavailable_reason", unavailable_reason)
        monkeypatch.setattr(api, "is_ready",
                            lambda min_api=1, consumer=None, purpose=None: unavailable_reason(min_api) is None)
        monkeypatch.setattr(api, "describe", describe)

        def client(consumer, *, purpose=None, timeout_seconds=None, max_attempts=None):
            made = fake_client(lambda call: _next(state, call), model=model_for(purpose), consumer=consumer,
                               max_attempts=max_attempts or 2, timeout_seconds=timeout_seconds or 30.0)
            state.clients.append(made)
            state.purposes.append(purpose)
            return made

        monkeypatch.setattr(api, "client", client)
        return state

    def _next(state, call):
        state.calls.append(call)
        if callable(state.answers):
            return state.answers(call)
        answers = list(state.answers)
        if not answers:
            raise AssertionError("the fake AI client got more calls than answers")
        answer, state.answers = answers[0], answers[1:]
        return answer
