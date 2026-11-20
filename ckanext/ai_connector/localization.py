"""Interface texts: English source, translated by CKAN when it can.

The client also runs without a CKAN request (unit tests, the CLI, a background job), so translation is looked up lazily
and falls back to the English text. Babel extracts every ``_("...")`` call; see babel.cfg and ``i18n/``.
"""


def _(message: str) -> str:
    try:
        from ckan.plugins.toolkit import _ as translate

        return translate(message)
    except Exception:
        return message
