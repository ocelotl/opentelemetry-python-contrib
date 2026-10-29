# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import contextlib
from unittest import mock

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation._semconv import (
    OTEL_SEMCONV_STABILITY_OPT_IN,
    _OpenTelemetrySemanticConventionStability,
    _StabilityMode,
)
from opentelemetry.instrumentation.pymongo import (
    CommandTracer,
    PymongoInstrumentor,
)
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.semconv._incubating.attributes.db_attributes import (
    DB_MONGODB_COLLECTION,
    DB_NAME,
    DB_STATEMENT,
    DB_SYSTEM,
)
from opentelemetry.semconv._incubating.attributes.net_attributes import (
    NET_PEER_NAME,
    NET_PEER_PORT,
)
from opentelemetry.semconv._incubating.metrics.db_metrics import (
    DB_CLIENT_OPERATION_DURATION,
    create_db_client_operation_duration,
)
from opentelemetry.semconv.attributes.db_attributes import (
    DB_COLLECTION_NAME,
    DB_NAMESPACE,
    DB_OPERATION_NAME,
    DB_QUERY_TEXT,
    DB_SYSTEM_NAME,
)
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.semconv.attributes.server_attributes import (
    SERVER_ADDRESS,
    SERVER_PORT,
)
from opentelemetry.test.test_base import TestBase


@contextlib.contextmanager
def use_semconv_opt_in(sem_conv_mode):
    env_patch = mock.patch.dict(
        "os.environ",
        {OTEL_SEMCONV_STABILITY_OPT_IN: sem_conv_mode},
    )
    _OpenTelemetrySemanticConventionStability._initialized = False
    env_patch.start()
    try:
        yield
    finally:
        env_patch.stop()
        _OpenTelemetrySemanticConventionStability._initialized = False


