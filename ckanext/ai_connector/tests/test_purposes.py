"""Models for different purposes (API 2): declared by a consumer, chosen on the settings page, resolved for each client."""

import pytest

from ckan import model
from ckan.plugins import toolkit

from ckanext.ai_connector import api, checks, client as client_module, keys, purposes, settings
from ckanext.ai_connector.connection import ConnectionSettings
from ckanext.ai_connector.testing import ai_connector_fake  # noqa: F401

P = settings.PREFIX

pytestmark = [
    pytest.mark.ckan_config("ckan.plugins", "ai_connector"),
    pytest.mark.ckan_config("SECRET_KEY", "a-portal-secret-for-the-tests"),
    pytest.mark.ckan_config(P + "model", "gemini/main-model"),
    pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY"),
    pytest.mark.usefixtures("with_plugins", "clean_db"),
]


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "a-gemini-key-from-the-server")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-anthropic-key-from-the-server")
    purposes._declared.clear()
    api.declare_purposes("my_extension", {"translate": "Translation", "review": lambda: "Review"})
    yield
    purposes._declared.clear()


def _seen_key(current):
    """The key the transport would be built with (never sent anywhere)."""
    return current.key()


def test_the_api_version_grows_and_old_consumers_are_unchanged():
    assert api.API_VERSION == 2 and api.unavailable_reason(min_api=2) is None
    assert api.client("my_extension").model == "gemini/main-model"
    assert api.client("my_extension", purpose="translate").model == "gemini/main-model"   # nothing chosen: the main one


def test_a_model_chosen_for_a_purpose_comes_with_its_own_services_key():
    purposes.save("my_extension", "translate", "anthropic/claude-haiku-4-5", "ANTHROPIC_API_KEY")
    chosen = ConnectionSettings.for_purpose("my_extension", "translate")
    assert chosen.model == "anthropic/claude-haiku-4-5" and _seen_key(chosen) == "an-anthropic-key-from-the-server"
    assert chosen.timeout_seconds == ConnectionSettings.in_use().timeout_seconds   # the connection's time limits
    assert api.describe("my_extension", "translate")["service"] == "anthropic"
    assert api.describe("my_extension", "review")["model"] == "gemini/main-model"
    assert api.client("my_extension", purpose="translate").model == "anthropic/claude-haiku-4-5"


def test_a_key_of_another_service_is_refused_when_saved_and_before_a_call():
    with pytest.raises(toolkit.Invalid):
        purposes.save("my_extension", "translate", "anthropic/claude-haiku-4-5", "GEMINI_API_KEY")
    assert purposes.choice("my_extension", "translate") is None
    # A row written some other way is still refused before anything is sent.
    settings.write_row(purposes.row_key("my_extension", "translate"),
                       '{"model": "anthropic/claude-haiku-4-5", "api_key_env": "GEMINI_API_KEY"}')
    with pytest.raises(api.AIConfigurationError):
        api.client("my_extension", purpose="translate")
    assert "GEMINI_API_KEY" in api.unavailable_reason(consumer="my_extension", purpose="translate")
    assert api.unavailable_reason(consumer="my_extension", purpose="review") is None   # the other purpose is fine
    assert any(c["key"] == "ai_purpose" and c["status"] == checks.ERROR for c in checks.run_checks())


def test_a_choice_that_cannot_be_read_is_refused_not_replaced_by_the_main_connection():
    settings.write_row(purposes.row_key("my_extension", "review"), "not json")
    with pytest.raises(api.AIConfigurationError) as caught:
        api.client("my_extension", purpose="review")
    assert "Review" in str(caught.value)
    assert not api.is_ready(consumer="my_extension", purpose="review")


def test_a_removed_key_makes_the_purpose_refuse(monkeypatch):
    purposes.save("my_extension", "translate", "anthropic/claude-haiku-4-5", "ANTHROPIC_API_KEY")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(api.AIConfigurationError):
        api.client("my_extension", purpose="translate")
    assert api.client("my_extension", purpose="review").model == "gemini/main-model"


