"""Actions for sysadmins: try the connection, forget the saved settings, and list the setup checks."""

import time
from dataclasses import replace

from ckan.plugins import toolkit

from ckanext.ai_connector import keys, settings
from ckanext.ai_connector.client import AIError
from ckanext.ai_connector.connection import ConnectionSettings
from ckanext.ai_connector.localization import _

# A small task every model can do, as JSON (so that the strict answer shape is tried too).
_TEST_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
                "additionalProperties": False}
_TEST_MESSAGES = [{"role": "user", "content": 'What is the capital of France? Answer with JSON: {"answer": "<the city>"}.'}]


def settings_with(data_dict):
    """The connection in use, with submitted (unsaved) values on top, and a key in ``api_key`` (not yet saved) if one is
    given. Raises ValidationError for a key variable outside the allowlist or a key that cannot be one."""
    with settings.reading():
        values = settings.current()   # saved values before the server's, as in use
        for name in settings.FIELDS:
            submitted = data_dict.get(name)
            if submitted not in (None, ""):
                values[name] = submitted
        if data_dict.get("api_key_env") and data_dict["api_key_env"] not in keys.allowed_key_envs():
            raise toolkit.ValidationError({"api_key_env": [_("Choose one of the allowed keys.")]})
        current = ConnectionSettings.from_values(values)
        key = (data_dict.get("api_key") or "").strip()
        if key:
            keys.check_key(current.api_key_env, key)
            current = replace(current, api_key=key)
        return current.with_stored_key()


def _status_hint(code):
    """What the sysadmin can do about the most common answers of an AI service, so that a key that is wrong does not read
    as a service that cannot be reached."""
    if code in (401, 403):
        return _("The AI service did not accept the key. Check that the key is right and that it is for the service of "
                 "the model.")
    if code == 404:
        # Also a model the service has retired (Google first closes old models to new accounts, while still listing them).
        return _("The AI service does not know the model, no longer offers it (old models are retired, sometimes first for "
                 "new accounts), or the key does not give access to it. Check the name, or choose a newer model.")
    if code in (408, 504):
        return _("The AI service did not answer within the time limit. Choose a faster model (for example one called "
                 "\"lite\" or \"mini\"), or raise the time limit for an ordinary call.")
    if code in (500, 502, 503):
        return _("The AI service is overloaded or down for the moment; it is not the settings. Try again in a while.")
    if code == 429:
        return _("The AI service refused more calls for now (too many calls, or the account's quota is used up). Try again "
                 "later, or check the account at the service.")
    return ""


def ai_connector_test(context, data_dict):
    """Try the connection with one small call, using the submitted values on top of the effective ones, without saving
    them. Never returns the key: only whether the call worked, how long it took, the answer, or the kind of error."""
    toolkit.check_access("ai_connector_settings", context, data_dict)
    try:
        current = settings_with(data_dict)
    except AIError as error:
        return {"success": False, "error": str(error)}
    return _try(current)


def _try(current):
    """One small call with a connection: whether it worked, how long it took, the answer, or the kind of error."""
    try:
        client = current.client("ai_connector_test", max_attempts=1)
    except AIError as error:
        return {"success": False, "error": str(error), "model": current.model}
    start = time.monotonic()
    try:
        answer = client.structured(_TEST_MESSAGES, _TEST_SCHEMA, name="connection_test", temperature=0.1).data
    except AIError as error:
        detail = error.error_type or ""
        if error.status_code:
            detail += " (%s)" % error.status_code
        return {"success": False, "error": str(error), "hint": _status_hint(error.status_code), "detail": detail,
                "model": current.model}
    return {"success": True, "model": current.model, "answer": str(answer.get("answer") or ""),
            "seconds": round(time.monotonic() - start, 1)}


def ai_connector_test_purposes(context, data_dict):
    """Try the model every declared purpose uses: its own, or the main connection's. One small call per model and key
    (purposes that share them share the answer); nothing is saved. ``consumer`` limits it to one extension's purposes, and
    ``choices`` (``{"consumer.purpose": {"model", "api_key_env"}}``, the values typed on the page) are tried instead of
    the saved ones; an empty model there is the main model. A list in the order of the page, each with ``consumer``,
    ``purpose``, ``label``, ``source`` ("purpose" or "main") and the result of a test."""
    from ckanext.ai_connector import purposes

    toolkit.check_access("ai_connector_settings", context, data_dict)
    choices = data_dict.get("choices") or {}
    if not isinstance(choices, dict) or not all(isinstance(c, dict) for c in choices.values()):
        raise toolkit.ValidationError({"choices": [_("Give the choices as {\"consumer.purpose\": {\"model\": ..., \"api_key_env\": ...}}.")]})
    only = data_dict.get("consumer")
    tried, results = {}, []
    rows = [(consumer, purpose) for consumer, declared in purposes.declared().items() for purpose in declared
            if not only or consumer == only]
    for consumer, purpose in sorted(rows, key=lambda r: (r[0], purposes.label(*r).lower())):
        typed = choices.get("%s.%s" % (consumer, purpose))
        row = {"consumer": consumer, "purpose": purpose, "label": purposes.label(consumer, purpose),
               "source": ("purpose" if typed.get("model") else "main") if typed is not None
               else purposes.source(consumer, purpose)}
        if typed is not None:
            typed = {"model": str(typed.get("model") or "").strip(), "api_key_env": str(typed.get("api_key_env") or "").strip()}
        if typed is not None and typed["model"]:
            # Typed, not saved: checked as a save would check it, before anything is sent.
            if not typed["api_key_env"]:
                results.append(dict(row, success=False, error=_("Choose the API key for this model."), model=typed["model"]))
                continue
            from ckanext.ai_connector.connection import pairing_error

            try:
                typed = {"model": settings.VALIDATORS["model"](typed["model"]),
                         "api_key_env": settings.VALIDATORS["api_key_env"](typed["api_key_env"])}
            except toolkit.Invalid as error:
                results.append(dict(row, success=False, error=error.error, model=typed["model"]))
                continue
            mismatch = pairing_error(typed["model"], typed["api_key_env"], settings.value("api_base"))
            if mismatch:
                results.append(dict(row, success=False, error=mismatch, model=typed["model"]))
                continue
        try:
            current = ConnectionSettings.for_purpose(consumer, purpose, choice=typed)
        except AIError as error:
            results.append(dict(row, success=False, error=str(error), model=""))
            continue
        pair = (current.model, current.api_key_env, current.api_base)
        if pair not in tried:
            tried[pair] = _try(current)
        results.append(dict(row, **tried[pair]))
    return results


def ai_connector_reset(context, data_dict):
    """Forget the connection settings saved in the interface; the server's apply again. Keys entered on the page stay."""
    toolkit.check_access("ai_connector_settings", context, data_dict)
    settings.reset()
    return {"reset": list(settings.FIELDS)}


@toolkit.side_effect_free
def ai_connector_status(context, data_dict):
    """What the connection needs and what is missing. Calls no AI service."""
    from ckanext.ai_connector import checks

    toolkit.check_access("ai_connector_settings", context, data_dict)
    result = checks.run_checks()
    return {"ok": all(c["status"] != checks.ERROR for c in result), "checks": result}


def get_actions():
    return {"ai_connector_test": ai_connector_test, "ai_connector_test_purposes": ai_connector_test_purposes,
            "ai_connector_reset": ai_connector_reset, "ai_connector_status": ai_connector_status}


def ai_settings_auth(context, data_dict):
    """Sysadmins only (they pass every check)."""
    return {"success": False}
