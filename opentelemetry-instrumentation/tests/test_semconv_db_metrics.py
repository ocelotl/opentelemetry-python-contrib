# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from unittest import TestCase
from unittest.mock import Mock

from opentelemetry.instrumentation._semconv import (
    _db_client_operation_duration_attributes,
    _record_db_client_operation_duration,
)
from opentelemetry.semconv.attributes.db_attributes import (
    DB_NAMESPACE,
    DB_OPERATION_NAME,
    DB_SYSTEM_NAME,
)
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.semconv.attributes.server_attributes import (
    SERVER_ADDRESS,
    SERVER_PORT,
)


class TestDbClientOperationDuration(TestCase):
    def test_attributes_omit_what_the_caller_does_not_know(self):
        self.assertEqual(
            _db_client_operation_duration_attributes("postgresql"),
            {DB_SYSTEM_NAME: "postgresql"},
        )

    def test_attributes_carry_every_value_the_caller_supplies(self):
        attributes = _db_client_operation_duration_attributes(
            "postgresql",
            operation_name="SELECT",
            namespace="testdb",
            server_address="localhost",
            server_port="5432",
        )
        self.assertEqual(
            attributes,
            {
                DB_SYSTEM_NAME: "postgresql",
                DB_OPERATION_NAME: "SELECT",
                DB_NAMESPACE: "testdb",
                SERVER_ADDRESS: "localhost",
                SERVER_PORT: 5432,
            },
        )
        self.assertIsInstance(attributes[SERVER_PORT], int)

    def test_a_successful_block_is_recorded_without_error_type(self):
        histogram = Mock()
        attributes = {DB_SYSTEM_NAME: "redis"}

        with _record_db_client_operation_duration(histogram, attributes):
            pass

        histogram.record.assert_called_once()
        duration, kwargs = (
            histogram.record.call_args.args[0],
            histogram.record.call_args.kwargs,
        )
        self.assertGreaterEqual(duration, 0)
        self.assertEqual(kwargs["attributes"], {DB_SYSTEM_NAME: "redis"})

    def test_a_failed_block_is_recorded_with_error_type(self):
        histogram = Mock()
        attributes = {DB_SYSTEM_NAME: "redis"}
        raised = ValueError("boom")

        with self.assertRaises(ValueError) as caught:
            with _record_db_client_operation_duration(histogram, attributes):
                raise raised

        self.assertIs(caught.exception, raised)
        histogram.record.assert_called_once()
        self.assertEqual(
            histogram.record.call_args.kwargs["attributes"],
            {DB_SYSTEM_NAME: "redis", ERROR_TYPE: "ValueError"},
        )

    def test_a_cancelled_block_is_recorded_without_error_type(self):
        histogram = Mock()
        attributes = {DB_SYSTEM_NAME: "redis"}

        with self.assertRaises(KeyboardInterrupt):
            with _record_db_client_operation_duration(histogram, attributes):
                raise KeyboardInterrupt

        histogram.record.assert_called_once()
        self.assertEqual(
            histogram.record.call_args.kwargs["attributes"],
            {DB_SYSTEM_NAME: "redis"},
        )
