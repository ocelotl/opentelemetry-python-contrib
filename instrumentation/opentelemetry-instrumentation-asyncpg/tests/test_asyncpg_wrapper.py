# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
from unittest import mock

import asyncpg
import asyncpg.connection
import pytest
from asyncpg import Connection, Record, cursor
from asyncpg.prepared_stmt import PreparedStatement

try:
    # wrapt 2.0.0+
    from wrapt import BaseObjectProxy  # pylint: disable=no-name-in-module
except ImportError:
    from wrapt import ObjectProxy as BaseObjectProxy

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation._semconv import (
    OTEL_SEMCONV_STABILITY_OPT_IN,
    _OpenTelemetrySemanticConventionStability,
)
from opentelemetry.instrumentation.asyncpg import (
    _PREPARED_STMT_METHODS,
    AsyncPGInstrumentor,
)
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.semconv._incubating.attributes.db_attributes import (
    DB_NAME,
    DB_STATEMENT,
    DB_SYSTEM,
    DB_USER,
)
from opentelemetry.semconv._incubating.attributes.net_attributes import (
    NET_PEER_NAME,
    NET_PEER_PORT,
    NET_TRANSPORT,
    NetTransportValues,
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
from opentelemetry.semconv.attributes.network_attributes import (
    NETWORK_TRANSPORT,
    NetworkTransportValues,
)
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


class TestAsyncPGInstrumentation(TestBase):
    def tearDown(self):
        super().tearDown()
        AsyncPGInstrumentor().uninstrument()

    def test_duplicated_instrumentation_can_be_uninstrumented(self):
        AsyncPGInstrumentor().instrument()
        AsyncPGInstrumentor().instrument()
        AsyncPGInstrumentor().instrument()
        AsyncPGInstrumentor().uninstrument()
        for method_name in ["execute", "fetch"]:
            method = getattr(Connection, method_name, None)
            self.assertFalse(hasattr(method, "_opentelemetry_ext_asyncpg_applied"))

    def test_duplicated_instrumentation_works(self):
        first = AsyncPGInstrumentor()
        first.instrument()
        second = AsyncPGInstrumentor()
        second.instrument()
        self.assertIsNotNone(first._tracer)
        self.assertIsNotNone(second._tracer)

    def test_duplicated_uninstrumentation(self):
        AsyncPGInstrumentor().instrument()
        AsyncPGInstrumentor().uninstrument()
        AsyncPGInstrumentor().uninstrument()
        AsyncPGInstrumentor().uninstrument()
        for method_name in ["execute", "fetch"]:
            method = getattr(Connection, method_name, None)
            self.assertFalse(hasattr(method, "_opentelemetry_ext_asyncpg_applied"))

    def test_cursor_instrumentation(self):
        def assert_wrapped(assert_fnc):
            for cls, methods in [
                (cursor.Cursor, ("forward", "fetch", "fetchrow")),
                (cursor.CursorIterator, ("__anext__",)),
            ]:
                for method_name in methods:
                    method = getattr(cls, method_name, None)
                    assert_fnc(
                        isinstance(method, BaseObjectProxy),
                        f"{method} isinstance {type(method)}",
                    )

        assert_wrapped(self.assertFalse)
        AsyncPGInstrumentor().instrument()
        assert_wrapped(self.assertTrue)
        AsyncPGInstrumentor().uninstrument()
        assert_wrapped(self.assertFalse)

    def test_cursor_span_creation(self):
        """Test the cursor wrapper if it creates spans correctly."""

        # Mock out all interaction with postgres
        async def bind_mock(*args, **kwargs):
            return []

        async def exec_mock(*args, **kwargs):
            return [], None, True

        conn = mock.Mock()
        conn.is_closed = lambda: False

        conn._protocol = mock.Mock()
        conn._protocol.bind = bind_mock
        conn._protocol.execute = exec_mock
        conn._protocol.bind_execute = exec_mock
        conn._protocol.close_portal = bind_mock

        state = mock.Mock()
        state.closed = False

        apg = AsyncPGInstrumentor()
        apg.instrument(tracer_provider=self.tracer_provider)

        # init the cursor and fetch a single record
        crs = cursor.Cursor(conn, "SELECT * FROM test", state, [], Record)
        asyncio.run(crs._init(1))
        with suppress_instrumentation():
            self.assertEqual(asyncio.run(crs.fetch(1)), [])
        self.assertEqual(self.memory_exporter.get_finished_spans(), ())
        asyncio.run(crs.fetch(1))

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].name, "CURSOR: SELECT")
        self.assertTrue(spans[0].status.is_ok)

        # Now test that the StopAsyncIteration of the cursor does not get recorded as an ERROR
        crs_iter = cursor.CursorIterator(conn, "SELECT * FROM test", state, [], Record, 1, 1)

        with suppress_instrumentation(), pytest.raises(StopAsyncIteration):
            asyncio.run(anext(crs_iter))
        self.assertEqual(len(self.memory_exporter.get_finished_spans()), 1)

        with pytest.raises(StopAsyncIteration):
            asyncio.run(anext(crs_iter))

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 2)
        self.assertEqual([span.status.is_ok for span in spans], [True, True])

    def test_no_op_tracer_provider(self):
        AsyncPGInstrumentor().uninstrument()
        AsyncPGInstrumentor().instrument(tracer_provider=trace_api.NoOpTracerProvider())

        # Mock out all interaction with postgres
        async def bind_mock(*args, **kwargs):
            return []

        async def exec_mock(*args, **kwargs):
            return [], None, True

        conn = mock.Mock()
        conn.is_closed = lambda: False

        conn._protocol = mock.Mock()
        conn._protocol.bind = bind_mock
        conn._protocol.execute = exec_mock
        conn._protocol.bind_execute = exec_mock
        conn._protocol.close_portal = bind_mock

        state = mock.Mock()
        state.closed = False

        # init the cursor and fetch a single record
        crs = cursor.Cursor(conn, "SELECT * FROM test", state, [], Record)
        asyncio.run(crs._init(1))
        asyncio.run(crs.fetch(1))

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 0)

    def test_prepared_statement_instrumentation(self):
        methods = [m for m in _PREPARED_STMT_METHODS if hasattr(PreparedStatement, m)]

        for method_name in methods:
            with self.subTest(method=method_name, phase="before"):
                self.assertFalse(
                    isinstance(
                        getattr(PreparedStatement, method_name),
                        BaseObjectProxy,
                    )
                )

        AsyncPGInstrumentor().instrument()

        for method_name in methods:
            with self.subTest(method=method_name, phase="instrumented"):
                self.assertTrue(
                    isinstance(
                        getattr(PreparedStatement, method_name),
                        BaseObjectProxy,
                    )
                )

        AsyncPGInstrumentor().uninstrument()

        for method_name in methods:
            with self.subTest(method=method_name, phase="uninstrumented"):
                self.assertFalse(
                    isinstance(
                        getattr(PreparedStatement, method_name),
                        BaseObjectProxy,
                    )
                )

    @staticmethod
    def _make_prepared_stmt_conn():
        async def bind_execute_mock(*args, **kwargs):
            return [], b"SELECT 1", True

        async def bind_execute_many_mock(*args, **kwargs):
            return None

        conn = mock.Mock()
        conn._pool_release_ctr = 0
        conn.is_closed = lambda: False
        conn._protocol = mock.Mock()
        conn._protocol.bind_execute = bind_execute_mock
        conn._protocol.bind_execute_many = bind_execute_many_mock

        state = mock.Mock()
        state.closed = False
        return conn, state

    def test_prepared_statement_span(self):
        # Per-method: (query, call_args, expected_span_name)
        method_cases = {
            "fetch": ("SELECT * FROM users", (), "SELECT"),
            "fetchval": ("SELECT id FROM users WHERE id=$1", (1,), "SELECT"),
            "fetchrow": ("SELECT * FROM t WHERE v=$1", ("x",), "SELECT"),
            "executemany": (
                "INSERT INTO t (v) VALUES ($1)",
                ([("a",), ("b",)],),
                "INSERT",
            ),
            "fetchmany": ("SELECT * FROM t", ([],), "SELECT"),
        }

        for method_name in _PREPARED_STMT_METHODS:
            if not hasattr(PreparedStatement, method_name):
                continue
            query, call_args, expected_name = method_cases[method_name]
            with self.subTest(method=method_name):
                self.memory_exporter.clear()
                conn, state = self._make_prepared_stmt_conn()
                apg = AsyncPGInstrumentor()
                apg.instrument(tracer_provider=self.tracer_provider)

                stmt = PreparedStatement(conn, query, state)
                with suppress_instrumentation():
                    suppressed_result = asyncio.run(getattr(stmt, method_name)(*call_args))
                self.assertEqual(self.memory_exporter.get_finished_spans(), ())
                result = asyncio.run(getattr(stmt, method_name)(*call_args))
                self.assertEqual(result, suppressed_result)

                spans = self.memory_exporter.get_finished_spans()
                self.assertEqual(len(spans), 1)
                self.assertEqual(spans[0].name, expected_name)
                self.assertTrue(spans[0].status.is_ok)
                self.assertEqual(spans[0].attributes.get("db.statement"), query)
                self.assertEqual(spans[0].attributes.get("db.system"), "postgresql")

                apg.uninstrument()

    def test_prepared_statement_error_span(self):
        async def bind_execute_error(*args, **kwargs):
            raise RuntimeError("db error")

        conn = mock.Mock()
        conn._pool_release_ctr = 0
        conn.is_closed = lambda: False
        conn._protocol = mock.Mock()
        conn._protocol.bind_execute = bind_execute_error

        state = mock.Mock()
        state.closed = False

        apg = AsyncPGInstrumentor()
        apg.instrument(tracer_provider=self.tracer_provider)

        stmt = PreparedStatement(conn, "SELECT 1", state)
        with suppress_instrumentation(), self.assertRaisesRegex(RuntimeError, "db error"):
            asyncio.run(stmt.fetch())
        self.assertEqual(self.memory_exporter.get_finished_spans(), ())

        with self.assertRaises(RuntimeError):
            asyncio.run(stmt.fetch())

        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        self.assertFalse(spans[0].status.is_ok)


