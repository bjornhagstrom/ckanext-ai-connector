"""The connection settings: which model and key the portal uses, and the time limits.

Runtime-editable CKAN options (``update_config_schema``): stored by CKAN's ``config_option_update`` in the database, applied at
once in every worker, and falling back to the server's configuration (.env or the ini file) when nothing is stored. A value
saved in the interface goes before the server's.

``api_base``, the address of an AI server of your own, and ``api_key_envs``, the allowlist of key variables, come from the
server's configuration only (``SERVER_ONLY``), never from a value in the database: changing them in the interface could send
the key to another server, or another secret to the AI service.

The rows this module reads from CKAN's ``system_info`` table are read in one query and kept for the rest of the web request
(``flask.g``), or for the duration of one :func:`reading` block outside a request (a command, a background job); every write
here clears them.
"""

import contextlib
import contextvars
import re
import time

from ckan import model
from ckan.plugins import toolkit

from ckanext.ai_connector.localization import _

PREFIX = "ckanext.ai_connector."
# What the settings page saves.
FIELDS = ("model", "api_key_env", "timeout_seconds", "page_read_timeout_seconds")
# Every setting, also those the page cannot change.
NAMES = FIELDS + ("api_key_envs", "api_base")
# Never read from the database, only from the server's configuration.
SERVER_ONLY = frozenset({"api_base", "api_key_envs"})
DEFAULTS = {"timeout_seconds": "30", "page_read_timeout_seconds": "180"}
# The longest time limit anyone may set or ask for, in seconds.
MAX_SECONDS = 600
_MODEL = re.compile(r"^[A-Za-z0-9._:/@-]{1,200}$")


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


# --- the rows in system_info ------------------------------------------------------------------------------------------

_outside_request = contextvars.ContextVar("ai_connector_rows", default=None)


class _Holder:
    rows = None


def _holder():
    """Where the rows are kept: flask.g in a web request, else the current :func:`reading` block, else nowhere."""
    try:
        from flask import g, has_request_context

        if has_request_context():
            return g
    except Exception:
        pass
    return _outside_request.get()


@contextlib.contextmanager
def reading():
    """Read the rows once for everything inside the block, outside a web request too."""
    if _holder() is not None:
        yield
        return
    token = _outside_request.set(_Holder())
    try:
        yield
    finally:
        _outside_request.reset(token)


def _rows():
    """This extension's rows in system_info (its settings, the stored keys and the list of added keys), in one query."""
    holder = _holder()
    cached = getattr(holder, "_ai_connector_rows", None) if holder is not None else None
    if cached is not None:
        return cached
    column = model.SystemInfo.key
    rows = dict(model.Session.query(column, model.SystemInfo.value).filter(
        column.like(PREFIX.replace("_", r"\_") + "%", escape="\\")).all())
    if holder is not None:
        holder._ai_connector_rows = rows
    return rows


def forget_rows():
    """Read the rows again at the next read (after a write)."""
    holder = _holder()
    if holder is not None:
        holder._ai_connector_rows = None


def saved_row(key):
    """A row of system_info that this extension reads (a setting, a stored key, the list of added keys), or None."""
    return _rows().get(key)


def write_row(key, value):
    model.set_system_info(key, value)
    forget_rows()


def delete_row(key):
    model.delete_system_info(key)
    forget_rows()


# --- values ------------------------------------------------------------------------------------------------------------

def _server(key):
    """A key's value in the server's configuration. Without a saved value CKAN's configuration holds it; with one, CKAN
    kept it aside the first time it applied saved values (ckan.lib.app_globals._CONFIG_CACHE, in CKAN 2.11 and 2.12)."""
    from ckan.lib import app_globals

    if _blank(saved_row(key)) or key not in app_globals._CONFIG_CACHE:
        return toolkit.config.get(key) or ""   # (a value saved but not applied yet: the configuration still holds the server's)
    return app_globals._CONFIG_CACHE[key] or ""


def stored(name):
    """The value saved in the interface, or None. Never for a server-only option."""
    if name in SERVER_ONLY:
        return None
    found = saved_row(PREFIX + name)
    return None if _blank(found) else found


