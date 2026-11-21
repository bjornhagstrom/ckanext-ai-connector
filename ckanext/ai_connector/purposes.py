"""Models for different purposes (API version 2).

A consumer names what it uses the AI for (``api.declare_purposes("my_extension", {"translate": _("Translation")})``) and
asks for a client for one of them (``api.client("my_extension", purpose="translate")``). A sysadmin may choose, on the AI
settings page, another model and key for that purpose; without a choice the purpose uses the main connection. Only the
model and the key are chosen per purpose: the time limits and an own AI server (``api_base``) are the connection's.

A choice is stored as one row in CKAN's ``system_info`` table, ``ckanext.ai_connector.purpose.<consumer>.<purpose>``, holding
``{"model": ..., "api_key_env": ...}``. The key itself stays where every key is (the server's environment, or the page's
encrypted keys), so the rule that a known service's key only goes to that service's models (``pairing_error``) applies to a
purpose's choice as to the main one: it is checked when the choice is saved and again before every call. A choice that can no
longer be used (its key removed, its row unreadable) is refused, not replaced by the main connection: the sysadmin chose a
model for that purpose, and the setup checks say what is wrong.

The declarations are kept per process (each web and job worker declares when its plugins load).
"""

from __future__ import annotations

import json
import re
from typing import Callable, Mapping, Union

from ckanext.ai_connector import settings
from ckanext.ai_connector.localization import _

ROW_PREFIX = settings.PREFIX + "purpose."
_PURPOSE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_CONSUMER = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

Label = Union[str, Callable[[], str]]
_declared: dict[str, dict[str, Label]] = {}
_names: dict[str, Label] = {}


class PurposeError(ValueError):
    """A consumer or purpose name that cannot be used."""


def check_names(consumer: str, purpose: str) -> None:
    if not isinstance(consumer, str) or not _CONSUMER.match(consumer):
        raise PurposeError(_("A consumer with purposes is named with lower-case letters, digits and _ (2-64 characters)."))
    if not isinstance(purpose, str) or not _PURPOSE.match(purpose):
        raise PurposeError(_("A purpose is named with lower-case letters, digits and _ (1-40 characters)."))


def declare(consumer: str, purposes: Mapping[str, Label], name: Label | None = None) -> None:
    """Make ``purposes`` (name -> label, or a function giving the label in the page's language) known for ``consumer``,
    and ``name``, the extension's own name for the page (API 3). Declaring again adds to or replaces labels; nothing is
    stored."""
    for purpose in purposes:
        row_key(consumer, purpose)   # the names, and that they fit in a row's key
    _declared.setdefault(consumer, {}).update(purposes)
    if name:
        _names[consumer] = name


def declared() -> dict[str, dict[str, Label]]:
    return {consumer: dict(names) for consumer, names in _declared.items()}


def _text(found, fallback: str) -> str:
    try:
        found = found() if callable(found) else found
    except Exception:
        found = None
    return str(found or fallback)


def label(consumer: str, purpose: str) -> str:
    return _text(_declared.get(consumer, {}).get(purpose), purpose)


def consumer_name(consumer: str) -> str:
    """The name an extension gave itself for the page, else its short name (``my_extension``)."""
    return _text(_names.get(consumer), consumer)


def source(consumer: str | None, purpose: str | None) -> str:
    """``"purpose"`` when a model is chosen for the purpose (also one that cannot be used now), else ``"main"``."""
    if not (consumer and purpose):
        return "main"
    try:
        return "purpose" if choice(consumer, purpose) is not None else "main"
    except PurposeError:
        return "main"


# CKAN's system_info.key is a varchar(100).
MAX_KEY_LENGTH = 100


def row_key(consumer: str, purpose: str) -> str:
    check_names(consumer, purpose)
    key = "%s%s.%s" % (ROW_PREFIX, consumer, purpose)
    if len(key) > MAX_KEY_LENGTH:
        raise PurposeError(_("A consumer's name and a purpose together may be at most {count} characters.").format(
            count=MAX_KEY_LENGTH - len(ROW_PREFIX) - 1))
    return key


BROKEN = {"broken": True}


def _parse(raw):
    try:
        found = json.loads(raw)
    except (TypeError, ValueError):
        return dict(BROKEN)
    if not isinstance(found, dict) or not isinstance(found.get("model"), str) or not isinstance(found.get("api_key_env"), str):
        return dict(BROKEN)
    return {"model": found["model"].strip(), "api_key_env": found["api_key_env"].strip()}


def choice(consumer: str, purpose: str) -> dict | None:
    """The model and key chosen for a purpose, ``{"broken": True}`` for a row that cannot be read, or None (the main
    connection applies)."""
    raw = settings.saved_row(row_key(consumer, purpose))
    if raw is None or not str(raw).strip():
        return None
    return _parse(raw)


def saved() -> list[dict]:
    """Every stored choice, declared in this process or not: ``consumer``, ``purpose``, ``label``, ``declared`` and the choice."""
    found = []
    for key, raw in sorted(settings._rows().items()):
        if not key.startswith(ROW_PREFIX) or raw is None or not str(raw).strip():
            continue
        consumer, _dot, purpose = key[len(ROW_PREFIX):].rpartition(".")
        found.append({"consumer": consumer, "purpose": purpose, "label": label(consumer, purpose),
                      "declared": purpose in _declared.get(consumer, {}), **_parse(raw)})
    return found


def save(consumer: str, purpose: str, model: str, api_key_env: str) -> None:
    """Store the model and key for a purpose; an empty model forgets the choice (the main connection applies). Raises
    ``toolkit.Invalid`` with a sentence for the sysadmin when the model or key cannot be used together."""
    from ckan.plugins import toolkit

    from ckanext.ai_connector.connection import pairing_error

    key = row_key(consumer, purpose)
    model = (model or "").strip()
    if not model:
        forget(consumer, purpose)
        return
    model = settings.VALIDATORS["model"](model)
    api_key_env = settings.VALIDATORS["api_key_env"]((api_key_env or "").strip())
    mismatch = pairing_error(model, api_key_env, settings.value("api_base"))
    if mismatch:
        raise toolkit.Invalid(mismatch)
    settings.write_row(key, json.dumps({"model": model, "api_key_env": api_key_env}, sort_keys=True))


def forget(consumer: str, purpose: str) -> None:
    key = row_key(consumer, purpose)
    if settings.saved_row(key) is not None:
        settings.delete_row(key)


def broken_reason(consumer: str, purpose: str) -> str:
    return _("The model chosen for {purpose} cannot be read. Choose it again on the AI settings page.").format(
        purpose=label(consumer, purpose))
