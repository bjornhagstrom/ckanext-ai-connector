"""The call to the AI service, through LiteLLM: one connection, any model LiteLLM supports.

What an extension asks the AI for (its prompts and the shape of the answer) stays in that extension. This module owns how the
call is made: the key, the address of an own server, time limits, a second attempt after a transient failure, a model that
refuses a temperature, and how a model reads a web page. Nothing here logs a key, a prompt or an answer: a failure is logged
with the consumer's name, its type and status code only.

The client builds a neutral request (:class:`Call`) and hands it to a transport. The transport for real calls is LiteLLM's
``completion``; :mod:`ckanext.ai_connector.testing` hands it to a fake one, so that a consumer's tests run the same client code
without a key or a network.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from ckanext.ai_connector.localization import _

log = logging.getLogger(__name__)

# Status codes and error names that are worth one more attempt.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_RETRYABLE_NAMES = ("timeout", "ratelimit", "serviceunavailable")
# The one capability a model may have: reading a public web page itself, during the call.
PAGE_READ = "page_read"
# Which services' models can read a page, and how (an internal detail of read_page()).
_PAGE_READERS = ("gemini", "anthropic")
# The most page text Claude's fetch puts in the call (about 2 500 tokens for a normal page).
_ANTHROPIC_FETCH_MAX_TOKENS = 10000
_CONSUMER = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
MAX_SECONDS = 600


def status_code(error: BaseException | None) -> int | None:
    code = getattr(error, "status_code", None)
    if code is None:
        code = getattr(getattr(error, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def is_retryable(error: BaseException) -> bool:
    """Recognize transient failures without depending on vendor classes."""
    # LiteLLM reports a timed-out call as status_code=408; the name check is the fallback for an error without a code.
    if status_code(error) in RETRYABLE_STATUS:
        return True
    name = type(error).__name__.lower()
    return any(part in name for part in _RETRYABLE_NAMES)


class AIError(RuntimeError):
    """A failure of the AI call, safe to show to the person: the message never holds the key, the prompt or the service's own
    text. ``status_code`` is the service's HTTP status when there was one, and ``retryable`` says whether the failure is
    transient (a rate limit, a timeout, an overloaded service). ``error_type`` is the class name of the underlying error, or
    None. The underlying error itself is not chained (no ``__cause__``): it may hold text from the service, which would
    otherwise end up in any logged traceback."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False,
                 error_type: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.error_type = error_type


class AIConfigurationError(AIError):
    """The connection cannot be used: no model, no key, the plugin or a library missing, or a capability the model lacks."""


class AIResponseError(AIError):
    """The service answered, but not in the requested shape."""


@dataclass(frozen=True)
class AIResult:
    """An answer: ``text`` as the model gave it, ``data`` the parsed JSON for a structured call (else None), the ``model``
    that answered, and ``usage`` (token counts as the service reported them, or None)."""

    text: str
    data: Any = None
    model: str = ""
    usage: dict | None = None


@dataclass
class Call:
    """One request to the model, in neutral form: what the transport sends, and what a fake transport records."""

    kind: str                       # "text", "structured" or "read_page"
    messages: list
    schema: dict | None = None
    name: str = "answer"
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: float = 30.0
    url: str | None = None          # the page the model reads (read_page)
    consumer: str = ""
    model: str = ""


@dataclass
class Reply:
    """What a transport gives back: the text, token usage and, for a page read, whether the page was actually read
    (None when the service does not say)."""

    text: str
    usage: dict | None = None
    page_read: bool | None = None
    model: str = ""
    extra: dict = field(default_factory=dict)


def service_of(model: str) -> str:
    """The service a LiteLLM model name belongs to: the part before the first slash ("gemini/x" -> "gemini")."""
    return model.split("/", 1)[0].lower() if "/" in model else ""


def capabilities_of(model: str) -> frozenset:
    return frozenset({PAGE_READ}) if service_of(model) in _PAGE_READERS else frozenset()


