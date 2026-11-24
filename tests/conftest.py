from dataclasses import replace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from po_intake.config import Settings
from po_intake.db import make_engine
from po_intake.seed import seed


@pytest.fixture
def postgres_schema(tmp_path, monkeypatch):
    name = "test_" + uuid4().hex
    admin = make_engine(Settings().database_url)
    with admin.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {name}"))
    url = make_url(Settings().database_url).update_query_dict({"options": f"-csearch_path={name}"})
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    settings = replace(Settings(), storage=tmp_path, foreground=True)
    engine = make_engine(settings.database_url)
    try:
        yield settings, engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {name} CASCADE"))
        admin.dispose()


@pytest.fixture
def database(postgres_schema):
    settings, engine = postgres_schema
    command.upgrade(Config("alembic.ini"), "head")
    seed(engine)
    return settings, engine
