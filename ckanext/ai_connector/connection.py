"""The connection in use, as a value, and the client built from it. Internal: other extensions use ``api``."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace

from ckanext.ai_connector import settings as _settings
from ckanext.ai_connector.client import AIClient, AIConfigurationError, litellm_transport
from ckanext.ai_connector.localization import _

log = logging.getLogger(__name__)


# Services LiteLLM routes under another name than their key's: the key is the same service's.
_SAME_SERVICE = {"codestral": "mistral", "text-completion-codestral": "mistral", "text-completion-openai": "openai"}


def routed_service(model):
    """The service LiteLLM sends ``model`` to, as LiteLLM itself decides (``gpt-4o`` goes to OpenAI without a prefix), in
    lower case; the prefix when LiteLLM does not know the model (then it makes no call either)."""
    try:
        import litellm

        provider = litellm.get_llm_provider(model=model)[1]
    except Exception:
        provider = model.split("/", 1)[0] if "/" in model else ""
    provider = (provider or "").lower()
    return _SAME_SERVICE.get(provider, provider)


def _is_custom(name, is_custom_key):
    try:
        return is_custom_key(name)
    except Exception:   # cannot tell: refuse rather than risk sending a key of one's own to a known service
        return True


def pairing_error(model, api_key_env, api_base=""):
    """Why ``api_key_env`` must not be used with ``model``, or "": a known service's key only goes to that service's
    models, and a key entered on the page under a name of one's own never to a known service. A variable the server
    allows under another name (``api_key_envs``) is the server's choice and goes where its model goes. Not with an AI
    server of one's own (``api_base``), where the key goes to that server."""
    from ckanext.ai_connector.keys import PROVIDER_KEYS, SERVICE_LABELS, is_custom_key

    if api_base or not model or not api_key_env:
        return ""
    service = routed_service(model)
    owner = next((name for name, env in PROVIDER_KEYS.items() if env == api_key_env), None)
    if owner and service != owner:
        return _("The key {name} is for {service}, not for the model {model}. Choose a model of {service}, or that "
                 "model's key.").format(name=api_key_env, service=SERVICE_LABELS.get(owner, owner), model=model)
    if not owner and service in PROVIDER_KEYS and _is_custom(api_key_env, is_custom_key):
        return _("The model {model} is {service}'s: choose its key, {name}.").format(
            model=model, service=SERVICE_LABELS.get(service, service), name=PROVIDER_KEYS[service])
    return ""


