# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import functools
from typing import Any, Callable, Generator, Optional, TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy.engine import Engine

from superset import db
from superset.extensions import feature_flag_manager
from superset.utils.core import json_dumps_w_dates
from superset.utils.database import get_example_database, remove_database
from tests.integration_tests.test_app import app, login

if TYPE_CHECKING:
    from flask.testing import FlaskClient

    from superset.connectors.sqla.models import Database

CTAS_SCHEMA_NAME = "sqllab_test_db"
ADMIN_SCHEMA_NAME = "admin_database"


@pytest.fixture
def app_context():
    with app.app_context():
        yield


@pytest.fixture(autouse=True, scope="session")
def setup_sample_data() -> Any:
    # TODO(john-bodley): Determine a cleaner way of setting up the sample data without
    # relying on `tests.integration_tests.test_app.app` leveraging an  `app` fixture which is purposely
    # scoped to the function level to ensure tests remain idempotent.
    with app.app_context():
        setup_presto_if_needed()

        from superset.cli.test import load_test_users_run

        load_test_users_run()

        from superset.examples.css_templates import load_css_templates

        load_css_templates()

    yield

    with app.app_context():
        engine = get_example_database().get_sqla_engine()

        # drop sqlachemy tables

        db.session.commit()
        from sqlalchemy.ext import declarative

        sqla_base = declarative.declarative_base()
        # uses sorted_tables to drop in proper order without violating foreign constrains
        for table in sqla_base.metadata.sorted_tables:
            table.__table__.drop()
        db.session.commit()


@pytest.fixture
def login_as(test_client: "FlaskClient[Any]"):
    """Fixture with app context and logged in admin user."""

    def _login_as(username: str, password: str = "general"):
        login(test_client, username=username, password=password)

    yield _login_as
    # no need to log out as both app_context and test_client are
    # function level fixtures anyway


@pytest.fixture
def login_as_admin(login_as: Callable[..., None]):
    yield login_as("admin")


def drop_from_schema(engine: Engine, schema_name: str):
    schemas = engine.execute(f"SHOW SCHEMAS").fetchall()
    if schema_name not in [s[0] for s in schemas]:
        # schema doesn't exist
        return
    tables_or_views = engine.execute(f"SHOW TABLES in {schema_name}").fetchall()
    for tv in tables_or_views:
        engine.execute(f"DROP TABLE IF EXISTS {schema_name}.{tv[0]}")
        engine.execute(f"DROP VIEW IF EXISTS {schema_name}.{tv[0]}")


@pytest.fixture(scope="session")
def example_db_provider() -> Callable[[], Database]:  # type: ignore
    class _example_db_provider:
        _db: Optional[Database] = None

        def __call__(self) -> Database:
            with app.app_context():
                if self._db is None:
                    self._db = get_example_database()
                    self._load_lazy_data_to_decouple_from_session()

                return self._db

        def _load_lazy_data_to_decouple_from_session(self) -> None:
            self._db.get_sqla_engine()  # type: ignore
            self._db.backend  # type: ignore

        def remove(self) -> None:
            if self._db:
                with app.app_context():
                    remove_database(self._db)

    _instance = _example_db_provider()

    yield _instance

    # TODO - can not use it until referenced objects will be deleted.
    # _instance.remove()


def setup_presto_if_needed():
    backend = app.config["SQLALCHEMY_EXAMPLES_URI"].split("://")[0]
    database = get_example_database()
    extra = database.get_extra()

    if backend == "presto":
        # decrease poll interval for tests
        extra = {
            **extra,
            "engine_params": {
                "connect_args": {"poll_interval": app.config["PRESTO_POLL_INTERVAL"]}
            },
        }
    else:
        # remove `poll_interval` from databases that do not support it
        extra = {**extra, "engine_params": {}}
    database.extra = json_dumps_w_dates(extra)
    db.session.commit()

    if backend in {"presto", "hive"}:
        database = get_example_database()
        engine = database.get_sqla_engine()
        drop_from_schema(engine, CTAS_SCHEMA_NAME)
        engine.execute(f"DROP SCHEMA IF EXISTS {CTAS_SCHEMA_NAME}")
        engine.execute(f"CREATE SCHEMA {CTAS_SCHEMA_NAME}")

        drop_from_schema(engine, ADMIN_SCHEMA_NAME)
        engine.execute(f"DROP SCHEMA IF EXISTS {ADMIN_SCHEMA_NAME}")
        engine.execute(f"CREATE SCHEMA {ADMIN_SCHEMA_NAME}")


def with_feature_flags(**mock_feature_flags):
    """
    Use this decorator to mock feature flags in tests.integration_tests.

    Usage:

        class TestYourFeature(SupersetTestCase):

            @with_feature_flags(YOUR_FEATURE=True)
            def test_your_feature_enabled(self):
                self.assertEqual(is_feature_enabled("YOUR_FEATURE"), True)

            @with_feature_flags(YOUR_FEATURE=False)
            def test_your_feature_disabled(self):
                self.assertEqual(is_feature_enabled("YOUR_FEATURE"), False)
    """

    def mock_get_feature_flags():
        feature_flags = feature_flag_manager._feature_flags or {}
        return {**feature_flags, **mock_feature_flags}

    def decorate(test_fn):
        def wrapper(*args, **kwargs):
            with patch.object(
                feature_flag_manager,
                "get_feature_flags",
                side_effect=mock_get_feature_flags,
            ):
                test_fn(*args, **kwargs)

        return functools.update_wrapper(wrapper, test_fn)

    return decorate
