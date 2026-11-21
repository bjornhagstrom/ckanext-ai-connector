"""The contract other extensions use. Import from here only (and from ``ckanext.ai_connector.testing`` in tests).

Version 0.x may still change this module; from 1.0 nothing here is removed or changed within a major version, and
``API_VERSION`` grows by one when something is added. ``read_page`` is experimental until it is marked stable.

A consumer::

    try:
        from ckanext.ai_connector import api
    except ImportError:
        api = None

    def summary(text):
        reason = _("ckanext-ai-connector is not installed.") if api is None else api.unavailable_reason(min_api=1)
        if reason:
            raise toolkit.ValidationError({"ai": [reason]})
        try:
            result = api.client("my_extension").text([{"role": "user", "content": "Summarise: " + text}])
        except api.AIError as error:
            raise toolkit.ValidationError({"ai": [str(error)]})
        return result.text

Nothing here checks who may make a call, saves or publishes anything: the consumer checks access before every call (a call
costs money), and treats the answer as untrusted text.
"""

from __future__ import annotations

from ckanext.ai_connector.client import PAGE_READ, AIClient, AIConfigurationError, AIError, AIResponseError, AIResult

API_VERSION = 3

__all__ = ["API_VERSION", "PAGE_READ", "AIClient", "AIConfigurationError", "AIError", "AIResponseError", "AIResult",
           "client", "declare_purposes", "describe", "is_ready", "unavailable_reason"]


def _plugin_loaded() -> bool:
    try:
        from ckan.plugins import plugin_loaded

        return plugin_loaded("ai_connector")
    except Exception:
        return False


def _too_old(min_api: int) -> str | None:
    from ckanext.ai_connector.localization import _

    if API_VERSION < min_api:
        return _("This needs version {required} of the AI connection's API (ckanext-ai-connector); version {installed} is installed.").format(
            required=min_api, installed=API_VERSION)
    return None


def _chosen(consumer, purpose):
    """The stored choice for a consumer's purpose, or None (the main connection applies)."""
    if not (consumer and purpose):
        return None
    from ckanext.ai_connector import purposes

    return purposes.choice(consumer, purpose)


def unavailable_reason(min_api: int = 1, consumer: str | None = None, purpose: str | None = None) -> str | None:
    """Why AI calls cannot be made here, as a translated sentence for a sysadmin, or None when they can. Covers an API older
    than ``min_api``, the ``ai_connector`` plugin not switched on, a missing library, no model and no key. With ``consumer``
    and ``purpose`` (API 2), for the model and key chosen for that purpose, if any. Never raises."""
    from ckanext.ai_connector.localization import _

    too_old = _too_old(min_api)
    if too_old:
        return too_old
    if not _plugin_loaded():
        return _("The AI connection (the ai_connector plugin) is not switched on. Add ai_connector to ckan.plugins.")
    try:
        from ckanext.ai_connector import checks
        from ckanext.ai_connector.connection import ConnectionSettings

        problems = [check for check in checks.run_checks() if check["status"] != checks.OK]
        library = next((check for check in problems if check["key"] == "ai_library"), None)
        if library:
            return library["detail"]
        from ckanext.ai_connector import purposes

        try:
            chosen = _chosen(consumer, purpose)
        except purposes.PurposeError as error:
            return str(error)
        if chosen is not None:
            try:
                current = ConnectionSettings.for_purpose(consumer, purpose)
                if not current.model:
                    return _("The AI model is missing from the configuration.")
                current.key()
            except AIError as error:
                return str(error)
            return None
    except Exception:   # never raise from here: a broken setup is a reason, not an error
        return _("The AI connection cannot be checked.")
    for check in problems:
        if check["key"] in ("ai_plugin", "ai_model", "ai_library", "ai_key"):
            return check["detail"]
    return None


def is_ready(min_api: int = 1, consumer: str | None = None, purpose: str | None = None) -> bool:
    """Can AI calls be made here (for that purpose): :func:`unavailable_reason` has nothing to say."""
    return unavailable_reason(min_api, consumer, purpose) is None


def describe(consumer: str | None = None, purpose: str | None = None) -> dict:
    """The connection in use (for that purpose, API 2), for showing or deciding: ``model``, ``service`` (the part of the
    model before the slash), ``capabilities`` (a list, maybe holding ``page_read``), ``timeout_seconds``,
    ``page_read_timeout_seconds`` and (API 3) ``source``: ``"purpose"`` when a model is chosen for the purpose, ``"main"``
    when the main connection applies. Never the key or the service's address."""
    from ckanext.ai_connector import purposes
    from ckanext.ai_connector.client import capabilities_of, service_of
    from ckanext.ai_connector.connection import ConnectionSettings

    try:
        current = ConnectionSettings.for_purpose(consumer, purpose) if consumer else ConnectionSettings.in_use()
    except AIError:
        current = None
    model = current.model if current else ""
    return {"model": model, "service": service_of(model), "capabilities": sorted(capabilities_of(model)),
            "timeout_seconds": current.timeout_seconds if current else None,
            "page_read_timeout_seconds": current.page_read_timeout_seconds if current else None,
            "source": purposes.source(consumer, purpose)}


def declare_purposes(consumer: str, purposes: dict, *, name=None) -> None:
    """API 2. Name what ``consumer`` uses the AI for, so that the AI settings page can offer a model per purpose:
    ``{"translate": _("Translation"), ...}``, or a function per purpose that returns the label in the page's language.
    Purpose names are lower case (letters, digits, ``_``). Call it when the plugin loads (for example in ``update_config``).
    Without a model chosen for it, a purpose uses the main connection. ``name`` (API 3): the extension's name as the page
    shows it over its purposes (a text, or a function returning it in the page's language); without it, ``consumer``."""
    from ckanext.ai_connector import purposes as _purposes

    _purposes.declare(consumer, purposes, name=name)


def client(consumer: str, *, purpose: str | None = None, timeout_seconds: float | None = None,
           max_attempts: int | None = None) -> AIClient:
    """A client for the connection in use. ``consumer`` is a short name for the calling extension (lower case, such as
    ``"my_extension"``): it is in every log line, and limits shared between consumers will be counted by it. ``purpose``
    (API 2) is one of the consumer's declared purposes: the model and key chosen for it, else the main connection.
    ``timeout_seconds`` replaces the time limit for ordinary calls (at most 600 s); ``max_attempts`` (default 2) is how
    often a call is tried after a transient failure. Raises AIConfigurationError when the connection cannot be used."""
    from ckanext.ai_connector.connection import ConnectionSettings
    from ckanext.ai_connector.localization import _

    if not _plugin_loaded():
        raise AIConfigurationError(_("The AI connection (the ai_connector plugin) is not switched on. Add ai_connector to ckan.plugins."))
    return ConnectionSettings.for_purpose(consumer, purpose).client(consumer, timeout_seconds=timeout_seconds,
                                                                    max_attempts=max_attempts)
