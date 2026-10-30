# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from unittest import mock

import flask
from werkzeug.test import Client
from werkzeug.wrappers import Response

from opentelemetry.context import Context, attach, detach
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.test.wsgitestutil import WsgiTestBase

# pylint: disable=import-error
from .base_test import InstrumentationTest


class TestSQLCommenter(InstrumentationTest, WsgiTestBase):
    def setUp(self):
        super().setUp()
        FlaskInstrumentor().instrument()
        self.app = flask.Flask(__name__)
        self._common_initialization()

    def tearDown(self):
        super().tearDown()
        with self.disable_logging():
            FlaskInstrumentor().uninstrument()

    def test_sqlcommenter_enabled_default(self):
        self.app = flask.Flask(__name__)
        self.app.route("/sqlcommenter")(self._sqlcommenter_endpoint)
        client = Client(self.app, Response)

        resp = client.get("/sqlcommenter")
        self.assertEqual(200, resp.status_code)
        self.assertRegex(
            list(resp.response)[0].strip(),
            b'{"controller":"_sqlcommenter_endpoint","framework":"flask:(.*)","route":"/sqlcommenter"}',
        )

    def test_sqlcommenter_enabled_with_configurations(self):
        FlaskInstrumentor().uninstrument()
        FlaskInstrumentor().instrument(enable_commenter=True, commenter_options={"route": False})

        self.app = flask.Flask(__name__)
        self.app.route("/sqlcommenter")(self._sqlcommenter_endpoint)
        client = Client(self.app, Response)

        resp = client.get("/sqlcommenter")
        self.assertEqual(200, resp.status_code)
        self.assertRegex(
            list(resp.response)[0].strip(),
            b'{"controller":"_sqlcommenter_endpoint","framework":"flask:(.*)"}',
        )

    def test_sqlcommenter_context_is_detached(self):
        """The context carrying the commenter values is detached again.

        The token returned by ``context.attach`` used to be discarded, so the
        commenter context was undone only as a side effect of detaching the
        older span token, which resets the context variable past it. This
        asserts that the commenter token itself reaches ``context.detach``.
        """
        self.app = flask.Flask(__name__)
        self.app.route("/sqlcommenter")(self._sqlcommenter_endpoint)
        client = Client(self.app, Response)

        attached: list[tuple[object, Context]] = []
        detached: list[object] = []

        def recording_attach(context: Context) -> object:
            token: object = attach(context)
            attached.append((token, context))
            return token

        def recording_detach(token: object) -> None:
            detached.append(token)
            detach(token)

        with (
            mock.patch("opentelemetry.context.attach", recording_attach),
            mock.patch("opentelemetry.context.detach", recording_detach),
        ):
            resp = client.get("/sqlcommenter")

        self.assertEqual(200, resp.status_code)
        commenter_tokens: list[object] = [
            token for token, context in attached if context.get("SQLCOMMENTER_ORM_TAGS_AND_VALUES") is not None
        ]
        self.assertEqual(1, len(commenter_tokens))
        self.assertTrue(any(token is commenter_tokens[0] for token in detached))

    def test_sqlcommenter_disabled(self):
        FlaskInstrumentor().uninstrument()
        FlaskInstrumentor().instrument(enable_commenter=False)

        self.app = flask.Flask(__name__)
        self.app.route("/sqlcommenter")(self._sqlcommenter_endpoint)
        client = Client(self.app, Response)

        resp = client.get("/sqlcommenter")
        self.assertEqual(200, resp.status_code)
        self.assertEqual(list(resp.response)[0].strip(), b"{}")