def test_a_key_entered_on_the_page_is_used_for_the_purpose(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    keys.store_key("ANTHROPIC_API_KEY", "sk-ant-a-key-entered-on-the-page-0042")
    purposes.save("my_extension", "translate", "anthropic/claude-haiku-4-5", "ANTHROPIC_API_KEY")
    assert _seen_key(ConnectionSettings.for_purpose("my_extension", "translate")) == "sk-ant-a-key-entered-on-the-page-0042"
    assert _seen_key(ConnectionSettings.in_use()) == "a-gemini-key-from-the-server"   # the main key is untouched


def test_an_empty_model_forgets_the_choice_and_names_are_checked():
    purposes.save("my_extension", "translate", "anthropic/claude-haiku-4-5", "ANTHROPIC_API_KEY")
    purposes.save("my_extension", "translate", "", "")
    assert purposes.choice("my_extension", "translate") is None
    for consumer, purpose in (("My-Extension", "translate"), ("my_extension", "Translate"), ("my_extension", "a.b")):
        with pytest.raises(purposes.PurposeError):
            purposes.row_key(consumer, purpose)
    with pytest.raises(api.AIConfigurationError):
        api.client("my_extension", purpose="Not.A.Name")


def test_a_choice_for_a_purpose_nobody_declares_is_a_warning():
    purposes.save("other_extension", "summary", "anthropic/claude-haiku-4-5", "ANTHROPIC_API_KEY")
    found = [c for c in checks.run_checks() if c["key"] == "ai_purpose"]
    assert [c["status"] for c in found] == [checks.WARN] and "other_extension" in found[0]["detail"]
    assert {row["purpose"]: row["declared"] for row in purposes.saved()} == {"summary": False}


def test_the_fake_fixture_takes_a_model_per_purpose(ai_connector_fake):
    ai_connector_fake.models = {"translate": "anthropic/fake"}
    ai_connector_fake.answers = ["one", "two"]
    assert api.client("my_extension", purpose="translate").model == "anthropic/fake"
    assert api.client("my_extension").model == "gemini/fake-model"
    assert ai_connector_fake.purposes == ["translate", None]
    assert api.describe("my_extension", "translate")["service"] == "anthropic"


# --- after ckan-reviewer 2026-11-20 -------------------------------------------------------------------------------------

def test_a_key_name_outside_the_allowlist_is_never_read_for_a_purpose(monkeypatch):
    monkeypatch.setenv("SOME_OTHER_SECRET", "must-never-leave")
    settings.write_row(purposes.row_key("my_extension", "translate"),
                       '{"model": "openai/gpt-4o-mini", "api_key_env": "SOME_OTHER_SECRET"}')
    with pytest.raises(api.AIConfigurationError):
        api.client("my_extension", purpose="translate")
    assert "SOME_OTHER_SECRET" in api.unavailable_reason(consumer="my_extension", purpose="translate")
    assert any(c["key"] == "ai_purpose" and c["status"] == checks.ERROR for c in checks.run_checks())


def test_a_key_added_on_the_page_cannot_be_removed_while_a_purpose_uses_it():
    keys.add_custom_key("ACME_API_KEY", "a-key-for-acme-that-is-long-enough-0042", user="boss")
    purposes.save("my_extension", "review", "ollama/acme-model", "ACME_API_KEY")
    with pytest.raises(toolkit.ValidationError) as caught:
        keys.remove_custom_key("ACME_API_KEY")
    assert "Review" in str(caught.value.error_dict)
    purposes.forget("my_extension", "review")
    keys.remove_custom_key("ACME_API_KEY")
    assert not keys.is_custom_key("ACME_API_KEY")


def test_names_that_would_not_fit_in_a_row_are_refused():
    with pytest.raises(purposes.PurposeError):
        api.declare_purposes("c" * 64, {"p" * 40: "Too long"})
    assert "at most" in api.unavailable_reason(consumer="c" * 64, purpose="p" * 40)
    assert "must" not in (api.unavailable_reason(consumer="my_extension", purpose="Not.A.Name") or "must")


def test_a_purpose_with_its_own_model_works_while_the_main_connection_has_none(ckan_config, monkeypatch):
    monkeypatch.setitem(ckan_config, P + "model", "")
    settings.write_row(purposes.row_key("my_extension", "review"), "not json")
    found = [c["key"] for c in checks.run_checks()]
    assert "ai_model" in found and "ai_purpose" in found   # the broken choice is reported also without a main model
    purposes.save("my_extension", "translate", "anthropic/claude-haiku-4-5", "ANTHROPIC_API_KEY")
    assert api.is_ready(consumer="my_extension", purpose="translate")
    assert not api.is_ready() and not api.is_ready(consumer="my_extension", purpose="other")


# --- the settings page (stage 2) ----------------------------------------------------------------------------------------

def _headers():
    from ckan.tests import factories

    return {"Authorization": factories.APIToken(user=factories.Sysadmin()["name"])["token"]}


def test_the_page_lists_the_declared_purposes_and_saves_a_model_with_its_services_key(app):
    headers = _headers()
    page = app.get("/ckan-admin/ai", headers=headers).body
    assert 'id="purposes"' in page and 'name="purpose_model.my_extension.translate"' in page and "Review" in page
    app.post("/ckan-admin/ai", headers=headers, data={
        "step": "save_purposes", "purpose_model.my_extension.translate": "anthropic/claude-haiku-4-5",
        "purpose_key.my_extension.translate": "", "purpose_model.my_extension.review": "",
        "purpose_key.my_extension.review": ""})
    settings.forget_rows()
    assert purposes.choice("my_extension", "translate") == {"model": "anthropic/claude-haiku-4-5",
                                                            "api_key_env": "ANTHROPIC_API_KEY"}
    assert purposes.choice("my_extension", "review") is None
    # Emptied again: the main model applies.
    app.post("/ckan-admin/ai", headers=headers, data={
        "step": "save_purposes", "purpose_model.my_extension.translate": "", "purpose_key.my_extension.translate": ""})
    settings.forget_rows()
    assert purposes.choice("my_extension", "translate") is None


def test_the_page_refuses_another_services_key_and_saves_nothing_for_that_row(app):
    page = app.post("/ckan-admin/ai", headers=_headers(), data={
        "step": "save_purposes", "purpose_model.my_extension.translate": "anthropic/claude-haiku-4-5",
        "purpose_key.my_extension.translate": "GEMINI_API_KEY"}).body
    settings.forget_rows()
    assert "is for Google Gemini" in page
    assert purposes.choice("my_extension", "translate") is None


def test_the_test_button_of_a_row_tests_that_rows_model_without_saving(app, monkeypatch):
    seen = []

    def client(self, consumer, **options):
        seen.append((self.model, self.key()))
        raise api.AIError("stop here")

    monkeypatch.setattr(ConnectionSettings, "client", client)
    page = app.post("/ckan-admin/ai", headers=_headers(), data={
        "step": "save_purposes", "purpose_test": "my_extension.translate",
        "purpose_model.my_extension.translate": "anthropic/claude-haiku-4-5",
        "purpose_key.my_extension.translate": ""}).body
    settings.forget_rows()
    assert seen == [("anthropic/claude-haiku-4-5", "an-anthropic-key-from-the-server")]
    assert "Translation:" in page and purposes.choice("my_extension", "translate") is None


def _post(app, headers, **rows):
    data = {"step": "save_purposes"}
    for name, value in rows.items():
        data[name.replace("__", ".")] = value
    return app.post("/ckan-admin/ai", headers=headers, data=data).body


def test_a_rows_test_never_falls_back_on_the_main_key(app, monkeypatch):
    """ckan-reviewer 2026-11-20 (BLOCKER): a model whose service has no known key, the key left on "the model's service":
    the test must say so and send nothing, not use the main connection's key."""
    keys.add_custom_key("ACME_API_KEY", "a-key-for-acme-that-is-long-enough-0042", user="boss")
    settings.write_row(P + "api_key_env", "ACME_API_KEY")
    settings.write_row(P + "model", "ollama/acme-main")
    seen = []
    monkeypatch.setattr(ConnectionSettings, "client", lambda self, consumer, **o: seen.append(self.key()))
    page = _post(app, _headers(), purpose_test="my_extension.translate",
                 purpose_model__my_extension__translate="groq/llama-x", purpose_key__my_extension__translate="")
    assert seen == [] and "Choose the API key for this model." in page


def test_the_key_follows_the_model_until_another_is_chosen_and_back(app):
    headers = _headers()
    _post(app, headers, purpose_model__my_extension__translate="anthropic/claude-haiku-4-5",
          purpose_key__my_extension__translate="")
    settings.forget_rows()
    page = app.get("/ckan-admin/ai", headers=headers).body
    row = page[page.index('name="purpose_key.my_extension.translate"'):]
    row = row[:row.index("</select>")]
    assert '<option value="" selected>' in row          # shown as "the model's service's key", not as a choice
    # So a model of another service takes that service's key with it.
    _post(app, headers, purpose_model__my_extension__translate="gemini/gemini-3.5-flash-lite",
          purpose_key__my_extension__translate="")
    settings.forget_rows()
    assert purposes.choice("my_extension", "translate")["api_key_env"] == "GEMINI_API_KEY"
    # A key chosen explicitly, and back to the service's own: saved, not skipped as unchanged.
    keys.add_custom_key("ACME_API_KEY", "a-key-for-acme-that-is-long-enough-0042", user="boss")
    _post(app, headers, purpose_model__my_extension__translate="ollama/acme", purpose_key__my_extension__translate="ACME_API_KEY")
    settings.forget_rows()
    assert purposes.choice("my_extension", "translate") == {"model": "ollama/acme", "api_key_env": "ACME_API_KEY"}
    _post(app, headers, purpose_model__my_extension__translate="anthropic/claude-haiku-4-5",
          purpose_key__my_extension__translate="")
    settings.forget_rows()
    assert purposes.choice("my_extension", "translate")["api_key_env"] == "ANTHROPIC_API_KEY"


def test_either_all_rows_are_saved_or_none(app):
    page = _post(app, _headers(), purpose_model__my_extension__translate="anthropic/claude-haiku-4-5",
                 purpose_key__my_extension__translate="",
                 purpose_model__my_extension__review="anthropic/claude-haiku-4-5",
                 purpose_key__my_extension__review="GEMINI_API_KEY")
    settings.forget_rows()
    assert "is for Google Gemini" in page
    assert purposes.choice("my_extension", "translate") is None and purposes.choice("my_extension", "review") is None


@pytest.mark.ckan_config(P + "api_base", "https://ai.example.org/v1")
def test_with_an_own_ai_server_a_model_of_an_unknown_service_takes_the_main_key(app):
    _post(app, _headers(), purpose_model__my_extension__translate="ollama/mistral",
          purpose_key__my_extension__translate="")
    settings.forget_rows()
    assert purposes.choice("my_extension", "translate") == {"model": "ollama/mistral", "api_key_env": "GEMINI_API_KEY"}


def test_enter_saves_and_a_model_without_a_key_is_not_saved(app):
    page = app.get("/ckan-admin/ai", headers=_headers()).body
    form = page[page.index('id="purposes"'):]
    first = form[form.index("<button"):form.index("</button>")]
    assert 'type="submit"' in first and "name=" not in first          # the form's default button: Save, not a row's Test
    page = _post(app, _headers(), purpose_model__my_extension__translate="groq/llama-x",
                 purpose_key__my_extension__translate="")
    settings.forget_rows()
    assert "Choose the API key for this model." in page and purposes.choice("my_extension", "translate") is None


@pytest.mark.ckan_config(P + "api_base", "https://ai.example.org/v1")
def test_with_an_own_ai_server_the_main_key_is_taken_also_for_a_known_services_model(app):
    _post(app, _headers(), purpose_model__my_extension__translate="anthropic/claude-haiku-4-5",
          purpose_key__my_extension__translate="")
    settings.forget_rows()
    assert purposes.choice("my_extension", "translate")["api_key_env"] == "GEMINI_API_KEY"   # the server's, not Anthropic's


def test_a_rows_key_options_say_which_service_they_belong_to(app):
    """The page's script lets a row's key follow its model: it needs to know each key's service, and the custom keys."""
    keys.add_custom_key("ACME_API_KEY", "a-key-for-acme-that-is-long-enough-0042", user="boss")
    page = app.get("/ckan-admin/ai", headers=_headers()).body
    row = page[page.index('name="purpose_key.my_extension.translate"'):]
    row = row[:row.index("</select>")]
    assert 'value="ANTHROPIC_API_KEY" data-service="anthropic"' in row
    assert 'value="ACME_API_KEY" data-service="" data-custom="yes"' in row
    assert 'id="purposes-form"' in page and 'data-purpose-key="purpose_key.my_extension.translate"' in page


@pytest.mark.ckan_config(P + "api_base", "https://ai.example.org/v1")
def test_with_an_own_ai_server_the_page_tells_the_script_to_leave_the_keys_alone(app):
    """Every key goes to the AI server of one's own, so the script must not pick a known service's key for a row."""
    page = app.get("/ckan-admin/ai", headers=_headers()).body
    form = page[page.index('id="purposes-form"'):]
    assert 'data-api-base="yes"' in form[:form.index(">")]