class TestAsyncPGSemconvMigration(TestBase):
    def setUp(self):
        super().setUp()
        _OpenTelemetrySemanticConventionStability._initialized = False

    def tearDown(self):
        super().tearDown()
        AsyncPGInstrumentor().uninstrument()
        _OpenTelemetrySemanticConventionStability._initialized = False

    @staticmethod
    def _make_execute_conn(addr=("testhost", 5432)):
        async def query_mock(*args, **kwargs):
            return b"SELECT 1", [], True

        async def execute_mock(*args, **kwargs):
            return [], b"SELECT 1", True

        conn = mock.Mock()
        conn.is_closed = lambda: False
        conn._pool_release_ctr = 0
        conn._query_loggers = []
        conn._execute = execute_mock
        conn._protocol = mock.Mock()
        conn._protocol.query = query_mock
        conn._addr = addr
        params = mock.Mock()
        params.database = "testdb"
        params.user = "testuser"
        conn._params = params
        return conn

    def _run_execute(self, conn, query="SELECT 1"):
        apg = AsyncPGInstrumentor()
        apg.instrument(tracer_provider=self.tracer_provider)

        async def do_execute():
            # Use __get__ to bind conn as the instance so wrapt correctly
            # receives instance=conn instead of instance=None.
            execute = asyncpg.connection.Connection.execute.__get__(  # pylint: disable=no-value-for-parameter
                conn, asyncpg.connection.Connection
            )
            await execute(query)

        asyncio.run(do_execute())
        return self.memory_exporter.get_finished_spans()

    def test_schema_url(self):
        apg = AsyncPGInstrumentor()
        apg.instrument(tracer_provider=self.tracer_provider)
        self.assertEqual(
            apg._tracer._instrumentation_scope.schema_url,
            "https://opentelemetry.io/schemas/1.11.0",
        )

    def test_schema_url_new_semconv(self):
        with use_semconv_opt_in("database"):
            apg = AsyncPGInstrumentor()
            apg.instrument(tracer_provider=self.tracer_provider)
            self.assertEqual(
                apg._tracer._instrumentation_scope.schema_url,
                "https://opentelemetry.io/schemas/1.25.0",
            )

    def test_suppress_instrumentation(self) -> None:
        conn = self._make_execute_conn()
        conn._protocol.query = mock.AsyncMock(return_value="SELECT 1")
        with suppress_instrumentation():
            spans = self._run_execute(conn)
        self.assertEqual(spans, ())
        conn._protocol.query.assert_awaited_once_with("SELECT 1", None)

        AsyncPGInstrumentor().uninstrument()
        self.assertEqual(len(self._run_execute(conn)), 1)

    def test_suppress_instrumentation_error(self) -> None:
        conn = self._make_execute_conn()
        error = RuntimeError("db error")
        conn._protocol.query = mock.AsyncMock(side_effect=error)
        with suppress_instrumentation(), self.assertRaises(RuntimeError) as raised:
            self._run_execute(conn)
        self.assertIs(raised.exception, error)
        self.assertEqual(self.memory_exporter.get_finished_spans(), ())

    def test_span(self):
        conn = self._make_execute_conn()
        spans = self._run_execute(conn)

        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.attributes[DB_SYSTEM], "postgresql")
        self.assertEqual(span.attributes[DB_NAME], "testdb")
        self.assertEqual(span.attributes[DB_USER], "testuser")
        self.assertEqual(span.attributes[NET_PEER_NAME], "testhost")
        self.assertEqual(span.attributes[NET_PEER_PORT], 5432)
        self.assertEqual(span.attributes[DB_STATEMENT], "SELECT 1")

    def test_keyword_query(self) -> None:
        method_cases = (
            ("execute", {"query": "SELECT 42"}, "SELECT 1"),
            ("fetch", {"query": "SELECT 42"}, [(42,)]),
            ("fetchval", {"query": "SELECT 42"}, 42),
            ("fetchrow", {"query": "SELECT 42"}, (42,)),
            ("executemany", {"command": "SELECT $1", "args": [(42,)]}, None),
        )
        for semconv_mode, statement_attribute in (
            ("", DB_STATEMENT),
            ("database", DB_QUERY_TEXT),
        ):
            for method_name, kwargs, expected_result in method_cases:
                with self.subTest(mode=semconv_mode, method=method_name), use_semconv_opt_in(semconv_mode):
                    self.memory_exporter.clear()
                    conn = self._make_execute_conn()
                    conn._protocol.query = mock.AsyncMock(return_value="SELECT 1")
                    conn._execute = mock.AsyncMock(return_value=[(42,)])
                    conn._executemany = mock.AsyncMock(return_value=None)
                    AsyncPGInstrumentor().instrument(tracer_provider=self.tracer_provider)
                    try:
                        method = getattr(Connection, method_name).__get__(conn, Connection)  # pylint: disable=no-value-for-parameter
                        self.assertEqual(asyncio.run(method(**kwargs, timeout=5)), expected_result)
                    finally:
                        AsyncPGInstrumentor().uninstrument()

                    spans = self.memory_exporter.get_finished_spans()
                    self.assertEqual(len(spans), 1)
                    self.assertEqual(spans[0].name, "SELECT")
                    self.assertEqual(
                        spans[0].attributes[statement_attribute], kwargs.get("query", kwargs.get("command"))
                    )

    def test_keyword_query_error(self) -> None:
        conn = self._make_execute_conn()
        error = RuntimeError("db error")
        conn._protocol.query = mock.AsyncMock(side_effect=error)
        AsyncPGInstrumentor().instrument(tracer_provider=self.tracer_provider)
        execute = Connection.execute.__get__(conn, Connection)  # pylint: disable=no-value-for-parameter

        with self.assertRaises(RuntimeError) as raised:
            asyncio.run(execute(query="SELECT 1"))

        self.assertIs(raised.exception, error)
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        self.assertFalse(spans[0].status.is_ok)

    def test_keyword_executemany_capture_parameters(self) -> None:
        conn = self._make_execute_conn()
        conn._executemany = mock.AsyncMock(return_value=None)
        AsyncPGInstrumentor(capture_parameters=True).instrument(tracer_provider=self.tracer_provider)
        executemany = Connection.executemany.__get__(conn, Connection)  # pylint: disable=no-value-for-parameter
        asyncio.run(executemany(command="SELECT $1", args=[(42,)], timeout=5))

        conn._executemany.assert_awaited_once_with("SELECT $1", [(42,)], 5)
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].attributes[DB_STATEMENT], "SELECT $1")
        self.assertEqual(spans[0].attributes["db.statement.parameters"], "([(42,)],)")

    def test_missing_query_raises_type_error(self) -> None:
        conn = self._make_execute_conn()
        AsyncPGInstrumentor().instrument(tracer_provider=self.tracer_provider)
        execute = Connection.execute.__get__(conn, Connection)  # pylint: disable=no-value-for-parameter
        with self.assertRaises(TypeError):
            asyncio.run(execute())  # pylint: disable=no-value-for-parameter

    def test_span_database_only_new_semconv(self):
        with use_semconv_opt_in("database"):
            conn = self._make_execute_conn()
            spans = self._run_execute(conn)

        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.attributes[DB_SYSTEM_NAME], "postgresql")
        self.assertEqual(span.attributes[DB_NAMESPACE], "testdb")
        self.assertEqual(span.attributes[DB_QUERY_TEXT], "SELECT 1")
        self.assertEqual(span.attributes[SERVER_ADDRESS], "testhost")
        self.assertEqual(span.attributes[SERVER_PORT], 5432)
        self.assertNotIn(NET_PEER_NAME, span.attributes)
        self.assertNotIn(NET_PEER_PORT, span.attributes)

    def test_span_both_semconv(self):
        with use_semconv_opt_in("database/dup,http/dup"):
            conn = self._make_execute_conn()
            spans = self._run_execute(conn)

        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.attributes[DB_SYSTEM], "postgresql")
        self.assertEqual(span.attributes[DB_NAME], "testdb")
        self.assertEqual(span.attributes[DB_STATEMENT], "SELECT 1")
        self.assertEqual(span.attributes[DB_USER], "testuser")
        self.assertEqual(span.attributes[NET_PEER_NAME], "testhost")
        self.assertEqual(span.attributes[NET_PEER_PORT], 5432)
        self.assertEqual(span.attributes[DB_SYSTEM_NAME], "postgresql")
        self.assertEqual(span.attributes[DB_NAMESPACE], "testdb")
        self.assertEqual(span.attributes[DB_QUERY_TEXT], "SELECT 1")
        self.assertEqual(span.attributes[SERVER_ADDRESS], "testhost")
        self.assertEqual(span.attributes[SERVER_PORT], 5432)

    def test_span_new_semconv(self):
        with use_semconv_opt_in("database,http"):
            conn = self._make_execute_conn()
            spans = self._run_execute(conn)

        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.attributes[DB_SYSTEM_NAME], "postgresql")
        self.assertEqual(span.attributes[DB_NAMESPACE], "testdb")
        self.assertEqual(span.attributes[DB_QUERY_TEXT], "SELECT 1")
        self.assertEqual(span.attributes[SERVER_ADDRESS], "testhost")
        self.assertEqual(span.attributes[SERVER_PORT], 5432)

    def test_span_unix_socket_default_semconv(self):
        conn = self._make_execute_conn(addr="/var/run/postgresql/.s.PGSQL.5432")
        spans = self._run_execute(conn)

        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(
            span.attributes[NET_PEER_NAME],
            "/var/run/postgresql/.s.PGSQL.5432",
        )
        self.assertEqual(span.attributes[NET_TRANSPORT], NetTransportValues.OTHER.value)
        self.assertNotIn(NET_PEER_PORT, span.attributes)
        self.assertNotIn(SERVER_ADDRESS, span.attributes)

    def test_span_unix_socket_new_semconv(self):
        with use_semconv_opt_in("database,http"):
            conn = self._make_execute_conn(addr="/var/run/postgresql/.s.PGSQL.5432")
            spans = self._run_execute(conn)

        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(
            span.attributes[SERVER_ADDRESS],
            "/var/run/postgresql/.s.PGSQL.5432",
        )
        self.assertEqual(
            span.attributes[NETWORK_TRANSPORT],
            NetworkTransportValues.UNIX.value,
        )
        self.assertNotIn(SERVER_PORT, span.attributes)

    def test_span_capture_parameters(self):
        apg = AsyncPGInstrumentor(capture_parameters=True)
        apg.instrument(tracer_provider=self.tracer_provider)
        conn = self._make_execute_conn()

        async def do_execute():
            execute = asyncpg.connection.Connection.execute.__get__(  # pylint: disable=no-value-for-parameter
                conn, asyncpg.connection.Connection
            )
            await execute("SELECT $1", "hello")

        asyncio.run(do_execute())
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        self.assertIn("db.statement.parameters", spans[0].attributes)
        self.assertIn("hello", spans[0].attributes["db.statement.parameters"])

    def _run_execute_error(self, semconv_mode):
        async def error_mock(*args, **kwargs):
            raise RuntimeError("db error")

        conn = mock.Mock()
        conn.is_closed = lambda: False
        conn._pool_release_ctr = 0
        conn._query_loggers = []
        conn._execute = error_mock
        conn._protocol = mock.Mock()
        conn._protocol.query = error_mock
        conn._addr = ("testhost", 5432)
        params = mock.Mock()
        params.database = "testdb"
        params.user = "testuser"
        conn._params = params

        with use_semconv_opt_in(semconv_mode):
            apg = AsyncPGInstrumentor()
            apg.instrument(tracer_provider=self.tracer_provider)

            async def do_execute():
                execute = asyncpg.connection.Connection.execute.__get__(  # pylint: disable=no-value-for-parameter
                    conn, asyncpg.connection.Connection
                )
                await execute("SELECT 1")

            with self.assertRaises(RuntimeError):
                asyncio.run(do_execute())

        return self.memory_exporter.get_finished_spans()

    def test_error_type_not_set_on_default_semconv(self):
        spans = self._run_execute_error("")
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertFalse(span.status.is_ok)
        self.assertNotIn(ERROR_TYPE, span.attributes)

    def test_error_type_set_on_new_db_semconv(self):
        spans = self._run_execute_error("database")
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertFalse(span.status.is_ok)
        self.assertEqual(span.attributes[ERROR_TYPE], "RuntimeError")

    def test_error_type_set_on_both_semconv(self):
        spans = self._run_execute_error("database/dup,http/dup")
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertFalse(span.status.is_ok)
        self.assertEqual(span.attributes[ERROR_TYPE], "RuntimeError")


