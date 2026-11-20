"""The settings, the keys, the page, the actions and the command, in CKAN."""

import base64
import hashlib

import pytest

from ckan import model
from ckan.plugins import toolkit
from ckan.tests import factories, helpers

from ckanext.ai_connector import actions, api, client as client_module, keys, settings
from ckanext.ai_connector.connection import ConnectionSettings

P = settings.PREFIX
SECRET = "test-key-value-that-must-never-show"
PAGE_KEY = "AIzaSy-a-key-entered-on-the-page-0042"

pytestmark = [
    pytest.mark.ckan_config("ckan.plugins", "ai_connector"),
    pytest.mark.ckan_config("SECRET_KEY", "a-portal-secret-for-the-tests"),
    pytest.mark.usefixtures("with_plugins", "clean_db"),
]


@pytest.fixture
def people():
    return {"sysadmin": factories.Sysadmin(), "editor": factories.User()}


def call(name, user, **data):
    return helpers.call_action(name, context={"user": user["name"], "ignore_auth": False}, **data)


def _fresh_config_cache():
    """What CKAN does at startup: keep the configured values aside before any saved value is applied."""
    from ckan.lib import app_globals

    for name in settings.FIELDS:
        app_globals._CONFIG_CACHE.pop(P + name, None)
    app_globals.reset()


def _answering(text):
    """A transport for ConnectionSettings.client() that answers with ``text`` and records the calls."""
    seen = []

    def transport(call):
        seen.append(call)
        return client_module.Reply(text=text)

    return transport, seen


# --- the settings ------------------------------------------------------------

@pytest.mark.ckan_config(P + "model", "gemini/server-value")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_a_saved_model_applies_at_once_and_a_value_set_back_is_forgotten(people):
    _fresh_config_cache()
    context = {"user": people["sysadmin"]["name"]}
    settings.save(context, {"model": "anthropic/claude-haiku-4-5", "timeout_seconds": "30"})
    assert toolkit.config[P + "model"] == "anthropic/claude-haiku-4-5"
    assert settings.value("model") == "anthropic/claude-haiku-4-5"
    assert {row["name"] for row in settings.overview() if row["saved"]} == {"model"}
    settings.save(context, {"model": "gemini/server-value"})
    assert settings.stored("model") is None and settings.value("model") == "gemini/server-value"


@pytest.mark.ckan_config(P + "model", "gemini/server-value")
def test_a_saved_value_goes_before_the_server_value_and_reset_forgets_it(people):
    _fresh_config_cache()
    call("config_option_update", people["sysadmin"], **{P + "model": "anthropic/saved"})
    settings.forget_rows()
    assert settings.value("model") == "anthropic/saved" and settings.server_default("model") == "gemini/server-value"
    settings.reset(["model"])
    assert settings.value("model") == "gemini/server-value" and settings.stored("model") is None


@pytest.mark.parametrize("name, value", [
    ("api_key_env", "CKAN___BEAKER__SESSION__SECRET"),  # another secret on the server
    ("api_key_env", "POSTGRES_PASSWORD"),
    ("model", "two words"),
    ("timeout_seconds", "0"),
    ("page_read_timeout_seconds", "601"),
])
def test_values_outside_the_rules_are_refused(people, name, value):
    with pytest.raises(toolkit.ValidationError):
        call("config_option_update", people["sysadmin"], **{P + name: value})
    assert model.get_system_info(P + name) is None


@pytest.mark.ckan_config(P + "api_base", "http://ai.server.example/v1")
@pytest.mark.ckan_config(P + "api_key_envs", "GEMINI_API_KEY")
def test_the_address_and_the_allowlist_never_come_from_the_database():
    """A row in the database must not decide where the key is sent, or which variable may be read."""
    model.set_system_info(P + "api_base", "http://somewhere.else.example")
    model.set_system_info(P + "api_key_envs", "POSTGRES_PASSWORD")
    settings.forget_rows()
    assert ConnectionSettings.in_use().api_base == "http://ai.server.example/v1"
    assert settings.value("api_base") == "http://ai.server.example/v1"
    assert keys.allowed_key_envs() == ["GEMINI_API_KEY"]
    assert settings.stored("api_base") is None
    assert "api_base" not in api.describe() and "somewhere" not in str(api.describe())


