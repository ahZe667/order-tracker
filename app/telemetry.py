import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

LOGGER_NAME = "order_tracker"


def setup_telemetry():
    """Install OpenTelemetry providers selected by ORDER_TRACKER_TELEMETRY.

    "console" prints signals to stdout; "otlp" sends them to
    OTEL_EXPORTER_OTLP_ENDPOINT. Unset means no SDK: the API stays a no-op,
    which keeps tests quiet.
    """
    mode = os.getenv("ORDER_TRACKER_TELEMETRY")
    if not mode:
        return
    if mode == "console":
        span_exporter = ConsoleSpanExporter()
        metric_exporter = ConsoleMetricExporter()
        log_exporter = ConsoleLogRecordExporter()
    elif mode == "otlp":
        span_exporter = OTLPSpanExporter()
        metric_exporter = OTLPMetricExporter()
        log_exporter = OTLPLogExporter()
    else:
        raise ValueError(f"Unsupported ORDER_TRACKER_TELEMETRY={mode!r}")

    resource = Resource.create({"service.name": "order-tracker"})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(metric_exporter)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(logger_provider)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))