def server_default(name):
    """The value from the server's configuration (.env or the ini file), whatever the interface has saved."""
    key = PREFIX + name
    # A server-only option is never in the database (CKAN does not apply it either): the configuration holds it.
    found = (toolkit.config.get(key) or "") if name in SERVER_ONLY else _server(key)
    return "" if _blank(found) else found


def value(name):
    """The value in use: a value saved in the interface goes before the server's, then the code's default."""
    found = stored(name)
    if found is None:
        found = server_default(name)
    return str(found or DEFAULTS.get(name, "")).strip()


def current():
    """Every setting's value in use, as a dict keyed by the names without prefix."""
    with reading():
        return {name: value(name) for name in NAMES}


# --- validators for update_config_schema -------------------------------------------------------------------------------

def _model(value):
    value = (value or "").strip()
    if not _MODEL.match(value):
        raise toolkit.Invalid(_("Write the model as service/name, without spaces."))
    return value


def _key_env(value):
    from ckanext.ai_connector.keys import allowed_key_envs

    if value not in allowed_key_envs():
        raise toolkit.Invalid(_("Choose one of the allowed keys."))
    return value


def _seconds(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise toolkit.Invalid(_("The AI timeout must be a number of seconds."))
    if not 0 < number <= MAX_SECONDS:
        raise toolkit.Invalid(_("Give a number of seconds between 1 and 600."))
    return str(value).strip()


VALIDATORS = {"model": _model, "api_key_env": _key_env, "timeout_seconds": _seconds, "page_read_timeout_seconds": _seconds}


def update_config_schema(schema):
    ignore_missing = toolkit.get_validator("ignore_missing")
    unicode_safe = toolkit.get_validator("unicode_safe")
    for name, check in VALIDATORS.items():
        schema[PREFIX + name] = [ignore_missing, check] if name.endswith("_seconds") else [ignore_missing, unicode_safe, check]
    return schema


def same_as_server(name, submitted):
    """True when a submitted value is what the server's configuration says."""
    default = server_default(name) or DEFAULTS.get(name, "")
    if name == "model" and not str(submitted).strip():
        return True   # an empty model field sets nothing: the server's model applies (and a key can be saved without one)
    if name.endswith("_seconds"):
        try:
            return float(submitted) == float(default)
        except (TypeError, ValueError):
            return False
    return str(submitted).strip() == str(default).strip()


def save(context, submitted):
    """Save the values that differ from the server's configuration; a value set back to the server's is no longer
    overridden. Raises ValidationError with the setting names (without prefix) as keys."""
    changed = {PREFIX + name: v for name, v in submitted.items() if not same_as_server(name, v)}
    try:
        if changed:
            toolkit.get_action("config_option_update")(context, changed)
    except toolkit.ValidationError as error:
        raise toolkit.ValidationError({key.replace(PREFIX, ""): v for key, v in error.error_dict.items()})
    finally:
        forget_rows()
    back = [name for name in submitted if PREFIX + name not in changed and stored(name) is not None]
    if back:
        reset(back)


def overview(names=FIELDS):
    """What the settings page shows: per setting the value in use, the server's value and whether the interface has
    overridden it."""
    return [{"name": name, "value": value(name), "default": server_default(name), "saved": stored(name) is not None}
            for name in names]


# The model and the key variable go together: the key is sent to the model's service.
PAIRED = ("model", "api_key_env")


def resettable():
    """The settings saved in the interface that the server's configuration also has a value for: the only ones the page
    offers to set back, since setting back one the server has no value for would just empty it (a model, and the AI stops).
    The model and the key variable only as a pair, when the server has both: setting back one of them alone could join a
    model of one service with the key of another, and send that key there."""
    rows = {row["name"]: row for row in overview()}
    names = [name for name, row in rows.items() if row["saved"] and row["default"]]
    if any(name in names for name in PAIRED) and not all(rows[name]["default"] for name in PAIRED):
        names = [name for name in names if name not in PAIRED]
    return names


def reset(names=FIELDS):
    """Forget the values saved in the interface; the server's apply again."""
    from ckan.lib import app_globals

    for name in names:
        delete_row(PREFIX + name)
    model.set_system_info("ckan.config_update", str(time.time()))
    app_globals.reset()
