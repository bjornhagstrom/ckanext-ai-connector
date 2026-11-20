"""The API keys: which may be used, keys entered on the settings page, and what the page may show of them.

The interface chooses *which* key is used, by the name of its environment variable, from an allowlist. The allowlist stops
anyone from pointing "the key" at another secret on the server and sending it to an AI service. A sysadmin may also enter a
key on the page: it is stored encrypted in the database, with a key derived from the portal's own SECRET_KEY, so a database
dump alone does not reveal it. It can be replaced or removed, never read back; the page shows its last four characters, and
none for a short key. A key entered on the page goes before the server's.

A key for another service can be added on the page under a name of its own. The name is only a label for a key stored here: it
is never looked up in the server's environment, so it cannot be used to read out some other variable. Names that look like
something else are refused.

No function here logs or returns a key, only whether one is set and, for a long one, its last four characters.

A stored key is a Fernet token, prefixed with the version of the derivation (``v1:``): the cipher's key is derived with
HKDF-SHA256 from the portal's SECRET_KEY and the label ``ckanext-ai-config/api-key/v1``. Another version can be added beside it
without making stored keys unreadable.
"""

import base64
import json
import logging
import os
import re

from ckan.plugins import toolkit

from ckanext.ai_connector import settings
from ckanext.ai_connector.localization import _

log = logging.getLogger(__name__)

DEFAULT_KEY_ENVS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_API_KEY", "MISTRAL_API_KEY")
# Which variable holds the key for a LiteLLM provider (the model's prefix).
# The services by name, for the list of services on the settings page.
SERVICE_LABELS = {"gemini": "Google Gemini", "anthropic": "Anthropic (Claude)", "openai": "OpenAI", "azure": "Azure OpenAI",
                  "mistral": "Mistral"}
PROVIDER_KEYS = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                 "mistral": "MISTRAL_API_KEY", "azure": "AZURE_API_KEY"}
# Models in LiteLLM's catalogue that answer in text but are made for something else (speech, robots, agents, music); not
# suggested.
_NOT_FOR_TEXT = ("tts", "robotics", "computer-use", "deep-research", "lyria", "learnlm", "-exp", "gemma-2", "customtools",
                 "image", "audio", "realtime", "search", "omni")
# Where a key entered on the page is stored, per variable name. Not in update_config_schema, so config_option_show and the
# config never hold it.
KEY_PREFIX = settings.PREFIX + "secret."
KEY_MIN_LENGTH, KEY_MAX_LENGTH = 8, 1024
# A key shorter than this shows no characters at all.
_TAIL_MIN_LENGTH = 20

CUSTOM_KEYS_OPTION = settings.PREFIX + "custom_key_names"
# The derivation of the cipher's key, and the prefix of every token stored with it.
TOKEN_VERSION = "v1"
_HKDF_INFO = b"ckanext-ai-config/api-key/v1"
_CUSTOM_KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}_API_KEY$")
_RESERVED_WORDS = ("SECRET", "PASSWORD", "PASSWD", "TOKEN", "DATABASE", "POSTGRES", "CKAN", "BEAKER", "SQLALCHEMY", "SOLR",
                   "REDIS", "SMTP", "UWSGI", "DATASTORE", "SESSION", "PRIVATE", "CREDENTIAL")


class KeyUnreadable(Exception):
    """A stored key that cannot be decrypted (the portal's secret changed)."""


def allowed_key_envs():
    """The environment variables the key may be read from, and the names of the keys added on the page."""
    configured = settings.value("api_key_envs")
    names = configured.split() if configured else list(DEFAULT_KEY_ENVS)
    default = settings.server_default("api_key_env")
    if default and default not in names:
        names.append(default)
    return names + [name for name in custom_key_names() if name not in names]


def custom_key_names():
    """The names of the keys added on the page, in the order they were added."""
    raw = settings.saved_row(CUSTOM_KEYS_OPTION)
    try:
        names = json.loads(raw or "[]")
    except ValueError:
        return []
    return [name for name in names if isinstance(name, str) and _CUSTOM_KEY_NAME.match(name)]