@pytest.mark.ckan_config(P + "model", "gemini/server-value")
def test_the_rows_are_read_once_per_request_and_again_after_a_save(people, app):
    _fresh_config_cache()
    with app.flask_app.test_request_context():
        settings.forget_rows()
        assert settings.value("model") == "gemini/server-value"
        settings.save({"user": people["sysadmin"]["name"]}, {"model": "anthropic/saved-now"})
        assert settings.value("model") == "anthropic/saved-now"
        keys.store_key("GEMINI_API_KEY", PAGE_KEY)
        assert keys.stored_key("GEMINI_API_KEY") == PAGE_KEY
        settings.reset(["model"])
        assert settings.value("model") == "gemini/server-value"
        model.set_system_info(P + "model", "written-elsewhere/in-this-request")
        assert settings.value("model") == "gemini/server-value"   # kept: only this extension's writes clear the rows
        settings.forget_rows()
        assert settings.value("model") == "written-elsewhere/in-this-request"


@pytest.mark.ckan_config(P + "model", "gemini/server-value")
def test_outside_a_request_a_reading_block_asks_the_database_once(monkeypatch):
    queries = []
    original = settings._rows

    def counting():
        holder = settings._holder()
        if holder is None or getattr(holder, "_ai_connector_rows", None) is None:
            queries.append(1)
        return original()

    monkeypatch.setattr(settings, "_rows", counting)
    with settings.reading():
        for _ in range(5):
            settings.value("model")
        keys.key_status()
    assert len(queries) == 1


# --- the keys ----------------------------------------------------------------

def test_a_key_is_stored_encrypted_with_a_versioned_token_and_never_in_clear():
    keys.store_key("GEMINI_API_KEY", PAGE_KEY)
    token = model.get_system_info(keys.KEY_PREFIX + "GEMINI_API_KEY")
    assert token.startswith("v1:") and PAGE_KEY not in token
    assert keys.stored_key("GEMINI_API_KEY") == PAGE_KEY


def test_the_cipher_is_derived_with_hkdf_under_its_own_label():
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    keys.store_key("GEMINI_API_KEY", PAGE_KEY)
    body = model.get_system_info(keys.KEY_PREFIX + "GEMINI_API_KEY").split(":", 1)[1]
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                   info=b"ckanext-ai-config/api-key/v1").derive(b"a-portal-secret-for-the-tests")
    assert Fernet(base64.urlsafe_b64encode(derived)).decrypt(body.encode()).decode() == PAGE_KEY
    # Not the plain hash an earlier version of this code used.
    plain = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"a-portal-secret-for-the-tests").digest()))
    with pytest.raises(Exception):
        plain.decrypt(body.encode())


def test_a_token_that_cannot_be_read_says_so_and_is_not_used(monkeypatch):
    model.set_system_info(keys.KEY_PREFIX + "GEMINI_API_KEY", "v1:not-a-token")
    settings.forget_rows()
    with pytest.raises(keys.KeyUnreadable):
        keys.stored_key("GEMINI_API_KEY")
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    assert keys.effective_key("GEMINI_API_KEY") is None   # the server's key does not stand in for the chosen one
    assert {row["name"]: row for row in keys.key_status()}["GEMINI_API_KEY"]["unreadable"]


@pytest.mark.ckan_config(P + "model", "gemini/gemini-3.5-flash")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_an_unreadable_stored_key_turns_the_ai_off_also_for_the_client_when_the_server_has_the_variable(monkeypatch):
    """The sysadmin chose the page's key: the server's variable does not stand in for it. is_ready() and client() agree."""
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    _fresh_config_cache()
    assert api.is_ready()
    model.set_system_info(keys.KEY_PREFIX + "GEMINI_API_KEY", "v1:not-a-token")
    settings.forget_rows()
    assert not api.is_ready() and "cannot be read" in api.unavailable_reason()
    with pytest.raises(api.AIConfigurationError) as raised:
        api.client("my_extension")
    assert "cannot be read" in str(raised.value) and SECRET not in str(raised.value)


def test_a_key_for_another_service_is_never_read_from_the_environment(monkeypatch):
    keys.add_custom_key("acme_api_key", PAGE_KEY.replace("AIzaSy", "acme"), user="boss")
    monkeypatch.setenv("ACME_API_KEY", "from-the-environment-0000000000")
    assert keys.effective_key("ACME_API_KEY") == PAGE_KEY.replace("AIzaSy", "acme")
    status = {row["name"]: row for row in keys.key_status()}["ACME_API_KEY"]
    assert status["custom"] and status["source"] == "page" and not status["server_set"]
    keys.remove_key("ACME_API_KEY")
    settings.forget_rows()
    current = ConnectionSettings(model="acme/model", api_key_env="ACME_API_KEY").with_stored_key()
    with pytest.raises(api.AIConfigurationError):
        current.key()   # the environment's value is never used for an added key, also when the stored one is gone


