# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
from unittest import mock

from tortoise import Tortoise, fields, models

from opentelemetry import trace
from opentelemetry.instrumentation._semconv import (
    OTEL_SEMCONV_STABILITY_OPT_IN,
    _OpenTelemetrySemanticConventionStability,
)
from opentelemetry.instrumentation.tortoiseorm import TortoiseORMInstrumentor
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.semconv._incubating.attributes.db_attributes import (
    DB_NAME,
    DB_STATEMENT,
    DB_SYSTEM,
)
from opentelemetry.semconv._incubating.metrics.db_metrics import (
    DB_CLIENT_OPERATION_DURATION,
)
from opentelemetry.semconv.attributes.db_attributes import (
    DB_NAMESPACE,
    DB_OPERATION_NAME,
    DB_QUERY_TEXT,
    DB_SYSTEM_NAME,
)
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.test.test_base import TestBase


class MockModel(models.Model):
    id = fields.IntField(pk=True)
    name = fields.TextField()

    def __str__(self):
        return self.name


@contextlib.contextmanager
def use_semconv_opt_in(sem_conv_mode):
    """Context manager to set OTEL_SEMCONV_STABILITY_OPT_IN and reset state."""
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


class TestTortoiseORMInstrumentor(TestBase):
    def setUp(self):
        super().setUp()
        _OpenTelemetrySemanticConventionStability._initialized = False
        TortoiseORMInstrumentor().instrument()

    def tearDown(self):
        super().tearDown()
        TortoiseORMInstrumentor().uninstrument()
        _OpenTelemetrySemanticConventionStability._initialized = False
        if Tortoise._inited:
            self._async_call(Tortoise._drop_databases())

    # pylint: disable-next=no-self-use
    def _async_call(self, coro):
        return asyncio.run(coro)

    # pylint: disable-next=no-self-use
    async def _init_tortoise(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": [__name__]},
        )
        await Tortoise.generate_schemas()

    def test_trace_integration(self):
        """Default mode (no opt-in): old semconv attributes only."""

        async def run():
            await self._init_tortoise()
            await MockModel.create(name="Test 1")
            await MockModel.filter(name="Test 1").first()

        self._async_call(run())
        spans = self.memory_exporter.get_finished_spans()

        crud_spans = [s for s in spans if s.name in ("INSERT", "SELECT")]
        self.assertGreaterEqual(len(crud_spans), 2)

        insert_span = next(s for s in crud_spans if s.name == "INSERT")
        self.assertEqual(insert_span.kind, trace.SpanKind.CLIENT)
        # Old attrs present
        self.assertEqual(insert_span.attributes[DB_SYSTEM], "sqlite")
        self.assertEqual(insert_span.attributes[DB_NAME], ":memory:")
        self.assertIn("INSERT", insert_span.attributes[DB_STATEMENT])
        # New attrs must NOT be present in default mode
        self.assertNotIn(DB_SYSTEM_NAME, insert_span.attributes)
        self.assertNotIn(DB_NAMESPACE, insert_span.attributes)
        self.assertNotIn(DB_QUERY_TEXT, insert_span.attributes)

        select_span = next(s for s in crud_spans if s.name == "SELECT")
        self.assertEqual(select_span.kind, trace.SpanKind.CLIENT)
        self.assertEqual(select_span.attributes[DB_SYSTEM], "sqlite")
        self.assertEqual(select_span.attributes[DB_NAME], ":memory:")
        self.assertIn("SELECT", select_span.attributes[DB_STATEMENT])

    def test_trace_integration_new_semconv(self):
        """database opt-in mode: new stable semconv attributes only."""
        TortoiseORMInstrumentor().uninstrument()

        with use_semconv_opt_in("database"):
            TortoiseORMInstrumentor().instrument()

            async def run():
                await self._init_tortoise()
                await MockModel.create(name="Test New Semconv")
                await MockModel.filter(name="Test New Semconv").first()

            self._async_call(run())

        spans = self.memory_exporter.get_finished_spans()
        crud_spans = [s for s in spans if s.name in ("INSERT", "SELECT")]
        self.assertGreaterEqual(len(crud_spans), 2)

        insert_span = next(s for s in crud_spans if s.name == "INSERT")
        # New attrs present
        self.assertEqual(insert_span.attributes[DB_SYSTEM_NAME], "sqlite")
        self.assertEqual(insert_span.attributes[DB_NAMESPACE], ":memory:")
        self.assertIn("INSERT", insert_span.attributes[DB_QUERY_TEXT])
        # Old attrs must NOT be present
        self.assertNotIn(DB_SYSTEM, insert_span.attributes)
        self.assertNotIn(DB_NAME, insert_span.attributes)
        self.assertNotIn(DB_STATEMENT, insert_span.attributes)

        select_span = next(s for s in crud_spans if s.name == "SELECT")
        self.assertEqual(select_span.attributes[DB_SYSTEM_NAME], "sqlite")
        self.assertIn("SELECT", select_span.attributes[DB_QUERY_TEXT])

    def test_trace_integration_dup_semconv(self):
        """database/dup mode: both old and new semconv attributes present."""
        TortoiseORMInstrumentor().uninstrument()

        with use_semconv_opt_in("database/dup"):
            TortoiseORMInstrumentor().instrument()

            async def run():
                await self._init_tortoise()
                await MockModel.create(name="Test Dup Semconv")

            self._async_call(run())

        spans = self.memory_exporter.get_finished_spans()
        insert_span = next((s for s in spans if s.name == "INSERT"), None)
        self.assertIsNotNone(insert_span)

        # Old attrs present
        self.assertIn(DB_SYSTEM, insert_span.attributes)
        self.assertIn(DB_NAME, insert_span.attributes)
        self.assertIn(DB_STATEMENT, insert_span.attributes)
        # New attrs also present
        self.assertIn(DB_SYSTEM_NAME, insert_span.attributes)
        self.assertIn(DB_NAMESPACE, insert_span.attributes)
        self.assertIn(DB_QUERY_TEXT, insert_span.attributes)
        # Values are consistent
        self.assertEqual(insert_span.attributes[DB_SYSTEM], "sqlite")
        self.assertEqual(insert_span.attributes[DB_SYSTEM_NAME], "sqlite")
        self.assertEqual(insert_span.attributes[DB_NAME], ":memory:")
        self.assertEqual(insert_span.attributes[DB_NAMESPACE], ":memory:")

    def test_capture_parameters(self):
        TortoiseORMInstrumentor().uninstrument()
        TortoiseORMInstrumentor().instrument(capture_parameters=True)

        async def run():
            await self._init_tortoise()
            await MockModel.create(name="Test Parameterized")

        self._async_call(run())
        spans = self.memory_exporter.get_finished_spans()
        insert_span = next(s for s in spans if s.name == "INSERT")
        self.assertIn("db.statement.parameters", insert_span.attributes)
        self.assertIn(
            "Test Parameterized",
            insert_span.attributes["db.statement.parameters"],
        )

    def test_uninstrument(self):
        TortoiseORMInstrumentor().uninstrument()

        async def run():
            await self._init_tortoise()
            await MockModel.create(name="Test Uninstrumented")

        self._async_call(run())
        spans = self.memory_exporter.get_finished_spans()
        crud_spans = [s for s in spans if s.name in ("INSERT", "SELECT")]
        self.assertEqual(len(crud_spans), 0)

    def test_suppress_instrumentation(self):
        async def run():
            await self._init_tortoise()
            with suppress_instrumentation():
                await MockModel.create(name="Test Suppressed")

        self._async_call(run())
        spans = self.memory_exporter.get_finished_spans()
        crud_spans = [s for s in spans if s.name in ("INSERT", "SELECT")]
        self.assertEqual(len(crud_spans), 0)

    def test_no_op_tracer_provider(self):
        TortoiseORMInstrumentor().uninstrument()
        TortoiseORMInstrumentor().instrument(tracer_provider=trace.NoOpTracerProvider())

        async def run():
            await self._init_tortoise()
            await MockModel.create(name="Test NoOp")

        self._async_call(run())
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 0)

    def test_span_name_empty_or_whitespace_statement(self):
        """Regression: an empty or whitespace-only raw statement has no tokens
        after split(); _do_execute must not raise IndexError."""

        async def run():
            await self._init_tortoise()
            conn = Tortoise.get_connection("default")
            await conn.execute_query("   ")
            await conn.execute_script("")

        self._async_call(run())

        spans = self.memory_exporter.get_finished_spans()
        # One span per raw statement; no operation token survives, so the
        # span name falls back to the database name (":memory:" for sqlite)
        # rather than the call raising.
        fallback_named = [s for s in spans if s.name == ":memory:"]
        self.assertEqual(len(fallback_named), 2)
        for span in fallback_named:
            self.assertEqual(span.kind, trace.SpanKind.CLIENT)

    def test_span_name_strips_leading_comment(self):
        async def run():
            await self._init_tortoise()
            conn = Tortoise.get_connection("default")
            await conn.execute_query("/* c */ SELECT 1")
            await conn.execute_query("/* c */")

        self._async_call(run())

        span_names = [s.name for s in self.memory_exporter.get_finished_spans()]
        self.assertIn("SELECT", span_names)
        self.assertIn(":memory:", span_names)
        self.assertNotIn("/*", span_names)


