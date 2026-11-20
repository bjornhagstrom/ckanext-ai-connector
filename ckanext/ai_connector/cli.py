"""Command line: ``ckan -c $CKAN_INI ai-connector doctor``."""

import click


@click.group("ai-connector", short_help="AI settings commands.")
def ai_connector():
    pass


@ai_connector.command("doctor", short_help="Check what the AI connection needs and say what is missing.")
def doctor():
    """List the setup checks (plugin, library, model, key). Calls no AI service and prints no key."""
    from ckanext.ai_connector import checks

    marks = {checks.OK: "ok     ", checks.WARN: "warning", checks.ERROR: "ERROR  "}
    failed = False
    for check in checks.run_checks():
        click.echo("%s %s: %s" % (marks[check["status"]], check["title"], check["detail"]))
        if check["fix"]:
            click.echo("        -> %s" % check["fix"])
        failed = failed or check["status"] == checks.ERROR
    if failed:
        raise click.exceptions.Exit(1)


def get_commands():
    return [ai_connector]
