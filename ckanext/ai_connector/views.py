"""The AI settings page, for sysadmins: the connection every extension that uses AI shares.

Save stores the values with CKAN's config_option_update, so they apply at once without a restart; Test tries the values in
the form without saving them; Reset goes back to the server's configuration. The page never shows a key, only which
variable holds it, whether it is set and, for a long key, its last four characters.
"""

from ckan.plugins import toolkit
from flask import Blueprint

from ckanext.ai_connector import checks, keys, purposes, settings
from ckanext.ai_connector.localization import _

blueprint = Blueprint("ai_connector", __name__)


def _purpose_rows():
    """The purposes the page offers a model for: every declared one, and every stored choice (also one that no installed
    extension declares any more, so that it can be removed). Sorted by extension, then by label."""
    from ckanext.ai_connector.connection import ConnectionSettings

    stored = {(row["consumer"], row["purpose"]): row for row in purposes.saved()}
    names = set(stored)
    for consumer, declared in purposes.declared().items():
        names.update((consumer, purpose) for purpose in declared)
    main_model = ""
    try:
        main_model = ConnectionSettings.in_use().model
    except Exception:
        pass
    rows = []
    for consumer, purpose in names:
        row = stored.get((consumer, purpose)) or {}
        rows.append({"consumer": consumer, "purpose": purpose, "id": "%s.%s" % (consumer, purpose),
                     "label": purposes.label(consumer, purpose),
                     "declared": purpose in purposes.declared().get(consumer, {}),
                     "model": row.get("model", ""), "api_key_env": row.get("api_key_env", ""),
                     "broken": bool(row.get("broken")), "main_model": main_model,
                     # The saved key is the one the model's service would get anyway: the form shows "the model's
                     # service's key", so that changing the model to another service changes the key with it.
                     "key_follows_model": bool(row.get("model")) and row.get("api_key_env") == _purpose_key(row.get("model", ""), "")})
    return sorted(rows, key=lambda r: (r["consumer"], str(r["label"]).lower()))


def _purpose_key(model, chosen):
    """The key for a purpose's model: the one chosen in the form, else the model's own service's key, else (with an AI
    server of one's own) the main connection's key, else "" (there is none: the row says so, and nothing is sent). Save and
    Test both use this, so a test never falls back on a key that was not chosen for the row."""
    if chosen:
        return chosen
    if not model:
        return ""
    if settings.value("api_base"):
        # Every call goes to the AI server of one's own: the main connection's key is the one meant for it, not a known
        # service's key that happens to be set (it would be sent to that server).
        return settings.value("api_key_env")
    from ckanext.ai_connector.connection import routed_service

    return keys.PROVIDER_KEYS.get(routed_service(model), "")


def _key_service(name):
    """The known service a key variable belongs to, or "" (a key under a name of its own, or another name the server
    allows)."""
    return next((service for service, env in keys.PROVIDER_KEYS.items() if env == name), "")


def _context():
    user = toolkit.current_user
    return {"user": user.name if user.is_authenticated else ""}