def test_an_added_key_can_be_removed_with_its_name_but_not_while_chosen(people):
    keys.add_custom_key("ACME_API_KEY", "acme-a-key-for-another-service-01")
    call("config_option_update", people["sysadmin"], **{P + "api_key_env": "ACME_API_KEY"})
    settings.forget_rows()
    with pytest.raises(toolkit.ValidationError):
        keys.remove_custom_key("ACME_API_KEY")
    settings.reset(["api_key_env"])
    keys.remove_custom_key("ACME_API_KEY")
    assert keys.custom_key_names() == [] and model.get_system_info(keys.KEY_PREFIX + "ACME_API_KEY") is None


@pytest.mark.ckan_config(P + "model", "gemini/gemini-3.5-flash-lite")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_a_key_for_a_known_service_is_added_without_changing_the_model(monkeypatch):
    """Seen on the server 2026-11-19: adding ANTHROPIC_API_KEY here said it existed already, while the model was Gemini."""
    _fresh_config_cache()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    keys.add_custom_key("anthropic_api_key", "sk-ant-a-key-entered-on-the-page-0042", user="boss")
    settings.forget_rows()
    assert keys.stored_key("ANTHROPIC_API_KEY") == "sk-ant-a-key-entered-on-the-page-0042"
    assert keys.custom_key_names() == []   # a known variable, not a name of its own
    assert settings.value("model") == "gemini/gemini-3.5-flash-lite" and settings.value("api_key_env") == "GEMINI_API_KEY"
    with pytest.raises(toolkit.ValidationError) as caught:   # a Gemini key under Anthropic's name
        keys.add_custom_key("ANTHROPIC_API_KEY", PAGE_KEY)
    assert "custom_key" in caught.value.error_dict


@pytest.mark.ckan_config(P + "model", "gemini/gemini-3.5-flash-lite")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_the_page_adds_a_key_for_a_service_chosen_in_a_list_or_under_a_name_of_ones_own(people, app, monkeypatch):
    _fresh_config_cache()
    for env in keys.PROVIDER_KEYS.values():   # the environment the test assumes, whatever the machine has
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    headers = {"Authorization": factories.APIToken(user=people["sysadmin"]["name"])["token"]}
    page = app.get("/ckan-admin/ai", headers=headers).body
    assert '<option value="ANTHROPIC_API_KEY"' in page and "Anthropic (Claude)" in page and '<option value="other"' in page
    app.post("/ckan-admin/ai", headers=headers, data={
        "step": "add_custom_key", "custom_service": "ANTHROPIC_API_KEY", "custom_name": "",
        "custom_key": "sk-ant-a-key-entered-on-the-page-0042", "custom_confirm": "1"})
    settings.forget_rows()
    assert keys.stored_key("ANTHROPIC_API_KEY") == "sk-ant-a-key-entered-on-the-page-0042" and keys.custom_key_names() == []
    assert settings.value("model") == "gemini/gemini-3.5-flash-lite"
    app.post("/ckan-admin/ai", headers=headers, data={
        "step": "add_custom_key", "custom_service": "other", "custom_name": "ACME_API_KEY",
        "custom_key": "acme-a-key-for-another-service-01", "custom_confirm": "1"})
    settings.forget_rows()
    assert keys.custom_key_names() == ["ACME_API_KEY"]
    page = app.get("/ckan-admin/ai", headers=headers).body
    assert '<option value="OPENAI_API_KEY" selected>' in page   # the first service without a key
    # "Manage API keys" sits in the connection form between the key and the time limits; its fields belong to two forms
    # of their own (no nested forms), each with its CSRF token and its step.
    fold = page[page.index('id="add-key"'):page.index("</details>", page.index('id="add-key"'))]
    assert page.index('id="ai_api_key_env"') < page.index('id="add-key"') < page.index('id="timeout_seconds"')
    assert "<form" not in fold and fold.count('form="ai-key-add"') == 5 and 'form="ai-key-remove"' in fold
    for name, step in (("ai-key-add", "add_custom_key"), ("ai-key-remove", "remove_key")):
        own = page[page.index('<form id="%s"' % name):]
        own = own[:own.index("</form>")]
        assert 'name="_csrf_token"' in own and 'name="step" value="%s"' % step in own
    # The model list knows each group's service, the suggested model per service and the services' names (settings.js
    # shows only the chosen key's models). No "the server has no key of its own" beside the keys entered here.
    assert 'data-provider="anthropic"' in page and 'data-provider="gemini"' in page
    assert "data-recommended=" in page and "Anthropic (Claude)" in page
    assert "the server has no key of its own" not in page
    assert "Anthropic (Claude) – has a key: this one replaces it" in page

    # A name written beside a chosen service (both fields show without the script) is refused, not ignored.
    page = app.post("/ckan-admin/ai", headers=headers, data={
        "step": "add_custom_key", "custom_service": "OPENAI_API_KEY", "custom_name": "OTHER_API_KEY",
        "custom_key": "sk-a-key-for-openai-entered-0042", "custom_confirm": "1"}).body
    assert "only for “Other service…”" in page and "sk-a-key-for-openai-entered-0042" not in page
    settings.forget_rows()
    assert keys.stored_key("OPENAI_API_KEY") is None
    # A name of one's own added already is refused too.
    page = app.post("/ckan-admin/ai", headers=headers, data={
        "step": "add_custom_key", "custom_service": "other", "custom_name": "ACME_API_KEY",
        "custom_key": "acme-another-key-for-the-same-01", "custom_confirm": "1"}).body
    assert "was added already" in page and keys.custom_key_names() == ["ACME_API_KEY"]