class TestAsyncPGOperationDuration(TestBase):
    """Tests for the db.client.operation.duration histogram."""

    def setUp(self):
        super().setUp()
        _OpenTelemetrySemanticConventionStability._initialized = False

    def tearDown(self):
        super().tearDown()
        AsyncPGInstrumentor().uninstrument()
        _OpenTelemetrySemanticConventionStability._initialized = False

    @staticmethod
    def _make_conn(addr=("testhost", 5432), error=None):
        async def protocol_query(*args, **kwargs):
            if error is not None:
                raise error
            return b"SELECT 1", [], True

        conn = mock.Mock()
        conn.is_closed = lambda: False
        conn._pool_release_ctr = 0
        conn._query_loggers = []
        conn._protocol = mock.Mock()
        conn._protocol.query = protocol_query
        conn._addr = addr
        params = mock.Mock()
        params.database = "testdb"
        params.user = "testuser"
        conn._params = params
        return conn

    def _execute(self, conn, query="SELECT 1"):
        AsyncPGInstrumentor().instrument(tracer_provider=self.tracer_provider)

        async def do_execute():
            execute = asyncpg.connection.Connection.execute.__get__(  # pylint: disable=no-value-for-parameter
                conn, asyncpg.connection.Connection
            )
            await execute(query)

        asyncio.run(do_execute())

    def _duration_points(self):
        metrics = [metric for metric in self.get_sorted_metrics() if metric.name == DB_CLIENT_OPERATION_DURATION]
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].unit, "s")
        return list(metrics[0].data.data_points)

    def test_duration_recorded_on_a_successful_query(self):
        self._execute(self._make_conn())

        points = self._duration_points()
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].count, 1)
        self.assertGreaterEqual(points[0].sum, 0.0)

        attributes = dict(points[0].attributes)
        self.assertEqual(
            attributes,
            {
                DB_SYSTEM_NAME: "postgresql",
                DB_OPERATION_NAME: "SELECT",
                DB_NAMESPACE: "testdb",
                SERVER_ADDRESS: "testhost",
                SERVER_PORT: 5432,
            },
        )
        self.assertIs(type(attributes[SERVER_PORT]), int)
        for key in (DB_SYSTEM_NAME, DB_OPERATION_NAME, DB_NAMESPACE, SERVER_ADDRESS):
            self.assertIs(type(attributes[key]), str)

    def test_duration_recorded_in_every_semconv_mode(self):
        for mode in ("", "database", "database/dup"):
            with self.subTest(mode=mode), use_semconv_opt_in(mode):
                self._execute(self._make_conn())
                attributes = dict(self._duration_points()[0].attributes)
                self.assertEqual(attributes[DB_SYSTEM_NAME], "postgresql")
                self.assertEqual(attributes[DB_OPERATION_NAME], "SELECT")
                AsyncPGInstrumentor().uninstrument()

    def test_duration_recorded_with_error_type_on_a_failed_query(self):
        error = RuntimeError("db error")
        with self.assertRaises(RuntimeError) as raised:
            self._execute(self._make_conn(error=error))
        self.assertIs(raised.exception, error)

        points = self._duration_points()
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].count, 1)
        attributes = dict(points[0].attributes)
        self.assertEqual(attributes[ERROR_TYPE], "RuntimeError")
        self.assertEqual(attributes[DB_OPERATION_NAME], "SELECT")

    def test_error_type_is_recorded_even_when_the_span_omits_it(self):
        """The span gates error.type on the opt-in mode; the histogram does not."""
        with use_semconv_opt_in(""):
            with self.assertRaises(RuntimeError):
                self._execute(self._make_conn(error=RuntimeError("db error")))

        span = self.memory_exporter.get_finished_spans()[0]
        self.assertNotIn(ERROR_TYPE, span.attributes)
        self.assertEqual(dict(self._duration_points()[0].attributes)[ERROR_TYPE], "RuntimeError")

    def test_server_port_is_absent_for_a_unix_socket_connection(self):
        self._execute(self._make_conn(addr="/var/run/postgresql/.s.PGSQL.5432"))

        attributes = dict(self._duration_points()[0].attributes)
        self.assertEqual(attributes[SERVER_ADDRESS], "/var/run/postgresql/.s.PGSQL.5432")
        self.assertNotIn(SERVER_PORT, attributes)

    def test_nothing_recorded_while_instrumentation_is_suppressed(self):
        conn = self._make_conn()
        with suppress_instrumentation():
            self._execute(conn)

        self.assertEqual(self.get_sorted_metrics(), [])

    def test_an_exhausted_cursor_is_recorded_without_error_type(self):
        async def bind_mock(*args, **kwargs):
            return []

        async def exec_mock(*args, **kwargs):
            return [], None, True

        conn = mock.Mock()
        conn.is_closed = lambda: False
        conn._protocol = mock.Mock()
        conn._protocol.bind = bind_mock
        conn._protocol.execute = exec_mock
        conn._protocol.bind_execute = exec_mock
        conn._protocol.close_portal = bind_mock
        conn._addr = ("testhost", 5432)
        params = mock.Mock()
        params.database = "testdb"
        conn._params = params

        state = mock.Mock()
        state.closed = False

        AsyncPGInstrumentor().instrument(tracer_provider=self.tracer_provider)
        crs_iter = cursor.CursorIterator(conn, "SELECT * FROM test", state, [], Record, 1, 1)
        with pytest.raises(StopAsyncIteration):
            asyncio.run(anext(crs_iter))

        points = self._duration_points()
        self.assertEqual(len(points), 1)
        attributes = dict(points[0].attributes)
        self.assertNotIn(ERROR_TYPE, attributes)
        self.assertEqual(attributes[DB_OPERATION_NAME], "SELECT")

    def test_duration_recorded_for_a_prepared_statement(self):
        async def bind_execute_mock(*args, **kwargs):
            return [], b"SELECT 1", True

        conn = mock.Mock()
        conn._pool_release_ctr = 0
        conn.is_closed = lambda: False
        conn._protocol = mock.Mock()
        conn._protocol.bind_execute = bind_execute_mock
        conn._addr = ("testhost", 5432)
        params = mock.Mock()
        params.database = "testdb"
        conn._params = params

        state = mock.Mock()
        state.closed = False

        AsyncPGInstrumentor().instrument(tracer_provider=self.tracer_provider)
        stmt = PreparedStatement(conn, "SELECT * FROM users", state)
        asyncio.run(stmt.fetch())

        attributes = dict(self._duration_points()[0].attributes)
        self.assertEqual(attributes[DB_OPERATION_NAME], "SELECT")
        self.assertEqual(attributes[DB_NAMESPACE], "testdb")
        self.assertEqual(attributes[SERVER_ADDRESS], "testhost")