@dataclass(frozen=True)
class ConnectionSettings:
    model: str
    api_key_env: str
    api_base: str | None = None
    timeout_seconds: float = 30.0
    page_read_timeout_seconds: float = 180.0
    # A key entered on the settings page (it goes before the server's environment variable), or one submitted for a test.
    api_key: str | None = field(default=None, repr=False, compare=False)
    # False for a key added on the settings page for another service: it is never looked up in the server's environment,
    # also when the stored key is missing or cannot be read (then there is no key, and that is said).
    environment_allowed: bool = field(default=True, compare=False)
    # A key is stored on the settings page but cannot be decrypted (the portal's secret changed). The sysadmin chose the
    # page's key, so the server's variable is not used instead: there is no key until it is entered again (as the setup
    # check and api.unavailable_reason() say).
    stored_unreadable: bool = field(default=False, compare=False)

    @classmethod
    def from_values(cls, values):
        """The settings from a dict of values keyed by the names without prefix (see ``settings.current()``)."""
        try:
            timeout = float(values.get("timeout_seconds") or 30.0)
            page_timeout = float(values.get("page_read_timeout_seconds") or 180.0)
        except (TypeError, ValueError) as error:
            raise AIConfigurationError(_("The AI timeout must be a number of seconds.")) from error
        if timeout <= 0 or page_timeout <= 0:
            raise AIConfigurationError(_("The AI timeout must be greater than zero."))
        return cls(model=str(values.get("model") or "").strip(), api_key_env=str(values.get("api_key_env") or "").strip(),
                   api_base=str(values.get("api_base") or "").strip() or None, timeout_seconds=timeout,
                   page_read_timeout_seconds=page_timeout)

    @classmethod
    def in_use(cls):
        """The connection in use, with a key entered on the settings page. One read of the database."""
        with _settings.reading():
            return cls.from_values(_settings.current()).with_stored_key()

    @classmethod
    def for_purpose(cls, consumer, purpose=None):
        """The connection for one of a consumer's purposes: the model and key chosen for it on the settings page, with the
        main connection's time limits and AI server; the main connection when nothing is chosen. A choice that cannot be
        read is refused (AIConfigurationError), not replaced by the main connection."""
        from ckanext.ai_connector import purposes

        with _settings.reading():
            main = cls.in_use()
            if not purpose:
                return main
            try:
                chosen = purposes.choice(consumer, purpose)
            except purposes.PurposeError as error:
                raise AIConfigurationError(str(error)) from None
            if chosen is None:
                return main
            if chosen.get("broken"):
                raise AIConfigurationError(purposes.broken_reason(consumer, purpose))
            from ckanext.ai_connector import keys

            if chosen["api_key_env"] not in keys.allowed_key_envs():
                # Checked when the choice was saved, and again here: the allowlist may have been narrowed since, a key
                # added on the page removed, or the row written some other way. Never read another variable.
                raise AIConfigurationError(_("The key {name} chosen for {purpose} is not one of the allowed keys. Choose "
                                             "another on the AI settings page.").format(
                                                 name=chosen["api_key_env"], purpose=purposes.label(consumer, purpose)))
            return replace(main, model=chosen["model"], api_key_env=chosen["api_key_env"], api_key=None,
                           environment_allowed=True, stored_unreadable=False).with_stored_key()

    def with_stored_key(self):
        from ckanext.ai_connector import keys

        with _settings.reading():
            try:
                key = keys.stored_key(self.api_key_env)
            except keys.KeyUnreadable:
                log.warning("The AI key stored for %s cannot be decrypted; enter it again.", self.api_key_env)
                key = None
                if not self.api_key:
                    return replace(self, stored_unreadable=True,
                                   environment_allowed=not keys.is_custom_key(self.api_key_env))
            result = replace(self, api_key=key) if key and not self.api_key else self
            if keys.is_custom_key(self.api_key_env):
                result = replace(result, environment_allowed=False)
        return result

    def key(self):
        """The key the call is made with: the page's (or a submitted one), else the server's environment variable, else
        "not-needed" for an own AI server. Raises AIConfigurationError when there is none, or when the key belongs to
        another service than the model (it would be sent there)."""
        if not self.api_key_env:
            raise AIConfigurationError(_("The name of the environment variable for the AI key is missing."))
        mismatch = pairing_error(self.model, self.api_key_env, self.api_base)
        if mismatch:
            raise AIConfigurationError(mismatch)
        if self.stored_unreadable and not self.api_key:
            raise AIConfigurationError(_("A stored key cannot be read (the portal's secret has changed)."))
        if not self.environment_allowed and not self.api_key:
            raise AIConfigurationError(
                _("The key {name} added on the settings page is missing or cannot be read. Enter it again.").format(name=self.api_key_env))
        found = self.api_key or (os.environ.get(self.api_key_env) if self.environment_allowed else None)
        if not found and self.api_base:
            # An AI server the organisation runs itself (``api_base``, set in the server's configuration): often it has no
            # key, and LiteLLM wants some text for an OpenAI-compatible endpoint. A server that does need a key answers 401,
            # which the test of the connection shows.
            found = "not-needed"
        if not found:
            raise AIConfigurationError(_("The environment variable '{name}' is missing or empty.").format(name=self.api_key_env))
        return found

    def client(self, consumer, *, timeout_seconds=None, max_attempts=None, transport=None, sleep_fn=None,
               retry_delay_seconds=None):
        if not self.model:
            raise AIConfigurationError(_("The AI model is missing from the configuration."))
        options = {"timeout_seconds": timeout_seconds or self.timeout_seconds,
                   "page_read_timeout_seconds": self.page_read_timeout_seconds}
        if max_attempts is not None:
            options["max_attempts"] = max_attempts
        if sleep_fn is not None:
            options["sleep_fn"] = sleep_fn
        if retry_delay_seconds is not None:
            options["retry_delay_seconds"] = retry_delay_seconds
        if transport is None:
            transport = litellm_transport(self.key(), self.api_base)
        return AIClient(consumer, self.model, transport, **options)
