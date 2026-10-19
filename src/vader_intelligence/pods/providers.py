"""Model provider interface. Paid model calls are disabled in this release.

`ScriptedProvider` is a deterministic double that answers only from its script and never
opens a connection. `UnwiredVendorProvider` stands in for any real vendor client: it
always raises `PaidCallsDisabled`, so a real integration is recorded as unexercised
rather than silently called.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Protocol

from .contracts import content_hash, normalize_fields, require


class PaidCallsDisabled(RuntimeError):
    """Raised by every real-vendor path: no paid model call can be made."""


class UnscriptedRequest(LookupError):
    pass


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    prompt: str
    max_tokens: int = 256
    temperature: float = 0.0
    seed: int = 0

    def __post_init__(self):
        normalize_fields(self)
        require(0 < self.max_tokens <= 32768, "max_tokens out of range")
        require(0.0 <= self.temperature <= 2.0, "temperature out of range")

    def request_hash(self) -> str:
        return content_hash(asdict(self))


@dataclass(frozen=True)
class ProviderResponse:
    provider: str
    model: str
    text: str
    request_hash: str
    cost_usd: float = 0.0


class ModelProvider(Protocol):
    name: str

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...


class UnwiredVendorProvider:
    """Placeholder for a real vendor client; every call raises PaidCallsDisabled."""

    def __init__(self, vendor: str):
        self.name = vendor

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        raise PaidCallsDisabled(
            f"paid model calls are disabled; vendor {self.name!r} is recorded as unexercised"
        )


class ScriptedProvider:
    """Deterministic double: maps prompts to fixed responses and records every request."""

    name = "scripted"

    def __init__(self, responses: Mapping[str, str]):
        self._responses = dict(responses)
        self.calls: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        require(isinstance(request, ProviderRequest), "request must be a ProviderRequest")
        self.calls.append(request)
        try:
            text = self._responses[request.prompt]
        except KeyError:
            raise UnscriptedRequest("no scripted response for this prompt") from None
        return ProviderResponse(self.name, request.model, text, request.request_hash())
