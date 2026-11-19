"""Migração do SQLite (versão anterior) para o PostgreSQL. Usa o arquivo real de teste tests/fixtures/etapa1.db."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "etapa1.db"

_spec = importlib.util.spec_from_file_location("migrar", ROOT / "scripts" / "migrar_sqlite_para_postgres.py")
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_copia_preserva_dados_e_ids(pg_url, schema, consulta):
    relatorio = mig.migrar(FIXTURE, pg_url, schema)
    assert relatorio["tabelas"]["clientes"] == {"origem": 2, "destino": 2}
    assert relatorio["tabelas"]["pagamentos_mensais"] == {"origem": 2, "destino": 2}
    assert relatorio["valor_total_centavos"]["pagamentos_mensais"]["origem"] == \
        relatorio["valor_total_centavos"]["pagamentos_mensais"]["destino"] == 175_000

    clientes = consulta("SELECT id, codigo, cnpj, razao_social, cidade FROM clientes ORDER BY id")
    assert len(clientes) == 2 and clientes[0][1] == "10" and clientes[0][2] == "11222333000181"
    pagamentos = consulta("SELECT cliente_id, mes_referencia, valor_centavos, classificacao "
                          "FROM pagamentos_mensais ORDER BY cliente_id, mes_referencia")
    assert len(pagamentos) == 2
    assert all(p[0] in {c[0] for c in clientes} for p in pagamentos)  # relacionamentos íntegros


def test_arquivo_de_origem_nao_e_alterado(pg_url, schema, tmp_path):
    origem = tmp_path / "antigo.db"
    origem.write_bytes(FIXTURE.read_bytes())
    antes = _hash(origem)
    mig.migrar(origem, pg_url, schema)
    assert _hash(origem) == antes
    assert not (tmp_path / "antigo.db-wal").exists()


def test_aplicacao_funciona_com_os_dados_migrados(pg_url, schema, client):
    mig.migrar(FIXTURE, pg_url, schema)
    lista = client.get("/api/clientes", query_string={"mes": "2026-11"}).json["clientes"]
    assert {c["codigo"] for c in lista} == {"10", "20"}
    detalhe = client.get(f"/api/clientes/{lista[0]['id']}").json
    assert detalhe["cnpj"] and detalhe["pagamentos"]


def test_novos_registros_nao_colidem_com_ids_migrados(pg_url, schema, client):
    mig.migrar(FIXTURE, pg_url, schema)
    maior = max(c["id"] for c in client.get("/api/clientes").json["clientes"])
    res = client.post("/api/clientes", json={"codigo": "99", "nome_fantasia": "Novo", "valor": "10", "mes": "2026-11"})
    assert res.status_code == 201
    assert res.json["id"] > maior


def test_destino_com_dados_exige_substituir(pg_url, schema):
    mig.migrar(FIXTURE, pg_url, schema)
    with pytest.raises(mig.MigracaoError, match="--substituir"):
        mig.migrar(FIXTURE, pg_url, schema)


def test_substituir_copia_de_novo_sem_duplicar(pg_url, schema, consulta):
    mig.migrar(FIXTURE, pg_url, schema)
    relatorio = mig.migrar(FIXTURE, pg_url, schema, substituir=True)
    assert relatorio["tabelas"]["clientes"] == {"origem": 2, "destino": 2}
    assert consulta("SELECT COUNT(*) FROM clientes")[0][0] == 2
    assert consulta("SELECT COUNT(*) FROM pagamentos_mensais")[0][0] == 2


def test_arquivo_inexistente_e_recusado(pg_url, schema):
    with pytest.raises(mig.MigracaoError, match="não encontrado"):
        mig.migrar(FIXTURE.with_name("nao_existe.db"), pg_url, schema)