def is_custom_key(name):
    return name in custom_key_names()


def _save_custom_key_names(names):
    if names:
        settings.write_row(CUSTOM_KEYS_OPTION, json.dumps(names))
    else:
        settings.delete_row(CUSTOM_KEYS_OPTION)


def add_custom_key(name, value, user=""):
    """Add a key for another provider: a name ending in _API_KEY and the key, stored encrypted like the others.

    Raises ValidationError keyed ``custom_name`` or ``custom_key``. Only the name is logged, never the value.
    """
    name = (name or "").strip().upper()
    if not _CUSTOM_KEY_NAME.match(name):
        raise toolkit.ValidationError({"custom_name": [_("Use capital letters, digits and underscores, ending in _API_KEY, for example ACME_API_KEY.")]})
    if any(word in name for word in _RESERVED_WORDS):
        raise toolkit.ValidationError({"custom_name": [_("That name is reserved. Choose a name for the service, such as ACME_API_KEY.")]})
    if name in allowed_key_envs() and not is_custom_key(name):
        # A known service's variable (ANTHROPIC_API_KEY): store the key for it, without changing the model or the chosen
        # key, so a key can be added before it is used. The same checks as a key entered with the model.
        try:
            store_key(name, value)
        except toolkit.ValidationError as error:
            raise toolkit.ValidationError({"custom_key": [message for messages in error.error_dict.values()
                                                          for message in messages]})
        log.info("AI key entered on the admin page: %s (by %s)", name, user or "?")
        return
    if name in allowed_key_envs():
        raise toolkit.ValidationError({"custom_name": [_("A key with that name was added already. Remove it under “API keys” "
                                                         "first to enter it again.")]})
    if os.environ.get(name):
        raise toolkit.ValidationError({"custom_name": [_("A variable with that name exists on the server. Ask whoever runs the server to add it to the allowed keys.")]})
    value = (value or "").strip()
    if not KEY_MIN_LENGTH <= len(value) <= KEY_MAX_LENGTH or any(c.isspace() for c in value) or _points_elsewhere(value):
        raise toolkit.ValidationError({"custom_key": [_("This does not look like an API key.")]})
    other = key_of_another_service(name, value)
    if other:
        raise toolkit.ValidationError({"custom_key": [_("This looks like a {service} key. Use the key of that service in the list above instead of adding it under another name.").format(service=other)]})
    if not can_store_keys():
        raise toolkit.ValidationError({"custom_key": [_("Keys cannot be stored on this portal.")]})
    settings.write_row(KEY_PREFIX + name, encrypt(value))
    _save_custom_key_names(custom_key_names() + [name])
    log.info("AI key added on the admin page: %s (by %s)", name, user or "?")


def remove_custom_key(name, user=""):
    """Remove a key added on the page, with its name. Not while it is the chosen key."""
    if not is_custom_key(name):
        return
    if settings.value("api_key_env") == name:
        raise toolkit.ValidationError({"remove_key": [_("This key is the chosen one. Choose another key and save before you remove it.")]})
    from ckanext.ai_connector import purposes

    used = [row["label"] for row in purposes.saved() if row.get("api_key_env") == name]
    if used:
        raise toolkit.ValidationError({"remove_key": [_("This key is chosen for: {purposes}. Choose another key for them "
                                                        "first.").format(purposes=", ".join(used))]})
    # The name first: a name without a key is a dead end, a key without a name is only an unused row.
    _save_custom_key_names([other for other in custom_key_names() if other != name])
    settings.delete_row(KEY_PREFIX + name)
    log.info("AI key removed on the admin page: %s (by %s)", name, user or "?")


