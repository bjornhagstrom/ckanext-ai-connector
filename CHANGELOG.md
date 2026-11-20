# Changelog

All notable changes to this project will be documented in this file. The format
is based on Keep a Changelog, and the project follows Semantic Versioning.

## [0.1.0] - 2026-11-20

### Added

- One AI connection that CKAN extensions share: the settings page (`/ckan-admin/ai`, *AI settings*) with model, key and
  time limits, keys entered on the page (encrypted, a versioned token) and keys for other services, *Test the connection*,
  setup checks, and the call through LiteLLM (a second attempt after a transient failure, no temperature for a model that
  refuses one, nothing but the consumer, the error's type and status in the log).
- The API for other extensions, `ckanext.ai_connector.api` (`API_VERSION = 1`, may change until 1.0): `unavailable_reason`,
  `is_ready`, `describe`, `client(consumer)` with `text`, `structured` (checked against the schema) and the experimental
  `read_page`, `AIResult`, and the errors `AIError` (with `status_code`, `retryable` and `error_type`, raised `from None` after a failed call), `AIConfigurationError`,
  `AIResponseError`.
- `ckanext.ai_connector.testing`: a fake client and the `ai_connector_fake` fixture, to test a consumer without a key. The
  fixture also makes `unavailable_reason`, `is_ready` and `describe` say the connection is ready, so a consumer that asks
  first (as the README's example does) runs through it.
- `read_page` raises `AIConfigurationError`, before anything is sent, for an address that is not public https (every
  failure of a call is in the `AIError` family); a stored key that cannot be read turns the connection off (the server's
  variable does not stand in for a key chosen on the page); a structured answer is always checked against its schema.
- Actions `ai_connector_test`, `ai_connector_reset` and `ai_connector_status` (sysadmins) and the command `ckan ai-connector doctor`.
  A failed *Test the connection* says what the service's answer means (`hint`): a key that was not accepted (401, 403),
  an unknown model (404), no answer within the time limit (408, 504), the service overloaded or down (500, 502, 503), or
  no more calls for now (429).
- The settings page: the model (a list of the models of the chosen key's service, with a suggested one, or any model
  written as LiteLLM writes it), the API key chosen among the keys there are, the time limits, and *Manage API keys*, a
  fold that lists the keys (entered on the page or set on the server) and adds, replaces or removes one, with the
  service chosen in a list (Google Gemini, Anthropic, OpenAI, Azure OpenAI, Mistral) or *Other service…* under a name of
  one's own. The first time, with no key anywhere, the page starts with adding one. *Use the server's values* asks first
  and sets back only what the server has a value for, the model and the key only together.
- A known service's key is only ever sent to that service's models (the service decided as LiteLLM routes the model),
  and a key of one's own never to a known service (not with an AI server of one's own): the connection refuses before a
  call, the page refuses to save such a pair, and the setup check reports it. A key written as `os.environ/NAME` is
  refused.
- Continuous integration on CKAN 2.11.1, the newest 2.11 and 2.12; `SECURITY.md`, `CONTRIBUTING.md`, `AI_ASSISTANCE.md`.
