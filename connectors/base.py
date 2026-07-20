"""The house interface every generated connector implements — plus a mock transport.

Two design choices make generated code safe to validate:
  1. **Transport injection** — all HTTP goes through a `Transport`. In production that's
     real httpx; in the validation harness we inject a `MockTransport`, so we can dry-run
     every action with no network and no credentials.
  2. **A machine-readable `spec()`** — each connector declares its actions (name + method +
     path + a realistic `sample_input`). The harness uses that to know what to dry-run.
"""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel


@dataclass
class HttpResponse:
    status_code: int
    json: dict = field(default_factory=dict)
    text: str = ""


@runtime_checkable
class Transport(Protocol):
    def request(self, method, url, *, headers=None, params=None, json=None) -> HttpResponse: ...


class MockTransport:
    """Records every call and returns canned/default responses — for dry-runs and tests."""

    def __init__(self, routes: dict[str, HttpResponse] | None = None):
        self.routes = routes or {}  # "POST /rest/api/3/issue" -> HttpResponse
        self.calls: list[dict] = []

    def request(self, method, url, *, headers=None, params=None, json=None) -> HttpResponse:
        self.calls.append({"method": method, "url": url, "headers": headers, "params": params, "json": json})
        for key, resp in self.routes.items():
            m, _, frag = key.partition(" ")
            if m.upper() == method.upper() and frag in url:
                return resp
        return HttpResponse(status_code=200, json={"ok": True})  # sensible default


class ActionSpec(BaseModel):
    name: str
    http_method: str = "GET"
    path: str = ""
    sample_input: dict = {}  # used by the dry-run harness to actually call the method


class CapabilitySpec(BaseModel):
    capability: str
    display_name: str = ""
    auth: str = ""
    base_url_template: str = ""
    actions: list[ActionSpec] = []

    def action_names(self) -> list[str]:
        return [a.name for a in self.actions]


class BaseConnector(abc.ABC):
    """Generated connectors subclass this: set `capability`, implement `spec()`, add one
    method per action, and route all HTTP through `self._request`."""

    capability: ClassVar[str] = ""

    def __init__(self, config: dict | None = None, transport: Transport | None = None):
        self.config = config or {}
        self.transport: Transport = transport or MockTransport()  # always safe to instantiate

    @classmethod
    @abc.abstractmethod
    def spec(cls) -> CapabilitySpec: ...

    def _base_url(self) -> str:
        tmpl = self.spec().base_url_template
        return re.sub(r"\{(\w+)\}", lambda m: str(self.config.get(m.group(1), m.group(1))), tmpl)

    def _request(self, method: str, path: str, **kwargs) -> HttpResponse:
        url = self._base_url().rstrip("/") + "/" + path.lstrip("/")
        return self.transport.request(method, url, **kwargs)