def public_https(url: str) -> bool:
    """True for a public https address: no credentials, no loopback, private or link-local address, no bare host name."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    host = parts.hostname
    if host == "localhost" or host.endswith(".localhost") or "." not in host.strip("[]"):
        return False
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return True
    return address.is_global


class AIClient:
    """A configured connection to one model, for one consumer. Create it with :func:`ckanext.ai_connector.api.client`.

    Public: ``consumer``, ``model``, ``service`` (the part of the model before the slash), ``capabilities``,
    ``timeout_seconds`` and ``page_read_timeout_seconds``, and the methods :meth:`text`, :meth:`structured` and
    :meth:`read_page`. Nothing public holds the key.
    """

    def __init__(self, consumer: str, model: str, transport: Callable[[Call], Reply], *, timeout_seconds: float = 30.0,
                 page_read_timeout_seconds: float = 180.0, max_attempts: int = 2, retry_delay_seconds: float = 1.0,
                 sleep_fn: Callable[[float], None] = time.sleep) -> None:
        if not isinstance(consumer, str) or not _CONSUMER.match(consumer):
            raise ValueError("consumer must be a short lower-case name, such as 'my_extension'")
        if not (model or "").strip():
            raise AIConfigurationError(_("The AI model is missing from the configuration."))
        self.consumer = consumer
        self.model = model
        self.service = service_of(model)
        self.capabilities = capabilities_of(model)
        self.timeout_seconds = _clamp(timeout_seconds)
        self.page_read_timeout_seconds = max(self.timeout_seconds, _clamp(page_read_timeout_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self._retry_delay = max(0.0, float(retry_delay_seconds))
        self._transport = transport
        self._sleep = sleep_fn

    def __repr__(self) -> str:   # never the key
        return "%s(consumer=%r, model=%r)" % (type(self).__name__, self.consumer, self.model)

    # --- the three kinds of call -------------------------------------------------------------------------------------

    def text(self, messages: list[dict[str, str]], *, temperature: float | None = None, max_tokens: int | None = None,
             timeout: float | None = None) -> AIResult:
        """A plain answer in text."""
        reply = self._send(Call("text", messages, temperature=temperature, max_tokens=max_tokens,
                                timeout=self._timeout(timeout)))
        return AIResult(text=reply.text, model=reply.model or self.model, usage=reply.usage)

    def structured(self, messages: list[dict[str, str]], schema: dict[str, Any], *, name: str = "answer",
                   temperature: float | None = None, max_tokens: int | None = None,
                   timeout: float | None = None) -> AIResult:
        """An answer in the shape of ``schema`` (a JSON schema, sent to the service as a strict response format). ``data`` is
        the parsed answer, checked against the schema. Raises AIResponseError when it is not JSON or not in that shape."""
        reply = self._send(Call("structured", messages, schema=schema, name=name, temperature=temperature,
                                max_tokens=max_tokens, timeout=self._timeout(timeout)))
        return AIResult(text=reply.text, data=_parsed(reply.text, schema), model=reply.model or self.model,
                        usage=reply.usage)

    def read_page(self, url: str, messages: list[dict[str, str]], *, schema: dict[str, Any] | None = None,
                  name: str = "answer", temperature: float | None = None, max_tokens: int | None = None) -> AIResult:
        """Experimental (outside the 1.0 promise until marked stable). The model reads the public page at ``url`` itself,
        during the call, and answers ``messages`` (which must name the address) in text or, with ``schema``, in that shape.
        The time limit is the one for reading a page. Raises AIConfigurationError, before anything is sent, when the model
        cannot read pages (``capabilities`` lacks ``page_read``) or the address is not a public https address (no
        credentials, no private, loopback or bare host name), and AIError when the service answered without having read the
        page. ValueError when ``messages`` do not name the address (a mistake in the calling code)."""
        if PAGE_READ not in self.capabilities:
            raise AIConfigurationError(_("The chosen AI model cannot read web pages."))
        if not public_https(url):
            raise AIConfigurationError(_("Only a public https address can be read by the AI service."))
        if not any(url in str(message.get("content") or "") for message in messages):
            raise ValueError("read_page(): the messages must name the address to read")
        reply = self._send(Call("read_page", messages, schema=schema, name=name, temperature=temperature,
                                max_tokens=max_tokens, timeout=self.page_read_timeout_seconds, url=url))
        if reply.page_read is False:
            log.warning("%s: the AI service answered without reading the page", self.consumer)
            raise AIError(_("The page could not be read."))
        data = _parsed(reply.text, schema) if schema is not None else None
        return AIResult(text=reply.text, data=data, model=reply.model or self.model, usage=reply.usage)

    # --- the call itself ---------------------------------------------------------------------------------------------

    def _timeout(self, timeout: float | None) -> float:
        return _clamp(timeout) if timeout else self.timeout_seconds

    def _send(self, call: Call) -> Reply:
        call.consumer, call.model = self.consumer, self.model
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._transport(call)
            except AIError:
                raise
            except AssertionError:
                # A test's assertion inside the fake transport (ckanext.ai_connector.testing) goes to the test as it is; from a
                # real transport it is a failure of the call like any other.
                if getattr(self._transport, "test_double", False):
                    raise
                error = AIError(_("The AI service could not answer."))
                log.warning("AI call failed: consumer=%s kind=%s attempt=%d/%d error_type=AssertionError",
                            self.consumer, call.kind, attempt, self.max_attempts)
                raise error from None
            except Exception as error:
                retryable = is_retryable(error)
                # Deliberately omit the exception's text, the request and the key.
                log.warning("AI call failed: consumer=%s kind=%s attempt=%d/%d error_type=%s status_code=%s retryable=%s",
                            self.consumer, call.kind, attempt, self.max_attempts, type(error).__name__,
                            status_code(error), retryable)
                if attempt < self.max_attempts and retryable:
                    self._sleep(self._retry_delay)
                    continue
                raise AIError(_("The AI service could not answer."), status_code=status_code(error),
                              retryable=retryable, error_type=type(error).__name__) from None
        raise AIError(_("The AI service could not answer."))   # not reached


def _clamp(seconds: float | None) -> float:
    try:
        number = float(seconds)
    except (TypeError, ValueError):
        return 30.0
    return min(max(number, 1.0), float(MAX_SECONDS))


def _parsed(text: str, schema: dict | None) -> Any:
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError) as error:
        raise AIResponseError(_("The AI service gave an answer in the wrong shape."),
                              error_type=type(error).__name__) from None
    if schema is not None:
        import jsonschema   # a dependency: an answer is never left unchecked

        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError:
            # The validation error quotes the answer: not chained, like the service's own errors.
            raise AIResponseError(_("The AI service gave an answer in the wrong shape."),
                                  error_type="ValidationError") from None
        except jsonschema.SchemaError as error:
            raise AIConfigurationError(_("The requested shape of the answer is not a valid JSON schema.")) from error
    return data


# --- the transport for real calls: LiteLLM -----------------------------------------------------------------------------

def litellm_transport(api_key: str, api_base: str | None = None, completion_fn: Callable[..., Any] | None = None):
    """The transport that calls LiteLLM's ``completion`` with the key. Raises AIConfigurationError when LiteLLM is missing."""
    if completion_fn is None:
        try:
            from litellm import completion
        except ImportError as error:
            raise AIConfigurationError(_("LiteLLM is not installed. Install ckanext-ai-connector again with its dependencies.")) from error
        completion_fn = completion

    def complete(**kwargs: Any) -> Any:
        """One call. A model that fixes its own sampling (the newer Claude models accept only temperature 1) refuses our
        temperature with LiteLLM's ``UnsupportedParamsError``: ask again without it."""
        try:
            return completion_fn(**kwargs)
        except Exception as error:
            if "temperature" in kwargs and type(error).__name__ == "UnsupportedParamsError" and "temperature" in str(error):
                kwargs.pop("temperature")
                return completion_fn(**kwargs)
            raise

    def transport(call: Call) -> Reply:
        kwargs: dict[str, Any] = {"model": call.model, "api_key": api_key, "messages": call.messages, "timeout": call.timeout}
        if call.schema is not None:
            kwargs["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": call.name, "schema": call.schema, "strict": True}}
        if call.temperature is not None:
            kwargs["temperature"] = call.temperature
        if call.max_tokens:
            kwargs["max_tokens"] = call.max_tokens
        anthropic = service_of(call.model) == "anthropic"
        if call.kind == "read_page":
            kwargs["tools"] = [{"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 1,
                                "max_content_tokens": _ANTHROPIC_FETCH_MAX_TOKENS} if anthropic else {"url_context": {}}]
        if api_base:
            kwargs["api_base"] = api_base
        response = complete(**kwargs)
        message = response.choices[0].message
        page_read = _anthropic_fetched(message) if (call.kind == "read_page" and anthropic) else None
        usage = getattr(response, "usage", None)
        if usage is not None and not isinstance(usage, dict):
            usage = {k: getattr(usage, k, None) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        return Reply(text=message.content or "", usage=usage, page_read=page_read, model=getattr(response, "model", "") or "")

    return transport


def _anthropic_fetched(message: Any) -> bool:
    """Claude answers even when its fetch failed (it then says so in the fields), so the answer is worth something only when
    the fetch result in the response is a page. Anthropic's error codes (``unavailable``, ``url_not_allowed`` ...) go to the
    log, not the person."""
    fields = getattr(message, "provider_specific_fields", None) or {}
    results = [item.get("content") or {} for item in (fields.get("web_search_results") or [])
               if isinstance(item, dict) and item.get("type") == "web_fetch_tool_result"]
    if any(result.get("type") == "web_fetch_result" for result in results):
        return True
    codes = [str(result.get("error_code")) for result in results if result.get("type") == "web_fetch_tool_result_error"]
    log.warning("The AI service's page fetch gave no page: %s", ",".join(codes) or "no fetch result")
    return False