def _fernet():
    """The cipher for stored keys, from the portal's own secret. None when the portal has no secret or cryptography is not
    installed."""
    secret = toolkit.config.get("SECRET_KEY") or toolkit.config.get("beaker.session.secret")
    if not secret:
        return None
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        return None
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(str(secret).encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(value):
    """A key as it is stored: the version of the derivation, a colon and the Fernet token. Raises ValueError when keys
    cannot be stored on this portal (no secret, or cryptography missing)."""
    fernet = _fernet()
    if fernet is None:
        raise ValueError("Keys cannot be stored on this portal.")
    return "%s:%s" % (TOKEN_VERSION, fernet.encrypt(value.encode()).decode())


def decrypt(token):
    """The key in a stored token. Raises KeyUnreadable when it cannot be decrypted (the portal's secret changed, or a token
    of another version)."""
    version, _sep, body = (token or "").partition(":")
    fernet = _fernet()
    if version != TOKEN_VERSION or not body or fernet is None:
        raise KeyUnreadable()
    from cryptography.fernet import InvalidToken

    try:
        return fernet.decrypt(body.encode()).decode()
    except InvalidToken as error:
        raise KeyUnreadable() from error


def can_store_keys():
    return _fernet() is not None


def _stored_token(name):
    return settings.saved_row(KEY_PREFIX + name)


def stored_key(name):
    """The key entered on the page for this variable, or None. Raises KeyUnreadable if it is there but cannot be
    decrypted."""
    if not isinstance(name, str) or name not in allowed_key_envs():
        return None
    token = _stored_token(name)
    if not token:
        return None
    try:
        return decrypt(token)
    except KeyUnreadable as error:
        raise KeyUnreadable(name) from error


def effective_key(name):
    """The key that is used for this variable: the page's, else the server's. None when a key entered on the page cannot be
    read: the sysadmin chose that key, so the server's does not stand in for it (as in the connection)."""
    try:
        key = stored_key(name)
    except KeyUnreadable:
        return None
    if is_custom_key(name):
        return key   # a key added on the page is never looked up in the server's environment
    return key or os.environ.get(name) or None


# How a key of a known service begins: a pasted key of another service is caught before it is stored (it would only fail
# at the first call).
_KEY_STARTS = {"sk-ant-": "ANTHROPIC_API_KEY", "AIza": "GEMINI_API_KEY"}
_SERVICE_NAMES = {"ANTHROPIC_API_KEY": "Anthropic", "GEMINI_API_KEY": "Gemini"}


def key_of_another_service(name, value):
    """The name of the service whose key ``value`` looks like, when that is not the service of ``name``; else None."""
    for start, env in _KEY_STARTS.items():
        if (value or "").startswith(start) and env != name:
            return _SERVICE_NAMES[env]
    return None


def _points_elsewhere(value):
    """LiteLLM reads a key written as ``os.environ/NAME`` as that environment variable: a way round the allowlist."""
    return value.lower().startswith("os.environ/")


def check_key(name, value):
    """Raise ValidationError when ``value`` cannot be the key for ``name``."""
    if name not in allowed_key_envs():
        raise toolkit.ValidationError({"api_key_env": [_("Choose one of the allowed keys.")]})
    if not KEY_MIN_LENGTH <= len(value) <= KEY_MAX_LENGTH or any(c.isspace() for c in value) or _points_elsewhere(value):
        raise toolkit.ValidationError({"api_key": [_("This does not look like an API key.")]})
    other = key_of_another_service(name, value)
    if other:
        raise toolkit.ValidationError({"api_key": [_("This looks like a {service} key, not the key of the chosen variable. Choose that key instead, or check what you pasted.").format(service=other)]})


def store_key(name, value):
    """Store a key entered on the page, encrypted. Never logged or shown."""
    value = (value or "").strip()
    check_key(name, value)
    if not can_store_keys():
        raise toolkit.ValidationError({"api_key": [_("Keys cannot be stored on this portal.")]})
    settings.write_row(KEY_PREFIX + name, encrypt(value))


def remove_key(name):
    if name in allowed_key_envs():
        settings.delete_row(KEY_PREFIX + name)


def _tail(value):
    return value[-4:] if value and len(value) >= _TAIL_MIN_LENGTH else ""


def key_status():
    """For each allowed key variable: whether a key is set, where it comes from, and its last four characters so a sysadmin
    can tell which key it is (as with a card number). Never more of the value."""
    rows = []
    for name in allowed_key_envs():
        custom = is_custom_key(name)
        server = "" if custom else (os.environ.get(name) or "")
        row = {"name": name, "custom": custom, "set": bool(server), "tail": _tail(server),
               "source": "server" if server else "", "unreadable": False,
               "server_set": bool(server), "server_tail": _tail(server)}
        try:
            found = stored_key(name)
        except KeyUnreadable:
            found, row["unreadable"] = None, True
        if found:
            row.update(set=True, tail=_tail(found), source="page")
        rows.append(row)
    return rows


def library_version():
    """The installed version of LiteLLM, the library the portal talks to AI services through, if any."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("litellm")
    except (PackageNotFoundError, ValueError):
        return ""


# The model suggested first for a service when nothing is chosen yet: a fast, inexpensive model, the first one LiteLLM's
# catalogue has. Fast matters: a model that thinks long (gemini-3.5-flash took over 30 s for the connection test on a
# server, 2026-11-19) runs into the time limit. A suggestion only: the sysadmin sees it and saves it, or chooses another.
RECOMMENDED_MODELS = {
    "gemini": ("gemini/gemini-3.5-flash-lite", "gemini/gemini-2.5-flash-lite", "gemini/gemini-flash-lite-latest"),
    "anthropic": ("anthropic/claude-haiku-4-5", "anthropic/claude-sonnet-4-5"),
    "openai": ("openai/gpt-5-mini", "openai/gpt-4.1-mini", "openai/gpt-4o-mini"),
    "mistral": ("mistral/mistral-small-latest", "mistral/mistral-medium-latest"),
}


def recommended_model(suggestions, key_env=""):
    """The model to suggest when none is chosen: one of RECOMMENDED_MODELS for the service of the chosen key ``key_env``,
    if the list has it. Never a model of another service (the chosen key would go to it), and nothing for a service without
    a preference (the first model in alphabetical order is not a recommendation)."""
    provider = next((p for p, env in PROVIDER_KEYS.items() if env == key_env), None)
    listed = {row["model"] for row in suggestions if row["provider"] == provider}
    return next((model for model in RECOMMENDED_MODELS.get(provider, ()) if model in listed), "")


def key_services():
    """The services a key can be added for on the page, by their names: ``{"env", "label", "set"}``, for the allowed
    variables of the known services."""
    allowed = allowed_key_envs()
    return [{"env": env, "label": SERVICE_LABELS.get(provider, provider), "set": bool(effective_key(env))}
            for provider, env in PROVIDER_KEYS.items() if env in allowed]


def model_suggestions():
    """Text models from LiteLLM's own catalogue (it ships with the library, no network call), for the providers whose key is
    set. The field stays free text: the catalogue lags behind new models until LiteLLM is updated."""
    try:
        import litellm
    except ImportError:
        return []
    usable = {provider for provider, env in PROVIDER_KEYS.items() if env in allowed_key_envs() and effective_key(env)}
    found = {}
    for name, info in litellm.model_cost.items():
        provider = info.get("litellm_provider")
        if provider not in usable or info.get("mode") != "chat" or not info.get("supports_response_schema"):
            continue
        model_name = name if name.startswith(provider + "/") else "%s/%s" % (provider, name)
        if any(word in model_name for word in _NOT_FOR_TEXT):
            continue
        # Can it read a web page itself: Gemini's URL context, or Claude's web fetch (a server tool: LiteLLM's catalogue only
        # says the model can use Anthropic's web tools).
        found[model_name] = {"model": model_name, "provider": provider,
                             "page_read": bool(info.get("supports_url_context")
                                               or (provider == "anthropic" and info.get("supports_web_search")))}
    return sorted(found.values(), key=lambda row: row["model"])
