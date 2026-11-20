# Security policy

## Supported versions

Security fixes are made only on the latest release. The extension is in development (0.x) and has not received a formal
security audit.

## What the extension protects

It stores API keys for AI services. A key entered on the settings page is encrypted with a key derived from the portal's
`SECRET_KEY` and is never shown again (only its last four characters, and none for a short key). Which environment
variable a key may be read from is limited to an allowlist set on the server, and the address of an own AI server can be
set only on the server, so that a sysadmin page cannot send a key, or another secret, somewhere else. A known service's
key goes only to that service's models.

The trust boundaries, by design:

- **The server's operator** decides where keys go. With an own AI server set (`ckanext.ai_connector.api_base`), the check
  that a key goes only to its own service is off, and the chosen key is sent with every call to the address that is set.
- **Installed extensions** run in CKAN's process with its rights. The extension cannot stop one from calling the
  connection, or from reading a key itself.
- **Sysadmins** can choose the model and the key, and add and remove keys, but cannot read a stored key back.

Reports about any way around these are especially welcome.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's **Report a vulnerability** function in the
repository's Security tab. The maintainer will acknowledge the report, assess impact and coordinate a fix and disclosure.
If private vulnerability reporting is temporarily unavailable, open a public issue containing no sensitive detail and ask
the maintainer for a private contact channel.

Do not include live credentials, API keys, personal data or data copied from a production CKAN installation in a report.
