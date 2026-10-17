"""OpenTelemetry tracer setup (exports to Arize Phoenix over OTLP gRPC) and a span helper."""

import functools
import inspect
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from gtm_core.config import get_settings

_configured = False


def setup_tracing(service_name: str = "gtm-platform", export: bool = True) -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if export and not get_settings().is_sandbox:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=get_settings().phoenix_collector_endpoint, insecure=True)
            )
        )
    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("gtm")


def new_trace_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def span(name: str, **attrs: Any):
    """Context manager creating a span; attribute values are stringified if not primitive."""
    with get_tracer().start_as_current_span(name) as s:
        for k, v in attrs.items():
            s.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        yield s


def traced(name: str) -> Callable:
    """Decorator for sync/async functions (agent nodes, judge calls, integration calls)."""

    def deco(fn: Callable) -> Callable:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrap(*a: Any, **kw: Any):
                with span(name):
                    return await fn(*a, **kw)

            return awrap

        @functools.wraps(fn)
        def wrap(*a: Any, **kw: Any):
            with span(name):
                return fn(*a, **kw)

        return wrap

    return deco