# pylint: disable=too-many-public-methods
class TestPymongo(TestBase):
    def setUp(self):
        super().setUp()
        self.tracer = self.tracer_provider.get_tracer(__name__)
        self.start_callback = mock.MagicMock()
        self.success_callback = mock.MagicMock()
        self.failed_callback = mock.MagicMock()
        PymongoInstrumentor._instance = None
        PymongoInstrumentor._commandtracer_instance = None
        _OpenTelemetrySemanticConventionStability._initialized = False

    def tearDown(self):
        super().tearDown()
        PymongoInstrumentor._instance = None
        PymongoInstrumentor._commandtracer_instance = None
        _OpenTelemetrySemanticConventionStability._initialized = False

    def test_pymongo_instrumentor(self):
        mock_register = mock.Mock()
        patch = mock.patch("pymongo.monitoring.register", side_effect=mock_register)
        with patch:
            PymongoInstrumentor().instrument()
        self.assertTrue(mock_register.called)

    def test_started(self):
        command_attrs = {
            "command_name": "find",
        }
        command_tracer = CommandTracer(self.tracer, request_hook=self.start_callback)
        mock_event = MockEvent(command_attrs, ("test.com", "1234"), "test_request_id")
        command_tracer.started(event=mock_event)
        # the memory exporter can't be used here because the span isn't ended
        # yet
        # pylint: disable=protected-access
        span = command_tracer._pop_span(mock_event)
        self.assertIs(span.kind, trace_api.SpanKind.CLIENT)
        self.assertEqual(span.name, "database_name.find")
        self.assertEqual(span.attributes[DB_SYSTEM], "mongodb")
        self.assertEqual(span.attributes[DB_NAME], "database_name")
        self.assertEqual(span.attributes[DB_STATEMENT], "find")
        self.assertEqual(span.attributes[NET_PEER_NAME], "test.com")
        self.assertEqual(span.attributes[NET_PEER_PORT], 1234)
        self.start_callback.assert_called_once_with(span, mock_event)

    def test_succeeded(self):
        mock_event = MockEvent({})
        command_tracer = CommandTracer(
            self.tracer,
            request_hook=self.start_callback,
            response_hook=self.success_callback,
        )
        command_tracer.started(event=mock_event)
        command_tracer.succeeded(event=mock_event)
        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 1)
        span = spans_list[0]
        self.assertIs(span.status.status_code, trace_api.StatusCode.UNSET)
        self.assertIsNotNone(span.end_time)
        self.start_callback.assert_called_once()
        self.success_callback.assert_called_once()

    def test_not_recording(self):
        mock_tracer = mock.Mock()
        mock_span = mock.Mock()
        mock_span.is_recording.return_value = False
        mock_tracer.start_span.return_value = mock_span
        mock_event = MockEvent({})
        command_tracer = CommandTracer(mock_tracer)
        command_tracer.started(event=mock_event)
        command_tracer.succeeded(event=mock_event)
        self.assertFalse(mock_span.is_recording())
        self.assertTrue(mock_span.is_recording.called)
        self.assertFalse(mock_span.set_attribute.called)
        self.assertFalse(mock_span.set_status.called)

    def test_suppression_key(self):
        mock_tracer = mock.Mock()
        mock_span = mock.Mock()
        mock_span.is_recording.return_value = True
        mock_tracer.start_span.return_value = mock_span
        mock_event = MockEvent({})
        mock_event.command.get = mock.Mock()
        mock_event.command.get.return_value = "dummy"

        with suppress_instrumentation():
            command_tracer = CommandTracer(mock_tracer)
            command_tracer.started(event=mock_event)
            command_tracer.succeeded(event=mock_event)

        # if suppression key is set, CommandTracer methods return immediately, so command.get is not invoked.
        self.assertFalse(
            mock_event.command.get.called  # pylint: disable=no-member
        )

    def test_failed(self):
        mock_event = MockEvent({})
        command_tracer = CommandTracer(
            self.tracer,
            request_hook=self.start_callback,
            failed_hook=self.failed_callback,
        )
        command_tracer.started(event=mock_event)
        mock_event.mark_as_failed()
        command_tracer.failed(event=mock_event)

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 1)
        span = spans_list[0]

        self.assertIs(
            span.status.status_code,
            trace_api.StatusCode.ERROR,
        )
        self.assertEqual(span.status.description, "operation failed")
        self.assertIsNotNone(span.end_time)
        self.start_callback.assert_called_once()
        self.failed_callback.assert_called_once()

    def test_multiple_commands(self):
        first_mock_event = MockEvent({}, ("firstUrl", "123"), "first")
        second_mock_event = MockEvent({}, ("secondUrl", "456"), "second")
        command_tracer = CommandTracer(self.tracer)
        command_tracer.started(event=first_mock_event)
        command_tracer.started(event=second_mock_event)
        command_tracer.succeeded(event=first_mock_event)
        second_mock_event.mark_as_failed()
        command_tracer.failed(event=second_mock_event)

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 2)
        first_span = spans_list[0]
        second_span = spans_list[1]

        self.assertIs(
            first_span.status.status_code,
            trace_api.StatusCode.UNSET,
        )
        self.assertIs(
            second_span.status.status_code,
            trace_api.StatusCode.ERROR,
        )

    def test_int_command(self):
        command_attrs = {
            "command_name": 123,
        }
        mock_event = MockEvent(command_attrs)

        command_tracer = CommandTracer(self.tracer)
        command_tracer.started(event=mock_event)
        command_tracer.succeeded(event=mock_event)

        spans_list = self.memory_exporter.get_finished_spans()

        self.assertEqual(len(spans_list), 1)
        span = spans_list[0]
        self.assertEqual(span.name, "database_name.123")

    def test_no_op_tracer(self):
        mock_event = MockEvent({})

        tracer = trace_api.NoOpTracer()
        command_tracer = CommandTracer(tracer)
        command_tracer.started(event=mock_event)
        command_tracer.succeeded(event=mock_event)

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 0)

    def test_capture_statement_getmore(self):
        command_attrs = {
            "command_name": "getMore",
            "collection": "test_collection",
        }
        mock_event = MockEvent(command_attrs)

        command_tracer = CommandTracer(self.tracer, capture_statement=True)
        command_tracer.started(event=mock_event)
        command_tracer.succeeded(event=mock_event)

        spans_list = self.memory_exporter.get_finished_spans()

        self.assertEqual(len(spans_list), 1)
        span = spans_list[0]

        self.assertEqual(
            span.attributes[DB_STATEMENT],
            "getMore test_collection",
        )

    def test_capture_statement_aggregate(self):
        pipeline = [
            {"$match": {"status": "active"}},
            {"$group": {"_id": "$category", "count": {"$sum": 1}}},
        ]
        command_attrs = {
            "command_name": "aggregate",
            "pipeline": pipeline,
        }
        command_tracer = CommandTracer(self.tracer, capture_statement=True)
        mock_event = MockEvent(command_attrs)
        command_tracer.started(event=mock_event)
        command_tracer.succeeded(event=mock_event)

        spans_list = self.memory_exporter.get_finished_spans()

        self.assertEqual(len(spans_list), 1)
        span = spans_list[0]

        expected_statement = f"aggregate {pipeline}"
        self.assertEqual(span.attributes[DB_STATEMENT], expected_statement)

    def test_capture_statement_disabled_getmore(self):
        command_attrs = {
            "command_name": "getMore",
            "collection": "test_collection",
        }
        command_tracer = CommandTracer(self.tracer, capture_statement=False)
        mock_event = MockEvent(command_attrs)
        command_tracer.started(event=mock_event)
        command_tracer.succeeded(event=mock_event)

        spans_list = self.memory_exporter.get_finished_spans()

        self.assertEqual(len(spans_list), 1)
        span = spans_list[0]

        self.assertEqual(span.attributes[DB_STATEMENT], "getMore")

    def test_capture_statement_disabled_aggregate(self):
        pipeline = [{"$match": {"status": "active"}}]
        command_attrs = {
            "command_name": "aggregate",
            "pipeline": pipeline,
        }
        command_tracer = CommandTracer(self.tracer, capture_statement=False)
        mock_event = MockEvent(command_attrs)
        command_tracer.started(event=mock_event)
        command_tracer.succeeded(event=mock_event)

        spans_list = self.memory_exporter.get_finished_spans()

        self.assertEqual(len(spans_list), 1)
        span = spans_list[0]

        self.assertEqual(span.attributes[DB_STATEMENT], "aggregate")

    def test_collection_name_attribute(self):
        scenarios = [
            (
                {
                    "command_name": "find",
                    "find": "test_collection",
                },
                "test_collection",
            ),
            ({"command_name": "find"}, None),
            ({"command_name": "find", "find": b"invalid"}, None),
        ]
        for command_attrs, expected in scenarios:
            with self.subTest(command_attrs=command_attrs, expected=expected):
                mock_event = MockEvent(command_attrs)

                command_tracer = CommandTracer(self.tracer, capture_statement=True)
                command_tracer.started(event=mock_event)
                command_tracer.succeeded(event=mock_event)

                spans_list = self.memory_exporter.get_finished_spans()

                self.assertEqual(len(spans_list), 1)
                span = spans_list[0]

                self.assertEqual(span.attributes[DB_STATEMENT], "find")

                self.assertEqual(
                    span.attributes.get(DB_MONGODB_COLLECTION),
                    expected,
                )
                self.memory_exporter.clear()

    def test_schema_url_default(self):
        with mock.patch("pymongo.monitoring.register"):
            instrumentor = PymongoInstrumentor()
            instrumentor.instrument(tracer_provider=self.tracer_provider)
        self.assertEqual(
            instrumentor._commandtracer_instance._tracer._instrumentation_scope.schema_url,
            "https://opentelemetry.io/schemas/1.11.0",
        )

    def test_schema_url_new_semconv(self):
        with use_semconv_opt_in("database"):
            with mock.patch("pymongo.monitoring.register"):
                instrumentor = PymongoInstrumentor()
                instrumentor.instrument(tracer_provider=self.tracer_provider)
        self.assertEqual(
            instrumentor._commandtracer_instance._tracer._instrumentation_scope.schema_url,
            "https://opentelemetry.io/schemas/1.25.0",
        )

    def test_started_new_semconv(self):
        command_tracer = CommandTracer(self.tracer, semconv_opt_in_mode=_StabilityMode.DATABASE)
        mock_event = MockEvent({"command_name": "find"}, ("testhost", 1234))
        command_tracer.started(event=mock_event)
        span = command_tracer._pop_span(mock_event)  # pylint: disable=protected-access
        self.assertEqual(span.attributes[DB_SYSTEM_NAME], "mongodb")
        self.assertEqual(span.attributes[DB_NAMESPACE], "database_name")
        self.assertEqual(span.attributes[DB_QUERY_TEXT], "find")
        self.assertEqual(span.attributes[SERVER_ADDRESS], "testhost")
        self.assertEqual(span.attributes[SERVER_PORT], 1234)

    def test_started_both_semconv(self):
        command_tracer = CommandTracer(self.tracer, semconv_opt_in_mode=_StabilityMode.DATABASE_DUP)
        mock_event = MockEvent({"command_name": "find"}, ("testhost", 1234))
        command_tracer.started(event=mock_event)
        span = command_tracer._pop_span(mock_event)  # pylint: disable=protected-access
        self.assertEqual(span.attributes[DB_SYSTEM], "mongodb")
        self.assertEqual(span.attributes[DB_NAME], "database_name")
        self.assertEqual(span.attributes[DB_STATEMENT], "find")
        self.assertEqual(span.attributes[NET_PEER_NAME], "testhost")
        self.assertEqual(span.attributes[NET_PEER_PORT], 1234)
        self.assertEqual(span.attributes[DB_SYSTEM_NAME], "mongodb")
        self.assertEqual(span.attributes[DB_NAMESPACE], "database_name")
        self.assertEqual(span.attributes[DB_QUERY_TEXT], "find")
        self.assertEqual(span.attributes[SERVER_ADDRESS], "testhost")
        self.assertEqual(span.attributes[SERVER_PORT], 1234)

    def test_collection_name_default_semconv(self):
        command_tracer = CommandTracer(self.tracer)
        mock_event = MockEvent({"command_name": "find", "find": "test_coll"})
        command_tracer.started(event=mock_event)
        command_tracer.succeeded(event=mock_event)
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.attributes[DB_MONGODB_COLLECTION], "test_coll")
        self.assertNotIn(DB_COLLECTION_NAME, span.attributes)

    def test_collection_name_new_semconv(self):
        command_tracer = CommandTracer(self.tracer, semconv_opt_in_mode=_StabilityMode.DATABASE)
        mock_event = MockEvent({"command_name": "find", "find": "test_coll"})
        command_tracer.started(event=mock_event)
        command_tracer.succeeded(event=mock_event)
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.attributes[DB_COLLECTION_NAME], "test_coll")
        self.assertNotIn(DB_MONGODB_COLLECTION, span.attributes)

    def test_collection_name_both_semconv(self):
        command_tracer = CommandTracer(self.tracer, semconv_opt_in_mode=_StabilityMode.DATABASE_DUP)
        mock_event = MockEvent({"command_name": "find", "find": "test_coll"})
        command_tracer.started(event=mock_event)
        command_tracer.succeeded(event=mock_event)
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.attributes[DB_MONGODB_COLLECTION], "test_coll")
        self.assertEqual(span.attributes[DB_COLLECTION_NAME], "test_coll")

    def test_failed_error_type_not_set_on_default(self):
        command_tracer = CommandTracer(self.tracer)
        mock_event = MockEvent({})
        command_tracer.started(event=mock_event)
        mock_event.mark_as_failed()
        command_tracer.failed(event=mock_event)
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertFalse(span.status.is_ok)
        self.assertNotIn(ERROR_TYPE, span.attributes)

    def test_failed_error_type_set_on_new_semconv(self):
        command_tracer = CommandTracer(self.tracer, semconv_opt_in_mode=_StabilityMode.DATABASE)
        mock_event = MockEvent({})
        command_tracer.started(event=mock_event)
        mock_event.failure = {
            "errmsg": "operation failed",
            "codeName": "OperationFailed",
        }
        command_tracer.failed(event=mock_event)
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertFalse(span.status.is_ok)
        self.assertEqual(span.attributes[ERROR_TYPE], "OperationFailed")

    def test_failed_error_type_set_on_both_semconv(self):
        command_tracer = CommandTracer(self.tracer, semconv_opt_in_mode=_StabilityMode.DATABASE_DUP)
        mock_event = MockEvent({})
        command_tracer.started(event=mock_event)
        mock_event.failure = {
            "errmsg": "operation failed",
            "codeName": "OperationFailed",
        }
        command_tracer.failed(event=mock_event)
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertFalse(span.status.is_ok)
        self.assertEqual(span.attributes[ERROR_TYPE], "OperationFailed")