class TestTortoiseORMOperationDuration(TestBase):
    """Tests for the db.client.operation.duration histogram."""

    def setUp(self):
        super().setUp()
        _OpenTelemetrySemanticConventionStability._initialized = False
        TortoiseORMInstrumentor().instrument()

    def tearDown(self):
        super().tearDown()
        TortoiseORMInstrumentor().uninstrument()
        _OpenTelemetrySemanticConventionStability._initialized = False
        if Tortoise._inited:
            asyncio.run(Tortoise._drop_databases())

    @staticmethod
    async def _init_tortoise():
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": [__name__]},
        )
        await Tortoise.generate_schemas()

    def _duration_points(self):
        metrics = [metric for metric in self.get_sorted_metrics() if metric.name == DB_CLIENT_OPERATION_DURATION]
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].unit, "s")
        return list(metrics[0].data.data_points)

    def test_duration_recorded_per_operation(self):
        async def run():
            await self._init_tortoise()
            await MockModel.create(name="Test 1")
            await MockModel.filter(name="Test 1").first()

        asyncio.run(run())

        points = self._duration_points()
        by_operation = {dict(point.attributes).get(DB_OPERATION_NAME): point for point in points}
        self.assertIn("INSERT", by_operation)
        self.assertIn("SELECT", by_operation)

        attributes = dict(by_operation["SELECT"].attributes)
        self.assertEqual(attributes[DB_SYSTEM_NAME], "sqlite")
        self.assertEqual(attributes[DB_NAMESPACE], ":memory:")
        self.assertNotIn(ERROR_TYPE, attributes)
        self.assertIs(type(attributes[DB_SYSTEM_NAME]), str)
        self.assertIs(type(attributes[DB_NAMESPACE]), str)
        self.assertGreaterEqual(by_operation["SELECT"].sum, 0.0)

    def test_duration_recorded_in_every_semconv_mode(self):
        for mode in ("", "database", "database/dup"):
            with self.subTest(mode=mode), use_semconv_opt_in(mode):
                TortoiseORMInstrumentor().uninstrument()
                TortoiseORMInstrumentor().instrument()

                async def run():
                    await self._init_tortoise()
                    await MockModel.create(name="Test 1")

                asyncio.run(run())
                asyncio.run(Tortoise._drop_databases())

                attributes = dict(self._duration_points()[0].attributes)
                self.assertEqual(attributes[DB_SYSTEM_NAME], "sqlite")

    def test_duration_recorded_with_error_type_on_a_failed_query(self):
        async def run():
            await self._init_tortoise()
            connection = Tortoise.get_connection("default")
            await connection.execute_query("SELECT * FROM table_that_does_not_exist")

        with self.assertRaises(Exception) as raised:
            asyncio.run(run())

        failed = [point for point in self._duration_points() if ERROR_TYPE in dict(point.attributes)]
        self.assertEqual(len(failed), 1)
        attributes = dict(failed[0].attributes)
        self.assertEqual(attributes[ERROR_TYPE], type(raised.exception).__qualname__)
        self.assertEqual(attributes[DB_OPERATION_NAME], "SELECT")
        self.assertIs(type(attributes[ERROR_TYPE]), str)

    def test_nothing_recorded_while_instrumentation_is_suppressed(self):
        async def run():
            with suppress_instrumentation():
                await self._init_tortoise()
                await MockModel.create(name="Test 1")

        asyncio.run(run())

        self.assertEqual(self.get_sorted_metrics(), [])
