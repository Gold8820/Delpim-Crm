"""Fixtures compartilhadas. Os testes rodam contra um PostgreSQL de TESTE.

Defina a variável TEST_DATABASE_URL (ou DATABASE_URL) apontando para um banco de teste.
Cada teste cria um schema temporário próprio e o apaga ao final. Nunca aponte para o banco de produção.

    TEST_DATABASE_URL=postgresql://usuario:senha@host:5432/postgres python -m pytest -q
"""
import os
import uuid

import psycopg
import pytest

from clientes import create_app
from clientes.db import connect


@pytest.fixture(scope="session")
def pg_url():
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail("Defina TEST_DATABASE_URL com a conexão de um PostgreSQL de TESTE "
                    "(os testes criam e apagam schemas temporários).")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    return url


@pytest.fixture()
def schema(pg_url):
    name = "t_" + uuid.uuid4().hex[:12]
    yield name
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


@pytest.fixture()
def app(pg_url, schema, tmp_path):
    return create_app(pg_url, schema=schema)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def consulta(app):
    """Consulta direta ao banco do teste: consulta("SELECT ...", params) -> lista de linhas."""
    def run(sql, params=()):
        conn = connect(app.config["DATABASE_URL"], app.config["DB_SCHEMA"])
        try:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall() if cursor.description is not None else []
            conn.commit()  # confirma também comandos de escrita feitos pelo teste (UPDATE/DELETE)
            return rows
        finally:
            conn.close()
    return run


@pytest.fixture()
def reabrir(app):
    """Recria a aplicação sobre o mesmo banco (simula reiniciar o sistema)."""
    def run():
        return create_app(app.config["DATABASE_URL"], schema=app.config["DB_SCHEMA"])
    return run