class MockCommand:
    def __init__(self, command_attrs):
        self.command_attrs = command_attrs

    def get(self, key, default=""):
        return self.command_attrs.get(key, default)


class MockEvent:
    def __init__(self, command_attrs, connection_id=None, request_id="", duration_micros=1000):
        self.command = MockCommand(command_attrs)
        self.command_name = self.command.get("command_name")
        self.connection_id = connection_id
        self.request_id = request_id
        self.failure = None
        self.duration_micros = duration_micros

    def mark_as_failed(self):
        # CommandFailedEvent.failure is type _DocumentOut, which pymongo defines as:
        # ```
        # _DocumentOut = Union[MutableMapping[str, Any], "RawBSONDocument"]
        # ```
        # we go with the former, but both provide a `.get(key, default)` method.
        #
        self.failure = {"errmsg": "operation failed"}

    def __getattr__(self, item):
        return item


class TestPymongoOperationDuration(TestBase):
    """Tests for the db.client.operation.duration histogram."""

    def setUp(self):
        super().setUp()
        _OpenTelemetrySemanticConventionStability._initialized = False
        self.tracer = self.tracer_provider.get_tracer(__name__)

    def tearDown(self):
        super().tearDown()
        _OpenTelemetrySemanticConventionStability._initialized = False

    def _command_tracer(self, semconv_opt_in_mode=_StabilityMode.DEFAULT):
        meter = self.meter_provider.get_meter(__name__)
        return CommandTracer(
            self.tracer,
            create_db_client_operation_duration(meter),
            semconv_opt_in_mode=semconv_opt_in_mode,
        )

    def _duration_points(self):
        metrics = [metric for metric in self.get_sorted_metrics() if metric.name == DB_CLIENT_OPERATION_DURATION]
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].unit, "s")
        return list(metrics[0].data.data_points)

    @staticmethod
    def _event(duration_micros=2500):
        return MockEvent(
            {"command_name": "find", "find": "test_collection"},
            ("testhost", 27017),
            "request_id",
            duration_micros=duration_micros,
        )

    def test_duration_recorded_on_a_succeeded_command(self):
        command_tracer = self._command_tracer()
        event = self._event()
        command_tracer.started(event=event)
        command_tracer.succeeded(event=event)

        points = self._duration_points()
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].count, 1)

        attributes = dict(points[0].attributes)
        self.assertEqual(
            attributes,
            {
                DB_SYSTEM_NAME: "mongodb",
                DB_OPERATION_NAME: "find",
                DB_NAMESPACE: "database_name",
                SERVER_ADDRESS: "testhost",
                SERVER_PORT: 27017,
            },
        )
        self.assertIs(type(attributes[SERVER_PORT]), int)
        for key in (DB_SYSTEM_NAME, DB_OPERATION_NAME, DB_NAMESPACE, SERVER_ADDRESS):
            self.assertIs(type(attributes[key]), str)

    def test_the_recorded_value_is_what_pymongo_measured_in_seconds(self):
        command_tracer = self._command_tracer()
        event = self._event(duration_micros=2500)
        command_tracer.started(event=event)
        command_tracer.succeeded(event=event)

        self.assertAlmostEqual(self._duration_points()[0].sum, 0.0025)

    def test_duration_recorded_with_error_type_on_a_failed_command(self):
        command_tracer = self._command_tracer()
        event = self._event()
        command_tracer.started(event=event)
        event.failure = {
            "errmsg": "operation failed",
            "codeName": "OperationFailed",
        }
        command_tracer.failed(event=event)

        points = self._duration_points()
        self.assertEqual(len(points), 1)
        attributes = dict(points[0].attributes)
        self.assertEqual(attributes[ERROR_TYPE], "OperationFailed")
        self.assertIs(type(attributes[ERROR_TYPE]), str)

    def test_error_type_agrees_with_the_span_in_new_semconv(self):
        command_tracer = self._command_tracer(_StabilityMode.DATABASE)
        event = self._event()
        command_tracer.started(event=event)
        event.failure = {
            "errmsg": "operation failed",
            "codeName": "OperationFailed",
        }
        command_tracer.failed(event=event)

        span = self.memory_exporter.get_finished_spans()[0]
        self.assertEqual(span.attributes[ERROR_TYPE], "OperationFailed")
        self.assertEqual(dict(self._duration_points()[0].attributes)[ERROR_TYPE], "OperationFailed")

    def test_error_type_is_recorded_even_when_the_span_omits_it(self):
        command_tracer = self._command_tracer(_StabilityMode.DEFAULT)
        event = self._event()
        command_tracer.started(event=event)
        event.failure = {
            "errmsg": "operation failed",
            "codeName": "OperationFailed",
        }
        command_tracer.failed(event=event)

        span = self.memory_exporter.get_finished_spans()[0]
        self.assertNotIn(ERROR_TYPE, span.attributes)
        self.assertEqual(dict(self._duration_points()[0].attributes)[ERROR_TYPE], "OperationFailed")

    def test_an_unnamed_failure_code_is_recorded_as_unknown(self):
        command_tracer = self._command_tracer()
        event = self._event()
        command_tracer.started(event=event)
        event.mark_as_failed()
        command_tracer.failed(event=event)

        self.assertEqual(
            dict(self._duration_points()[0].attributes)[ERROR_TYPE],
            "UnknownError",
        )

    def test_nothing_recorded_for_a_command_that_never_started(self):
        command_tracer = self._command_tracer()
        command_tracer.succeeded(event=self._event())

        self.assertEqual(self.get_sorted_metrics(), [])

    def test_nothing_recorded_while_instrumentation_is_suppressed(self):
        command_tracer = self._command_tracer()
        event = self._event()
        with suppress_instrumentation():
            command_tracer.started(event=event)
            command_tracer.succeeded(event=event)

        self.assertEqual(self.get_sorted_metrics(), [])

    def test_server_attributes_absent_without_a_connection_id(self):
        command_tracer = self._command_tracer()
        event = MockEvent({"command_name": "find"}, None, "request_id")
        command_tracer.started(event=event)
        command_tracer.succeeded(event=event)

        attributes = dict(self._duration_points()[0].attributes)
        self.assertNotIn(SERVER_ADDRESS, attributes)
        self.assertNotIn(SERVER_PORT, attributes)