@blueprint.route("/ckan-admin/ai", methods=["GET", "POST"], strict_slashes=False)
def settings_page():
    context = _context()
    try:
        toolkit.check_access("ai_connector_settings", context, {})
    except toolkit.NotAuthorized:
        return toolkit.abort(403, _("Not authorized."))

    here = "ai_connector.settings_page"
    form = {row["name"]: row["value"] for row in settings.overview()}
    errors, test, confirm_reset = {}, None, False
    if toolkit.request.method == "POST":
        submitted = {name: (toolkit.request.form.get(name) or "").strip() for name in settings.FIELDS}
        step = toolkit.request.form.get("step")
        if step == "save_purposes" and toolkit.request.form.get("purpose_test"):
            step = "test_purpose"   # the Test button of a row in the purposes' form
        if step == "remove_key":
            name = toolkit.request.form.get("key_name") or ""
            if keys.is_custom_key(name):
                try:
                    keys.remove_custom_key(name, user=context["user"])
                except toolkit.ValidationError as error:
                    errors = error.error_dict
                else:
                    toolkit.h.flash_success(_("The key and its name are removed."))
                    return toolkit.redirect_to(here)
            else:
                keys.remove_key(name)
                toolkit.h.flash_success(_("The key entered here is removed."))
                return toolkit.redirect_to(here)
        if step == "add_custom_key":
            if not toolkit.request.form.get("custom_confirm"):
                errors = {"custom_confirm": [_("Tick the box to confirm.")]}
            else:
                try:
                    # A service chosen in the list is its variable; "Other service…" takes the name written beside it.
                    service = toolkit.request.form.get("custom_service") or "other"
                    written = (toolkit.request.form.get("custom_name") or "").strip()
                    if service != "other" and written:
                        # Both fields show without the script: a name written beside a chosen service would be ignored.
                        raise toolkit.ValidationError({"custom_name": [
                            _("A name of your own is only for “Other service…”. Choose that, or empty the name.")]})
                    name = written if service == "other" else service
                    keys.add_custom_key(name, toolkit.request.form.get("custom_key"), user=context["user"])
                except toolkit.ValidationError as error:
                    errors = error.error_dict
                else:
                    if service == "other":
                        toolkit.h.flash_success(_("The key is added. To use it, choose it under “API key” and write the model, then test the connection and save."))
                    else:
                        toolkit.h.flash_success(_("The key is saved. To use it, choose a model of its service above, then test the connection and save."))
                    return toolkit.redirect_to(here)   # at the top: the confirmation, and the model to choose next
        if step == "save_purposes":
            failed, changes = {}, []
            for row in _purpose_rows():
                model_name = (toolkit.request.form.get("purpose_model." + row["id"]) or "").strip()
                key_name = _purpose_key(model_name, (toolkit.request.form.get("purpose_key." + row["id"]) or "").strip())
                if model_name == row["model"] and key_name == row["api_key_env"] and not row["broken"]:
                    continue   # unchanged (compared as resolved: "the model's service's key" is a key too)
                try:
                    purposes.row_key(row["consumer"], row["purpose"])   # a stored row with a name that cannot be saved
                except purposes.PurposeError as error:
                    failed["purpose." + row["id"]] = [str(error)]
                    continue
                if model_name and not key_name:
                    failed["purpose." + row["id"]] = [_("Choose the API key for this model.")]
                    continue
                if model_name:
                    # Checked for every row before anything is saved: either all the rows are saved, or none.
                    from ckanext.ai_connector.connection import pairing_error

                    try:
                        settings.VALIDATORS["model"](model_name)
                        settings.VALIDATORS["api_key_env"](key_name)
                    except toolkit.Invalid as error:
                        failed["purpose." + row["id"]] = [error.error]
                        continue
                    mismatch = pairing_error(model_name, key_name, settings.value("api_base"))
                    if mismatch:
                        failed["purpose." + row["id"]] = [mismatch]
                        continue
                changes.append((row, model_name, key_name))
            for row, model_name, key_name in ([] if failed else changes):
                try:
                    purposes.save(row["consumer"], row["purpose"], model_name, key_name)
                except (toolkit.Invalid, purposes.PurposeError) as error:
                    failed["purpose." + row["id"]] = [getattr(error, "error", None) or str(error)]
            if failed:
                errors = failed
                step = "none"
            else:
                toolkit.h.flash_success(_("The models for the purposes are saved and apply now."))
                return toolkit.redirect_to(here)
        if step == "test_purpose":
            row_id = toolkit.request.form.get("purpose_test") or ""
            row = next((r for r in _purpose_rows() if r["id"] == row_id), None)
            model_name = (toolkit.request.form.get("purpose_model." + row_id) or "").strip() if row else ""
            key_name = _purpose_key(model_name, (toolkit.request.form.get("purpose_key." + row_id) or "").strip())
            if row and model_name and not key_name:
                # No key for this model's service: say so, and send nothing (never the main connection's key instead).
                errors = {"purpose." + row_id: [_("Choose the API key for this model.")]}
            elif row and model_name:
                try:
                    test = toolkit.get_action("ai_connector_test")(context, {"model": model_name, "api_key_env": key_name})
                    test["purpose"] = row["label"]
                except toolkit.ValidationError as error:
                    errors = {"purpose." + row_id: [v for values in error.error_dict.values() for v in values]}
            elif row:
                errors = {"purpose." + row_id: [_("Write or choose a model to test, or leave it empty to use the main model.")]}
            step = "none"
        if step == "reset":
            # A first click changes nothing: the page shows what goes and what replaces it, and asks again.
            confirm_reset = True
            step = "none"
        if step == "reset_confirmed":
            settings.reset(settings.resettable())
            toolkit.h.flash_success(_("The AI settings from the server's configuration apply again."))
            return toolkit.redirect_to(here)
        if step in ("remove_key", "add_custom_key"):
            step = "none"   # the error is shown with the form as it was typed
        if step != "none":
            form.update(submitted)   # after an error in the key forms the page keeps what is saved: those forms send no connection fields
        if step == "none":
            pass
        elif step == "test":
            try:
                test = toolkit.get_action("ai_connector_test")(context, dict(submitted))
            except toolkit.ValidationError as error:
                errors = error.error_dict
        else:
            try:
                # Keys are added, replaced and removed under "API keys"; the connection only chooses one of them, and
                # never one of another service than the model (the key would be sent there).
                from ckanext.ai_connector.connection import pairing_error

                mismatch = pairing_error(submitted["model"] or settings.value("model"),
                                         submitted["api_key_env"] or settings.value("api_key_env"),
                                         settings.value("api_base"))
                if mismatch:
                    raise toolkit.ValidationError({"api_key_env": [mismatch]})
                settings.save(context, submitted)
            except toolkit.ValidationError as error:
                errors = error.error_dict
            else:
                toolkit.h.flash_success(_("The AI settings are saved and apply now."))
                return toolkit.redirect_to(here)

    rows = settings.overview()
    suggestions = keys.model_suggestions()
    key_rows = keys.key_status()
    for row in key_rows:
        row["service"] = _key_service(row["name"])   # for the purposes' rows: the key follows the model's service
    if not form.get("api_key_env"):
        # No key variable chosen anywhere (none saved here, none on the server) but a key is there: the page names it and
        # suggests a model of its service. With a model already chosen, only the key of that model's service: never one
        # that would be sent to another service.
        is_set = {row["name"] for row in key_rows if row.get("set")}
        if form.get("model"):
            wanted = keys.PROVIDER_KEYS.get(form["model"].split("/")[0])
            form["api_key_env"] = wanted if wanted in is_set else ""
        else:
            form["api_key_env"] = next((row["name"] for row in key_rows if row.get("set")), "")
    # No model yet: suggest one of the services whose key is set (the field shows it; nothing is saved until Save).
    recommended = "" if form.get("model") else keys.recommended_model(suggestions, form.get("api_key_env") or "")
    # The first time: no key anywhere and no AI server of one's own, so there is nothing to list models for yet.
    first_time = not any(row.get("set") for row in key_rows) and not settings.value("api_base")
    return toolkit.render("ai_connector/settings.html", {
        "checks": checks.run_checks(),
        "resettable": settings.resettable(),
        "confirm_reset": confirm_reset,
        "form": form,
        "errors": errors,
        "test": test,
        "rows": rows,
        "keys": key_rows,
        "key_services": keys.key_services(),
        "service_labels": keys.SERVICE_LABELS,
        # A suggested model per service with a key, for when the API key is changed on the page.
        "recommended_by_provider": {provider: keys.recommended_model(suggestions, env)
                                    for provider, env in keys.PROVIDER_KEYS.items()
                                    if keys.recommended_model(suggestions, env)},
        "first_time": first_time,
        "can_store_keys": keys.can_store_keys(),
        "suggestions": suggestions,
        "recommended": recommended,
        "provider_keys": keys.PROVIDER_KEYS,
        "library_version": keys.library_version(),
        "api_base": settings.value("api_base"),
        "purpose_rows": _purpose_rows(),
        "purpose_errors": any(name.startswith("purpose.") for name in errors),
        "submitted_purposes": dict(toolkit.request.form) if toolkit.request.method == "POST" else {},
    })
