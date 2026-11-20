"""The AI settings page, for sysadmins: the connection every extension that uses AI shares.

Save stores the values with CKAN's config_option_update, so they apply at once without a restart; Test tries the values in
the form without saving them; Reset goes back to the server's configuration. The page never shows a key, only which
variable holds it, whether it is set and, for a long key, its last four characters.
"""

from ckan.plugins import toolkit
from flask import Blueprint

from ckanext.ai_connector import checks, keys, settings
from ckanext.ai_connector.localization import _

blueprint = Blueprint("ai_connector", __name__)


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
    })
