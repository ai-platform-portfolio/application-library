"""Optional trace export, shared by hosted agents and evaluation processes."""
import os
from threading import Lock

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_lock = Lock()
_provider = None
_settings = None


def configure(*, service_name, service_version=None, langfuse=False, **_host_options):
    """Initialize once, before serving requests; return the process-owned provider.

    Bind caller settings with functools.partial for a hosting SDK callback.
    Additional SDK callback options are accepted; this configures OTLP traces
    only, not Azure Monitor, automatic instrumentation, logs or metrics.
    """
    global _provider, _settings
    if not isinstance(service_name, str) or not service_name.strip() or type(langfuse) is not bool:
        raise ValueError('Declare a non-empty service name and boolean Langfuse choice')
    endpoint = os.getenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT') or None
    settings = service_name, service_version, langfuse, endpoint
    with _lock:
        if _provider is not None:
            if settings != _settings:
                raise RuntimeError('Telemetry is already configured with different settings')
            return _provider
        if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            raise RuntimeError('A trace provider already exists; configure only one owner')
        os.environ['OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT'] = 'false'
        attributes = {'service.name': service_name, 'langfuse.export': str(langfuse).lower()}
        if service_version is not None:
            attributes['service.version'] = service_version
        provider = TracerProvider(resource=Resource.create(attributes))
        if endpoint:
            provider.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, timeout=2),
                max_queue_size=512, max_export_batch_size=128,
                schedule_delay_millis=1000, export_timeout_millis=3000,
            ))
        trace.set_tracer_provider(provider)
        _provider, _settings = provider, settings
        return provider


def shutdown():
    """Flush and shut down this package's provider, never a foreign provider.

    Call once at process exit. OpenTelemetry providers cannot be replaced/restarted
    in the same process; export is best-effort, not a delivery guarantee.
    """
    if _provider is not None:
        _provider.force_flush(timeout_millis=3000)
        _provider.shutdown()