def test_a_known_services_key_is_never_paired_with_another_services_model(people, app, monkeypatch):
    """Review 2026-11-19: a model written under "Other model…" for a service without a key kept the other service's
    key chosen, and that key would have been sent there. The connection refuses before anything is sent."""
    from ckanext.ai_connector.connection import pairing_error

    assert pairing_error("openai/gpt-x", "GEMINI_API_KEY")
    keys.add_custom_key("ACME_API_KEY", "acme-a-key-for-another-service-01")
    assert pairing_error("gemini/x", "ACME_API_KEY")          # a key of one's own never to a known service
    assert pairing_error("gpt-4o", "ACME_API_KEY")            # LiteLLM routes gpt-4o to OpenAI without a prefix
    assert pairing_error("gpt-4o", "GEMINI_API_KEY") and not pairing_error("gpt-4o", "OPENAI_API_KEY")
    assert not pairing_error("codestral/x", "MISTRAL_API_KEY")   # Mistral's, under another name in LiteLLM
    assert not pairing_error("openai/x", "PORTAL_OPENAI_KEY")    # a server variable under another name: the server's choice
    assert not pairing_error("gemini/x", "GEMINI_API_KEY") and not pairing_error("acme/x", "ACME_API_KEY")
    assert not pairing_error("openai/x", "GEMINI_API_KEY", api_base="https://ai.example.org")   # an own server
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    from ckanext.ai_connector.client import AIConfigurationError

    with pytest.raises(AIConfigurationError):
        ConnectionSettings.from_values({"model": "openai/gpt-x", "api_key_env": "GEMINI_API_KEY"}).key()
    sent = []
    monkeypatch.setattr(ConnectionSettings, "client", lambda self, consumer, **options: sent.append(self.key()))
    result = call("ai_connector_test", people["sysadmin"], model="openai/gpt-x", api_key_env="GEMINI_API_KEY")
    assert not result["success"] and "is for Google Gemini" in result["error"] and sent == []
    token = factories.APIToken(user=people["sysadmin"]["name"])["token"]
    page = app.post("/ckan-admin/ai", headers={"Authorization": token}, data={
        "model": "openai/gpt-x", "api_key_env": "GEMINI_API_KEY", "timeout_seconds": "30",
        "page_read_timeout_seconds": "180", "step": "save"}).body
    settings.forget_rows()
    assert "is for Google Gemini" in page and settings.stored("model") != "openai/gpt-x"
    # The setup check says it too, so is_ready() does not promise calls that would be refused.
    from ckanext.ai_connector import checks

    monkeypatch.setattr(settings, "value", lambda name: {"model": "openai/gpt-x", "api_key_env": "GEMINI_API_KEY"}.get(name, ""))
    assert any(c["key"] == "ai_key" and c["status"] == checks.ERROR and "is for Google Gemini" in c["detail"]
               for c in checks.run_checks())


def test_a_key_that_points_at_an_environment_variable_is_refused():
    """LiteLLM reads os.environ/NAME as that variable: a way round the allowlist."""
    for add in (lambda: keys.store_key("GEMINI_API_KEY", "os.environ/POSTGRES_PASSWORD"),
                lambda: keys.add_custom_key("ACME_API_KEY", "os.environ/POSTGRES_PASSWORD")):
        with pytest.raises(toolkit.ValidationError):
            add()


@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "SECRET_API_KEY", "CKAN_API_KEY", "lowercase", "MY_TOKEN_API_KEY"])
def test_a_name_that_could_point_at_something_else_is_refused(name):
    with pytest.raises(toolkit.ValidationError):
        keys.add_custom_key(name, "a-key-for-another-service-0001")
    assert keys.custom_key_names() == []


