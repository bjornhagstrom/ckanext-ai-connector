"""What the connection needs, and what is missing. Calls no AI service and shows no key, only whether one is found.

Used by the settings page, the ``ai_connector_status`` action, ``ckan ai-connector doctor`` and ``api.unavailable_reason()``.
"""

import importlib.util

from ckanext.ai_connector import keys, settings
from ckanext.ai_connector.localization import _

OK, WARN, ERROR = "ok", "warning", "error"


def _check(key, status, title, detail="", fix=""):
    return {"key": key, "status": status, "title": title, "detail": detail, "fix": fix}


def _plugin_loaded():
    try:
        from ckan.plugins import plugin_loaded

        return plugin_loaded("ai_connector")
    except Exception:
        return False


def run_checks():
    from ckanext.ai_connector.client import capabilities_of

    checks = []
    if not _plugin_loaded():
        checks.append(_check("ai_plugin", ERROR, _("AI connection"), _("The ai_connector plugin is not switched on."),
                             _("Add ai_connector to ckan.plugins.")))
    missing = [name for name in ("litellm", "cryptography") if importlib.util.find_spec(name) is None]
    if missing:
        checks.append(_check("ai_library", ERROR, _("AI library"),
                             _("Software that the AI connection needs is not installed: {names}.").format(names=", ".join(missing)),
                             "pip install ckanext-ai-connector"))
    with settings.reading():
        model_name = settings.value("model")
        if not model_name:
            return checks + [_check("ai_model", WARN, _("AI model"), _("No AI model is set, so nothing that needs the AI works."),
                                    _("Choose a model on the AI settings page."))] + purpose_checks()
        detail = model_name
        if capabilities_of(model_name):
            detail += " (" + _("can read web pages itself") + ")"
        checks.append(_check("ai_model", OK, _("AI model"), detail))
        env = settings.value("api_key_env")
        row = next((r for r in keys.key_status() if r["name"] == env), None)
        api_base = settings.value("api_base")
    from ckanext.ai_connector.connection import pairing_error

    mismatch = pairing_error(model_name, env, api_base) if env else ""
    if not env:
        checks.append(_check("ai_key", ERROR, _("API key"), _("No key is chosen."), _("Choose a key on the AI settings page.")))
    elif mismatch:
        # Every call would be refused: the key belongs to another service than the model.
        checks.append(_check("ai_key", ERROR, _("API key"), mismatch,
                             _("Choose a model and a key of the same service on the AI settings page.")))
    elif row and row["unreadable"]:
        checks.append(_check("ai_key", ERROR, _("API key"), _("A stored key cannot be read (the portal's secret has changed)."),
                             _("Enter the key again on the AI settings page.")))
    elif row and row["set"]:
        where = _("entered on the settings page") if row["source"] == "page" else _("set on the server")
        checks.append(_check("ai_key", OK, _("API key"), "%s, %s" % (env, where)))
    elif api_base:
        checks.append(_check("ai_key", OK, _("API key"),
                             _("None needed: the AI server at {address} is set in the server's configuration. If it asks for a key, enter one on the AI settings page.").format(
                                 address=api_base)))
    else:
        checks.append(_check("ai_key", ERROR, _("API key"), _("No API key is found for the chosen key name."),
                             _("Enter a key on the AI settings page, or set it on the server.")))
    return checks + purpose_checks()


def purpose_checks():
    """One check per model chosen for a purpose that cannot be used, or that no installed extension declares."""
    from ckanext.ai_connector import purposes
    from ckanext.ai_connector.client import AIError
    from ckanext.ai_connector.connection import ConnectionSettings

    found = []
    with settings.reading():
        for row in purposes.saved():
            title = _("Model for {purpose}").format(purpose=row["label"])
            if row.get("broken"):
                found.append(_check("ai_purpose", ERROR, title, purposes.broken_reason(row["consumer"], row["purpose"]),
                                    _("Choose the model again on the AI settings page.")))
                continue
            if not row["declared"]:
                found.append(_check("ai_purpose", WARN, title,
                                    _("A model is chosen for {consumer}'s purpose {purpose}, which no installed extension "
                                      "declares.").format(consumer=row["consumer"], purpose=row["purpose"]),
                                    _("Remove the choice on the AI settings page, or ignore it.")))
                continue
            try:
                current = ConnectionSettings.for_purpose(row["consumer"], row["purpose"])
                if not current.model:
                    raise AIError(_("The AI model is missing from the configuration."))
                current.key()
            except AIError as error:
                found.append(_check("ai_purpose", ERROR, title, str(error),
                                    _("Choose a model and a key of the same service for it on the AI settings page.")))
    return found
