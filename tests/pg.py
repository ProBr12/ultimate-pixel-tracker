"""A throwaway local Postgres database per test run (never production).

TEST_PG_URL points at a local server, e.g. the postgres:16 Docker image:
  docker run -d -e POSTGRES_USER=hub -e POSTGRES_PASSWORD=hub -p 55432:5432 postgres:16
"""
import os
from urllib.parse import urlparse

import psycopg2

BASE_URL = os.environ.get("TEST_PG_URL", "postgresql://hub:hub@localhost:55432/hub")


def fresh_database(name):
    host = urlparse(BASE_URL).hostname
    assert host in ("localhost", "127.0.0.1"), "tests only ever run against a local Postgres"
    conn = psycopg2.connect(BASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    return BASE_URL.rsplit("/", 1)[0] + "/" + name
