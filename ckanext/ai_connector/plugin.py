import os

import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit

from ckanext.ai_connector import actions, cli, settings, views


@toolkit.blanket.config_declarations
class AIConnectorPlugin(plugins.SingletonPlugin):
    """The AI connection (service, model, key, time limits) that other extensions share. See README and api.py."""

    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IBlueprint)
    plugins.implements(plugins.IActions)
    plugins.implements(plugins.IAuthFunctions)
    plugins.implements(plugins.ITranslation)
    plugins.implements(plugins.IClick)

    # IConfigurer
    def update_config(self, config):
        toolkit.add_template_directory(config, "templates")
        toolkit.add_resource("assets", "ai_connector")

    def update_config_schema(self, schema):
        return settings.update_config_schema(schema)

    # IBlueprint
    def get_blueprint(self):
        return views.blueprint

    # IActions, IAuthFunctions
    def get_actions(self):
        return actions.get_actions()

    def get_auth_functions(self):
        return {"ai_connector_settings": actions.ai_settings_auth}

    # IClick
    def get_commands(self):
        return cli.get_commands()

    # ITranslation. English is the source language; the Swedish catalogues are compiled from i18n/sv and i18n/sv_SE.
    def i18n_directory(self):
        return os.path.join(os.path.dirname(__file__), "i18n")

    def i18n_locales(self):
        directory = self.i18n_directory()
        return sorted(
            locale for locale in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, locale, "LC_MESSAGES", self.i18n_domain() + ".mo")))

    def i18n_domain(self):
        return "ckanext-ai-connector"
