# Contributing

Thank you for helping improve ckanext-ai-connector. The extension is in development (0.x), so bug reports, compatibility
results (CKAN version, AI service and model) and feedback on the API from extensions that use it are especially useful.

## Before opening an issue

- search existing issues;
- include the CKAN, Python and LiteLLM versions, and the model (`service/name`) used;
- describe the expected and actual result;
- include reproducible steps and relevant logs with keys, credentials and personal data removed.

Use a private security report instead of a public issue for vulnerabilities; see [SECURITY.md](SECURITY.md).

## Pull requests

1. Discuss changes to the API (`ckanext/ai_connector/api.py`) in an issue first: other extensions depend on it.
2. Keep each pull request focused.
3. Add or update tests when changing behavior. Run the suite as the README's "Development" section shows; continuous
   integration runs it on CKAN 2.11.1, the newest 2.11 patch and 2.12.
4. Update the README and changelog when user-visible behavior changes.
5. Internationalize all new user-visible strings: wrap them in `_(...)` with English source text, then add the Swedish
   `msgstr` to `ckanext/ai_connector/i18n/sv/LC_MESSAGES/ckanext-ai-connector.po` and `sv_SE`, and compile the `.mo` files.
   The tests fail when an extracted string has no translation or a `.mo` does not match its `.po`.
6. Never log, print or return a key, a prompt or an answer from the AI service; log failures by type and status code.
7. Disclose material generative-AI assistance in the pull request description (see [AI_ASSISTANCE.md](AI_ASSISTANCE.md)).

## Translations

A new language is welcome as a pull request: add `ckanext/ai_connector/i18n/<locale>/LC_MESSAGES/ckanext-ai-connector.po` and
its compiled `.mo`. No code change is needed; the plugin offers every compiled catalogue.