def test_only_the_last_four_characters_of_a_long_key_are_given(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", "short-key")
    status = {row["name"]: row for row in keys.key_status()}
    assert (status["GEMINI_API_KEY"]["set"], status["GEMINI_API_KEY"]["tail"]) == (True, "show")
    assert (status["OPENAI_API_KEY"]["set"], status["OPENAI_API_KEY"]["tail"]) == (True, "")


def test_a_key_pasted_for_the_wrong_service_is_refused():
    with pytest.raises(toolkit.ValidationError):
        keys.store_key("ANTHROPIC_API_KEY", PAGE_KEY)
    assert keys.stored_key("ANTHROPIC_API_KEY") is None


# --- the API -----------------------------------------------------------------

@pytest.mark.ckan_config(P + "model", "gemini/test")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_ready_needs_a_model_and_a_key_and_says_why_not(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert not api.is_ready() and "No API key" in api.unavailable_reason()
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    assert api.is_ready() and api.unavailable_reason() is None
    assert "version 99" in api.unavailable_reason(min_api=99)
    made = api.client("my_extension")
    assert (made.consumer, made.model, made.service) == ("my_extension", "gemini/test", "gemini")
    assert SECRET not in repr(made) and not any(SECRET in str(value) for value in vars(made).values())


@pytest.mark.ckan_config(P + "model", "gemini/test")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
@pytest.mark.ckan_config(P + "timeout_seconds", "20")
def test_describe_tells_what_the_connection_can_do_and_never_the_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    assert api.describe() == {"model": "gemini/test", "service": "gemini", "capabilities": ["page_read"],
                              "timeout_seconds": 20.0, "page_read_timeout_seconds": 180.0}


@pytest.mark.ckan_config(P + "model", "gemini/test")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_a_consumer_may_change_only_how_long_and_how_often(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    made = api.client("my_extension", timeout_seconds=5, max_attempts=1)
    assert (made.timeout_seconds, made.max_attempts) == (5, 1)
    assert api.client("my_extension", timeout_seconds=10000).timeout_seconds == 600
    for name, value in (("model", "openai/other"), ("api_key", "another"), ("api_base", "http://elsewhere")):
        with pytest.raises(TypeError):
            api.client("my_extension", **{name: value})
    with pytest.raises(TypeError):
        api.client()   # the consumer's name is required
    with pytest.raises(ValueError):
        api.client("Not a name!")


def test_without_the_plugin_nothing_is_ready_and_no_client_is_made(monkeypatch):
    monkeypatch.setattr("ckan.plugins.plugin_loaded", lambda name: name != "ai_connector")
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    assert not api.is_ready() and "ai_connector" in api.unavailable_reason()
    with pytest.raises(api.AIConfigurationError):
        api.client("my_extension")


# --- the page and the actions ------------------------------------------------

@pytest.mark.ckan_config(P + "model", "gemini/gemini-3.5-flash-lite")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_only_sysadmins_reach_the_page_and_the_actions(people, app):
    for action in ("ai_connector_test", "ai_connector_reset", "ai_connector_status"):
        with pytest.raises(toolkit.NotAuthorized):
            call(action, people["editor"])
    with pytest.raises(toolkit.NotAuthorized):
        call("config_option_update", people["editor"], **{P + "model": "x/y"})
    token = factories.APIToken(user=people["editor"]["name"])["token"]
    assert app.get("/ckan-admin/ai", headers={"Authorization": token}, status=403)
    assert app.get("/ckan-admin/ai", follow_redirects=False).status_code in (302, 403)


@pytest.mark.ckan_config(P + "model", "gemini/gemini-3.5-flash-lite")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_the_page_says_whether_a_key_is_set_but_never_shows_it(people, app, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    token = factories.APIToken(user=people["sysadmin"]["name"])["token"]
    page = " ".join(app.get("/ckan-admin/ai", headers={"Authorization": token}).body.split())
    assert "gemini/gemini-3.5-flash-lite" in page
    assert "GEMINI_API_KEY – set on the server (…show)" in page
    choice = page[page.index('id="ai_api_key_env"'):page.index("</select>", page.index('id="ai_api_key_env"'))]
    assert "OPENAI_API_KEY" not in choice   # only keys that are there; a missing one is added under "API keys"
    assert '<option value="OPENAI_API_KEY"' in page[page.index('id="add-key"'):]
    assert SECRET not in page and SECRET[:-4] not in page


@pytest.mark.ckan_config(P + "model", "gemini/gemini-3.5-flash-lite")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_the_page_saves_a_model_and_a_key_and_never_shows_the_key(people, app):
    _fresh_config_cache()
    token = factories.APIToken(user=people["sysadmin"]["name"])["token"]
    page = app.post("/ckan-admin/ai", data={"step": "add_custom_key", "custom_service": "GEMINI_API_KEY", "custom_name": "", "custom_key": PAGE_KEY,
                  "custom_confirm": "1"}, headers={"Authorization": token}, follow_redirects=True).body
    assert PAGE_KEY not in page
    form = {"model": "gemini/gemini-3.5-flash", "api_key_env": "GEMINI_API_KEY", "timeout_seconds": "30",
            "page_read_timeout_seconds": "180", "api_key": "ignored-the-field-is-gone-0042", "step": "save"}
    page = app.post("/ckan-admin/ai", data=form, headers={"Authorization": token}).body
    assert PAGE_KEY not in page and 'name="api_key"' not in page
    assert model.get_system_info(P + "model") == "gemini/gemini-3.5-flash"
    settings.forget_rows()
    assert keys.stored_key("GEMINI_API_KEY") == PAGE_KEY


@pytest.mark.ckan_config(P + "model", "gemini/server-value")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_the_page_offers_the_server_s_values_only_for_what_the_server_has_and_resets_only_that(people, app, monkeypatch):
    _fresh_config_cache()
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    token = factories.APIToken(user=people["sysadmin"]["name"])["token"]
    headers = {"Authorization": token}
    page = app.get("/ckan-admin/ai", headers=headers).body
    assert 'value="reset"' not in page and "instead of the server" not in page   # nothing saved: nothing to set back

    form = {"model": "gemini/page-value", "api_key_env": "GEMINI_API_KEY", "timeout_seconds": "45",
            "page_read_timeout_seconds": "180", "step": "save"}
    app.post("/ckan-admin/ai", data=form, headers=headers)
    settings.forget_rows()
    page = " ".join(app.get("/ckan-admin/ai", headers=headers).body.split())
    assert "Applies instead of the server's value: gemini/server-value" in page or \
        "Applies instead of the server&#39;s value: gemini/server-value" in page
    assert 'value="reset"' in page and "For: Model" in page and "For: Model," not in page   # the time limit has no server value

    page = " ".join(app.post("/ckan-admin/ai", data={"step": "reset"}, headers=headers).body.split())
    settings.forget_rows()
    assert settings.stored("model") == "gemini/page-value"   # the first click only asks
    assert 'value="reset_confirmed"' in page and "gemini/page-value" in page and "gemini/server-value" in page

    app.post("/ckan-admin/ai", data={"step": "reset_confirmed"}, headers=headers)
    settings.forget_rows()
    assert settings.stored("model") is None and settings.stored("timeout_seconds") == "45"


def test_with_a_key_but_no_variable_or_model_chosen_the_page_uses_the_key_and_suggests_its_model(people, app, monkeypatch):
    """After "Use the server's values" on a server without values: the key entered here stays, nothing else is chosen."""
    _fresh_config_cache()
    for env in keys.PROVIDER_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    keys.store_key("GEMINI_API_KEY", PAGE_KEY)
    token = factories.APIToken(user=people["sysadmin"]["name"])["token"]
    page = " ".join(app.get("/ckan-admin/ai", headers={"Authorization": token}).body.split())
    assert "Start with the API key" not in page and "A suggested model is chosen" in page
    assert '<option value="GEMINI_API_KEY" data-set="yes" selected>' in page
    field = page[page.index('id="ai_model"'):]
    assert 'value="gemini/' in field[:field.index(">")] and "placeholder" not in field[:field.index(">")]


@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_the_model_and_the_key_variable_are_only_set_back_together(people, app, monkeypatch):
    """The server has a key variable but no model: setting back the key variable alone would send the Gemini key to the
    model saved on the page (Anthropic). Neither is offered."""
    _fresh_config_cache()
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET + "-anthropic")
    token = factories.APIToken(user=people["sysadmin"]["name"])["token"]
    headers = {"Authorization": token}
    form = {"model": "anthropic/claude-haiku-4-5", "api_key_env": "ANTHROPIC_API_KEY", "timeout_seconds": "30",
            "page_read_timeout_seconds": "180", "step": "save"}
    app.post("/ckan-admin/ai", data=form, headers=headers)
    settings.forget_rows()
    assert settings.stored("api_key_env") == "ANTHROPIC_API_KEY" and settings.resettable() == []
    assert 'value="reset"' not in app.get("/ckan-admin/ai", headers=headers).body
    app.post("/ckan-admin/ai", data={"step": "reset_confirmed"}, headers=headers)
    settings.forget_rows()
    assert settings.stored("model") == "anthropic/claude-haiku-4-5" and settings.stored("api_key_env") == "ANTHROPIC_API_KEY"


@pytest.mark.ckan_config(P + "model", "openai/gpt-5-mini")
def test_with_a_model_but_no_key_variable_only_the_key_of_the_model_s_service_is_chosen(people, app, monkeypatch):
    _fresh_config_cache()
    for env in keys.PROVIDER_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    token = factories.APIToken(user=people["sysadmin"]["name"])["token"]
    page = app.get("/ckan-admin/ai", headers={"Authorization": token}).body
    assert 'value="ANTHROPIC_API_KEY" data-set="yes" selected' not in page   # never another service's key
    assert '<option value="" data-set="no" selected>' in page   # and the browser does not fall back on the first one
    monkeypatch.setenv("OPENAI_API_KEY", SECRET + "-openai")
    page = app.get("/ckan-admin/ai", headers={"Authorization": token}).body
    assert 'value="OPENAI_API_KEY" data-set="yes" selected' in page


def test_the_recommended_model_is_only_ever_one_of_the_chosen_key_s_service():
    rows = [{"model": "anthropic/claude-x", "provider": "anthropic"}, {"model": "gemini/gemini-0-pro", "provider": "gemini"},
            {"model": "gemini/gemini-2.5-flash-lite", "provider": "gemini"}]
    assert keys.recommended_model(rows, "GEMINI_API_KEY") == "gemini/gemini-2.5-flash-lite"   # the first of the list that is there
    assert keys.recommended_model(rows, "ANTHROPIC_API_KEY") == ""   # none of its preferences: no "first model" either
    assert keys.recommended_model(rows, "OPENAI_API_KEY") == ""      # never a model of another service: the key would go there
    assert keys.recommended_model(rows, "") == "" and keys.recommended_model([], "GEMINI_API_KEY") == ""


def test_every_service_with_preferences_has_one_in_the_installed_catalogue():
    """The names in RECOMMENDED_MODELS age with LiteLLM's catalogue: check them when LiteLLM is raised."""
    import litellm

    for provider, names in keys.RECOMMENDED_MODELS.items():
        known = {name if name.startswith(provider + "/") else "%s/%s" % (provider, name)
                 for name, info in litellm.model_cost.items() if info.get("litellm_provider") == provider}
        assert known & set(names), provider


@pytest.mark.ckan_config(P + "model", "gemini/gemini-3.5-flash-lite")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_an_empty_model_field_sets_nothing_so_a_key_is_saved_while_the_server_has_a_model(people, app, monkeypatch):
    _fresh_config_cache()
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    token = factories.APIToken(user=people["sysadmin"]["name"])["token"]
    app.post("/ckan-admin/ai", data={"step": "add_custom_key", "custom_service": "GEMINI_API_KEY", "custom_name": "", "custom_key": PAGE_KEY,
                  "custom_confirm": "1"}, headers={"Authorization": token})
    form = {"model": "", "api_key_env": "GEMINI_API_KEY", "timeout_seconds": "45", "page_read_timeout_seconds": "180",
            "step": "save"}
    app.post("/ckan-admin/ai", data=form, headers={"Authorization": token})
    settings.forget_rows()
    assert keys.stored_key("GEMINI_API_KEY") == PAGE_KEY and not model.get_system_info(P + "model")
    assert settings.stored("timeout_seconds") == "45"   # the save went through with an empty model field
    assert settings.value("model") == "gemini/gemini-3.5-flash-lite"


def test_with_a_key_of_its_own_the_page_does_not_ask_to_start_with_the_key(people, app, monkeypatch):
    _fresh_config_cache()
    for env in keys.PROVIDER_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    keys.add_custom_key("ACME_API_KEY", "acme-key-entered-on-the-page-0042", user=people["sysadmin"]["name"])
    token = factories.APIToken(user=people["sysadmin"]["name"])["token"]
    page = app.get("/ckan-admin/ai", headers={"Authorization": token}).body
    assert "Start with the API key" not in page and "A suggested model is chosen" not in page


@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_the_first_time_the_key_comes_first_can_be_saved_alone_and_then_a_model_is_suggested(people, app, monkeypatch):
    """Without any key there is no list of models: the page asks for the key first, saves it without a model, and then
    suggests a model of that service (in the field, not saved)."""
    _fresh_config_cache()
    for env in keys.PROVIDER_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    token = factories.APIToken(user=people["sysadmin"]["name"])["token"]
    page = app.get("/ckan-admin/ai", headers={"Authorization": token}).body
    assert "Start with the API key" in page
    assert 'id="ai_model"' not in page and 'value="test"' not in page   # no model field and nothing to test yet
    assert page.index('id="add-key"') < page.index("Start with the API key")   # the keys section comes first

    app.post("/ckan-admin/ai", data={"step": "add_custom_key", "custom_service": "GEMINI_API_KEY", "custom_name": "", "custom_key": PAGE_KEY,
                  "custom_confirm": "1"}, headers={"Authorization": token})
    settings.forget_rows()
    assert keys.stored_key("GEMINI_API_KEY") == PAGE_KEY and not model.get_system_info(P + "model")

    page = app.get("/ckan-admin/ai", headers={"Authorization": token}).body
    assert "Start with the API key" not in page and "A suggested model is chosen" in page
    assert page.index('id="ai_model"') < page.index('id="ai_api_key_env"') < page.index('id="add-key"')
    assert 'name="api_key"' not in page   # keys are entered under "API keys" only
    assert '<option value="OPENAI_API_KEY"' not in page[page.index('id="ai_api_key_env"'):page.index('id="add-key"')]
    field = page[page.index('id="ai_model"'):]
    assert 'value="gemini/' in field[:field.index(">")]
    assert not model.get_system_info(P + "model")   # suggested, not saved


@pytest.mark.ckan_config(P + "model", "gemini/gemini-3.5-flash-lite")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_a_test_uses_the_form_values_without_saving_them_and_says_why_it_failed(people, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    transport, seen = _answering('{"answer": "Paris"}')
    used_keys = []
    original = ConnectionSettings.client

    def client(self, consumer, **options):
        used_keys.append(self.key())
        return original(self, consumer, transport=transport, **options)

    monkeypatch.setattr(ConnectionSettings, "client", client)
    result = call("ai_connector_test", people["sysadmin"], model="anthropic/claude-haiku-4-5",
                  api_key_env="ANTHROPIC_API_KEY", api_key="sk-ant-a-key-typed-but-not-saved")
    assert result == {"success": True, "model": "anthropic/claude-haiku-4-5", "answer": "Paris", "seconds": result["seconds"]}
    assert seen[0].model == "anthropic/claude-haiku-4-5" and used_keys == ["sk-ant-a-key-typed-but-not-saved"]
    assert model.get_system_info(P + "model") is None and keys.stored_key("ANTHROPIC_API_KEY") is None

    def failing(call_):
        error = Exception("API key %s not valid" % SECRET)
        error.status_code = 401
        raise error

    monkeypatch.setattr(ConnectionSettings, "client",
                        lambda self, consumer, **options: original(self, consumer, transport=failing, **options))
    result = call("ai_connector_test", people["sysadmin"])
    assert not result["success"] and result["detail"] == "Exception (401)" and SECRET not in str(result)
    assert "did not accept the key" in result["hint"]
    assert "faster model" in actions._status_hint(408) and "Try again in a while" in actions._status_hint(503)
    assert actions._status_hint(418) == ""   # a wrong key does not read as a service that cannot be reached

    with pytest.raises(toolkit.ValidationError):
        call("ai_connector_test", people["sysadmin"], api_key_env="POSTGRES_PASSWORD")


@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_a_test_uses_a_saved_value(people, monkeypatch):
    _fresh_config_cache()
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    call("config_option_update", people["sysadmin"], **{P + "model": "gemini/saved-on-the-page"})
    settings.forget_rows()
    transport, seen = _answering('{"answer": "x"}')
    original = ConnectionSettings.client
    monkeypatch.setattr(ConnectionSettings, "client",
                        lambda self, consumer, **options: original(self, consumer, transport=transport, **options))
    assert call("ai_connector_test", people["sysadmin"])["success"]
    assert [c.model for c in seen] == ["gemini/saved-on-the-page"]


# --- the command -------------------------------------------------------------

@pytest.mark.ckan_config(P + "model", "gemini/test")
@pytest.mark.ckan_config(P + "api_key_env", "GEMINI_API_KEY")
def test_doctor_says_what_is_missing_and_never_prints_a_key(monkeypatch):
    from click.testing import CliRunner

    from ckanext.ai_connector.cli import ai_connector

    def doctor():   # in this test's configuration (the ckan command would read the ini file again)
        return CliRunner().invoke(ai_connector, ["doctor"])

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = doctor()
    assert result.exit_code == 1 and "No API key is found" in result.output and "gemini/test" in result.output

    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    result = doctor()
    assert result.exit_code == 0, result.output
    assert "GEMINI_API_KEY" in result.output and SECRET not in result.output and SECRET[-4:] not in result.output


def test_nothing_here_names_another_extension():
    """A standalone extension: no option, row or text of another extension (the shim of a migration belongs to the
    extension whose data it is)."""
    import pathlib

    root = pathlib.Path(actions.__file__).parent
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in (".py", ".html", ".js", ".yaml", ".po") and "tests" not in path.parts:
            assert "metadata_assistant" not in path.read_text(encoding="utf-8"), path
            assert "metadata-assistant" not in path.read_text(encoding="utf-8"), path
