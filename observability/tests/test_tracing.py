"""Exercise the installed wheel in fresh processes; OTel globals are process-owned."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
import sys
import threading
import unittest

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


class TracingTests(unittest.TestCase):
    def run_process(self, program, endpoint=''):
        result = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True,
                                env={**os.environ, 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': endpoint}, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_is_inert_and_disabled_export_still_creates_trace_ids(self):
        self.run_process('''
from opentelemetry import trace
before = trace.get_tracer_provider()
from portfolio_agent_observability import configure, shutdown
assert trace.get_tracer_provider() is before
provider = configure(service_name='another-agent')
assert configure(service_name='another-agent') is provider
assert provider.resource.attributes['langfuse.export'] == 'false'
with trace.get_tracer('test').start_as_current_span('work') as span:
    assert span.get_span_context().is_valid
try:
    configure(service_name='different-agent')
except RuntimeError:
    pass
else:
    raise AssertionError('conflicting settings were silently ignored')
shutdown()
''')

    def test_foreign_provider_is_neither_overridden_nor_shut_down(self):
        self.run_process('''
from unittest.mock import Mock
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from portfolio_agent_observability import configure, shutdown
foreign = TracerProvider()
foreign.shutdown = Mock()
trace.set_tracer_provider(foreign)
try:
    configure(service_name='another-agent')
except RuntimeError:
    pass
else:
    raise AssertionError('foreign provider was overridden')
shutdown()
foreign.shutdown.assert_not_called()
''')

    def test_wire_export_and_outage_are_optional_and_preserve_resource_marker(self):
        packets = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                packets.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        program = '''
from functools import partial
import os, time
from opentelemetry import trace
from portfolio_agent_observability import configure, shutdown
callback = partial(configure, service_name='second-agent', service_version='revision', langfuse=True)
started = time.monotonic()
callback(connection_string=None, log_level='INFO')
with trace.get_tracer('test').start_as_current_span('agent'):
    with trace.get_tracer('test').start_as_current_span('model'):
        pass
assert time.monotonic() - started < 1
assert os.environ['OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT'] == 'false'
shutdown()
'''
        try:
            self.run_process(program, f'http://127.0.0.1:{server.server_port}/v1/traces')
            self.run_process(program, 'http://127.0.0.1:1/v1/traces')
            names = set()
            self.assertTrue(packets)
            for packet in packets:
                for resource in ExportTraceServiceRequest.FromString(packet).resource_spans:
                    attributes = {v.key: v.value.string_value for v in resource.resource.attributes}
                    self.assertEqual(attributes['service.name'], 'second-agent')
                    self.assertEqual(attributes['service.version'], 'revision')
                    self.assertEqual(attributes['langfuse.export'], 'true')
                    for scope in resource.scope_spans:
                        names.update(span.name for span in scope.spans)
            self.assertEqual(names, {'agent', 'model'})
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
