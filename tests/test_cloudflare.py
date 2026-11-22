"""Testes do modo Cloudflare Workers (src/worker.py -> create_app(cloudflare=True)).

O runtime do Worker é simulado: o `env` do Worker vai em request.environ["workers.env"] (é o que o
adaptador `workers.wsgi` faz), com um binding HYPERDRIVE falso que aponta para o PostgreSQL de TESTE.

Como na produção, o Worker conecta com um usuário de privilégio mínimo (só SELECT/INSERT/UPDATE/DELETE,
sem DDL), com search_path definido no próprio usuário; o esquema é criado à parte, pelo usuário de migração.
Driver: pg8000 (o mesmo do Pyodide).
"""
import base64
import io
import json
import re
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import psycopg
import pytest

from clientes import create_app
from clientes.db import DriverError, init_db

from test_duas_planilhas import CNPJ_A, CNPJ_B, clientes_xlsx, contratos_xlsx

ROOT = Path(__file__).resolve().parent.parent
SENHA = "senha-de-teste-123"


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


AUTH = basic("admin", SENHA)


# ============================================================================ ambiente simulado

@pytest.fixture()
def banco_worker(pg_url):
    """Schema criado pelo 'usuário de migração' + usuário da aplicação só com DML (como no Supabase)."""
    sufixo = uuid.uuid4().hex[:10]
    schema, role = f"cf_{sufixo}", f"cf_app_{sufixo}"
    init_db(pg_url, schema)  # migração: separada da aplicação
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'app-teste'")
        conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
        conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {role}")
        conn.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role}")
        conn.execute(f"ALTER ROLE {role} SET search_path = {schema}, public, extensions")
    parts = urlsplit(pg_url)
    binding = SimpleNamespace(host=parts.hostname, port=str(parts.port or 5432), user=role,
                              password="app-teste", database=(parts.path or "/postgres").lstrip("/"),
                              connectionString="nao-usado")
    yield SimpleNamespace(schema=schema, role=role, binding=binding, admin_url=pg_url)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{role}'")
        conn.execute(f"DROP OWNED BY {role}")
        conn.execute(f"DROP ROLE IF EXISTS {role}")


def _worker_env(banco, **extra):
    values = {"HYPERDRIVE": banco.binding, "ACCESS_USER": "admin", "ACCESS_PASSWORD": SENHA}
    values.update(extra)
    return SimpleNamespace(**values)


@pytest.fixture()
def cf_app(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ACCESS_PASSWORD", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    return create_app(cloudflare=True)


@pytest.fixture()
def worker(cf_app, banco_worker):
    """Cliente HTTP do Worker simulado, já com usuário e senha."""
    c = cf_app.test_client()
    c.environ_base["workers.env"] = _worker_env(banco_worker)
    c.environ_base["HTTP_AUTHORIZATION"] = AUTH["Authorization"]
    return c


def consulta_admin(banco, sql, params=()):
    with psycopg.connect(banco.admin_url, options=f"-c search_path={banco.schema},public") as conn:
        return conn.execute(sql, params).fetchall()


# ============================================================================ inicialização e configuração

def test_modo_cloudflare_nao_exige_database_url_nem_altera_o_esquema(cf_app):
    assert cf_app.config["CLOUDFLARE"] is True
    assert cf_app.config["DATABASE_URL"] is None
    assert cf_app.static_folder is None   # estáticos vêm do Workers Static Assets


def test_aplicacao_do_worker_carrega_sem_psycopg():
    """No Pyodide não existe psycopg (depende da libpq). O pacote precisa importar sem ele."""
    codigo = ("import sys; sys.modules['psycopg'] = None\n"
              "from clientes import create_app\n"
              "import clientes.db as db\n"
              "assert db.psycopg is None\n"
              "app = create_app(cloudflare=True)\n"
              "print('ok', sorted(r.rule for r in app.url_map.iter_rules())[:3])")
    res = subprocess.run([sys.executable, "-c", codigo], cwd=ROOT, capture_output=True, text=True,
                         env={"PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("ok")


def test_worker_py_usa_o_adaptador_wsgi_oficial():
    texto = (ROOT / "src" / "worker.py").read_text(encoding="utf-8")
    assert "from workers import wsgi" in texto
    assert "Default = wsgi.entrypoint(app)" in texto
    assert "create_app(cloudflare=True)" in texto


CF = ROOT / "cloudflare"


def _wrangler():
    texto = (CF / "wrangler.jsonc").read_text(encoding="utf-8")
    sem_comentarios = re.sub(r"^\s*//.*$", "", texto, flags=re.M)
    return json.loads(sem_comentarios)


def test_wrangler_configura_python_workers_hyperdrive_e_estaticos():
    cfg = _wrangler()
    assert cfg["main"] == "build/worker.py" and cfg["build"]["command"] == "node build.mjs"
    assert "python_workers" in cfg["compatibility_flags"]
    assert date.fromisoformat(cfg["compatibility_date"]) >= date(2026, 9, 8)   # Hyperdrive no Python Workers
    assert [h["binding"] for h in cfg["hyperdrive"]] == ["HYPERDRIVE"]
    assert cfg["assets"] == {"directory": "../static", "binding": "ASSETS", "run_worker_first": True}
    assert [g for r in cfg["rules"] for g in r["globs"]] == ["templates/**/*.html"]


def test_build_copia_somente_o_codigo_do_sistema():
    """O pacote do Worker leva só src/worker.py, clientes/ e templates/ (sem testes, scripts, dados ou .env)."""
    texto = (CF / "build.mjs").read_text(encoding="utf-8")
    copias = re.findall(r'cpSync\(join\(root, ([^)]*)\)', texto)
    assert copias == ['"src", "worker.py"', '"clientes"', '"templates"']


def test_wrangler_nao_tem_segredos():
    cfg = _wrangler()
    assert set(cfg.get("vars", {})) <= {"ACCESS_USER"}
    texto = (CF / "wrangler.jsonc").read_text(encoding="utf-8")
    assert "postgres://" not in texto and "postgresql://" not in texto
    assert "ACCESS_PASSWORD\":" not in texto


def test_dependencias_do_worker_sao_compativeis_com_pyodide():
    import tomllib

    deps = tomllib.loads((CF / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    nomes = {re.split(r"[<>=\[ ]", d, maxsplit=1)[0].lower() for d in deps}
    assert nomes == {"flask", "openpyxl", "defusedxml", "pg8000"}   # todas em Python puro
    assert not any("psycopg" in d for d in deps)


# ============================================================================ acesso protegido

def test_sem_senha_configurada_no_worker_nada_e_servido(cf_app, banco_worker):
    c = cf_app.test_client()
    c.environ_base["workers.env"] = _worker_env(banco_worker, ACCESS_PASSWORD=None)
    for rota in ("/", "/api/clientes", "/api/meta", "/static/app.js"):
        assert c.get(rota, headers=AUTH).status_code == 503, rota


@pytest.mark.parametrize("rota,metodo", [("/", "get"), ("/api/clientes", "get"), ("/api/meta", "get"),
                                         ("/api/clientes", "post"), ("/api/importacao/arquivo", "post"),
                                         ("/api/importacao/previa", "post"), ("/api/importacao/confirmar", "post"),
                                         ("/api/configuracoes/curva-abc", "put"), ("/static/app.js", "get")])
def test_todas_as_rotas_exigem_login(cf_app, banco_worker, rota, metodo):
    c = cf_app.test_client()
    c.environ_base["workers.env"] = _worker_env(banco_worker)
    res = getattr(c, metodo)(rota)
    assert res.status_code == 401
    assert res.headers["WWW-Authenticate"].startswith("Basic")
    assert getattr(c, metodo)(rota, headers=basic("admin", "errada")).status_code == 401
    assert getattr(c, metodo)(rota, headers=basic("outro", SENHA)).status_code == 401


def test_usuario_e_senha_vem_dos_secrets_do_worker(cf_app, banco_worker):
    c = cf_app.test_client()
    c.environ_base["workers.env"] = _worker_env(banco_worker, ACCESS_USER="gestor", ACCESS_PASSWORD="outra-senha")
    assert c.get("/api/clientes", headers=AUTH).status_code == 401
    assert c.get("/api/clientes", headers=basic("gestor", "outra-senha")).status_code == 200


def test_healthz_aberto_e_sem_dados(cf_app, banco_worker):
    c = cf_app.test_client()
    c.environ_base["workers.env"] = _worker_env(banco_worker)
    res = c.get("/healthz")
    assert res.status_code == 200 and res.json == {"status": "ok"}


def test_arquivos_estaticos_vem_do_assets_depois_do_login(cf_app, banco_worker, monkeypatch):
    pedidos = []

    def falso(nome):
        pedidos.append(nome)
        return 200, b"body{}", "text/css; charset=utf-8"

    monkeypatch.setattr("clientes.cloudflare._fetch_asset", falso)
    c = cf_app.test_client()
    c.environ_base["workers.env"] = _worker_env(banco_worker)
    assert c.get("/static/style.css").status_code == 401 and pedidos == []
    res = c.get("/static/style.css", headers=AUTH)
    assert res.status_code == 200 and res.data == b"body{}" and pedidos == ["style.css"]
    assert res.headers["Cache-Control"] == "no-cache"
    pagina = c.get("/", headers=AUTH)
    assert pagina.status_code == 200
    assert b"/static/style.css?v=cf" in pagina.data and b"/static/app.js?v=cf" in pagina.data


# ============================================================================ banco via Hyperdrive

def test_conexao_pelo_binding_hyperdrive(worker, banco_worker):
    assert worker.get("/healthz").json == {"status": "ok"}
    res = worker.get("/api/meta")
    assert res.status_code == 200
    assert [f["classificacao"] for f in res.json["faixas"]] == ["A+", "A", "B", "C"]


def test_cada_requisicao_fecha_a_sua_conexao(worker, banco_worker):
    for _ in range(5):
        assert worker.get("/api/clientes").status_code == 200
    abertas = consulta_admin(banco_worker, "SELECT count(*) FROM pg_stat_activity WHERE usename = %s",
                             (banco_worker.role,))[0][0]
    assert abertas == 0


def test_usuario_da_aplicacao_nao_altera_o_esquema(banco_worker):
    from clientes.db import connect_hyperdrive

    conn = connect_hyperdrive(banco_worker.binding)
    try:
        with pytest.raises(DriverError):
            conn.execute("CREATE TABLE invasora (id int)")
        conn.rollback()
        with pytest.raises(DriverError):
            conn.execute("DROP TABLE clientes")
    finally:
        conn.close()


def test_erro_no_meio_da_requisicao_nao_deixa_transacao_aberta(worker, banco_worker, monkeypatch):
    import clientes.repository as repo

    def quebra(*a, **k):
        raise RuntimeError("falha simulada")

    worker.post("/api/clientes", json={"codigo": "X1", "razao_social": "Antes"})
    monkeypatch.setattr(repo, "upsert_payment", quebra)
    res = worker.post("/api/clientes", json={"codigo": "X2", "razao_social": "Depois", "valor": "500,00",
                                             "mes": "11/2026"})
    assert res.status_code == 500
    codigos = [r[0] for r in consulta_admin(banco_worker, "SELECT codigo FROM clientes ORDER BY codigo")]
    assert codigos == ["X1"]   # o cliente X2 não ficou gravado pela metade


# ============================================================================ funcionalidades pelo Worker

def test_cadastro_edicao_consulta_e_exclusao(worker):
    res = worker.post("/api/clientes", json={"codigo": "C01", "razao_social": "Alfa LTDA", "cnpj": CNPJ_A,
                                             "cidade": "Curitiba", "estado": "PR", "valor": "1.200,00",
                                             "mes": "11/2026", "observacoes": "Linha 1\nLinha 2 — ç"})
    assert res.status_code == 201, res.json
    cid = res.json["id"]
    assert res.json["observacoes"] == "Linha 1\nLinha 2 — ç"
    assert res.json["pagamentos"][0]["classificacao"] == "A+"

    res = worker.put(f"/api/clientes/{cid}", json={"codigo": "C01", "razao_social": "Alfa Comércio",
                                                   "cnpj": CNPJ_A, "valor": "250,00", "mes": "11/2026"})
    assert res.status_code == 200
    assert res.json["razao_social"] == "Alfa Comércio"
    assert res.json["pagamentos"][0]["classificacao"] == "B"

    dup = worker.post("/api/clientes", json={"codigo": "C02", "razao_social": "Outra", "cnpj": CNPJ_A})
    assert dup.status_code == 400 and "cnpj" in dup.json["campos"]

    busca = worker.get("/api/clientes", query_string={"q": "comercio"})
    assert busca.json["total"] == 1
    assert worker.delete(f"/api/clientes/{cid}").status_code == 200
    assert worker.get(f"/api/clientes/{cid}").status_code == 404


def _upload(c, conteudo, nome, papel):
    res = c.post("/api/importacao/arquivo", data={"arquivo": (io.BytesIO(conteudo), nome), "papel": papel},
                 content_type="multipart/form-data")
    assert res.status_code == 200, res.json
    return res.json


def _importar(c):
    cli = _upload(c, clientes_xlsx(), "clientes.xlsx", "clientes")
    con = _upload(c, contratos_xlsx(), "contratos.xlsx", "contratos")
    body = {"chave": "cnpj", "mes_padrao": "2026-11", "substituir_vazios": False,
            "arquivo_clientes": "clientes.xlsx", "arquivo_contratos": "contratos.xlsx",
            "clientes": {"token": cli["token"], "planilha": cli["planilha"], "mapeamento": cli["mapeamento"]},
            "contratos": {"token": con["token"], "planilha": con["planilha"], "mapeamento": con["mapeamento"]}}
    prev = c.post("/api/importacao/previa", json=body)
    assert prev.status_code == 200, prev.json
    done = c.post("/api/importacao/confirmar", json=body)
    assert done.status_code == 200, done.json
    return prev.json, done.json


def test_importacao_das_duas_planilhas_relaciona_e_nao_duplica(worker, banco_worker):
    prev, done = _importar(worker)
    assert prev["resumo"]["contratos_relacionados"] == 4
    contagem = lambda: consulta_admin(banco_worker, "SELECT (SELECT count(*) FROM clientes), "
                                      "(SELECT count(*) FROM contratos), (SELECT count(*) FROM contrato_valores), "
                                      "(SELECT count(*) FROM pagamentos_mensais)")[0]
    antes = contagem()
    assert antes[0] == 4 and antes[1] == 4
    # Cliente A tem dois contratos (600 + 500): o valor do mês é a soma, classificado como A+.
    alfa = consulta_admin(banco_worker, "SELECT p.valor_centavos, p.classificacao FROM pagamentos_mensais p "
                          "JOIN clientes c ON c.id = p.cliente_id WHERE c.cnpj = %s", (CNPJ_A,))
    assert alfa == [(110000, "A+")]
    # Reimportação idêntica (importação semanal): nada duplica.
    _importar(worker)
    assert contagem() == antes


def test_arquivo_enviado_fica_no_banco_entre_as_etapas(worker, banco_worker):
    cli = _upload(worker, clientes_xlsx(), "clientes.xlsx", "clientes")
    linhas = consulta_admin(banco_worker, "SELECT extensao, length(conteudo) FROM arquivos_importacao "
                            "WHERE token = %s", (cli["token"],))
    assert linhas and linhas[0][0] == ".xlsx" and linhas[0][1] > 0


def test_observacao_do_contrato_independente_da_do_cliente(worker):
    _importar(worker)
    alfa = worker.get("/api/clientes", query_string={"q": "alfa"}).json["clientes"][0]
    ficha = worker.get(f"/api/clientes/{alfa['id']}").json
    contrato = ficha["contratos"][0]
    res = worker.put(f"/api/clientes/{alfa['id']}/contratos/{contrato['id']}/observacoes",
                     json={"observacoes": "Renovar em março"})
    assert res.status_code == 200
    ficha = worker.get(f"/api/clientes/{alfa['id']}").json
    assert ficha["observacoes"] in (None, "")
    assert [c["observacoes"] for c in ficha["contratos"] if c["id"] == contrato["id"]] == ["Renovar em março"]


def test_curva_abc_editavel_reclassifica_pelo_valor_pago(worker):
    worker.post("/api/clientes", json={"codigo": "K1", "razao_social": "Kappa", "valor": "390,00", "mes": "10/2026"})
    atual = worker.get("/api/configuracoes/curva-abc").json["faixas"]
    assert [(f["classificacao"], f["minimo"], f["maximo"]) for f in atual] == [
        ("A+", "1.000,00", ""), ("A", "400,00", "999,99"), ("B", "180,00", "399,99"), ("C", "0,00", "179,99")]
    novas = [{"classificacao": "A+", "minimo": "1.000,00", "maximo": ""},
             {"classificacao": "A", "minimo": "350,00", "maximo": "999,99"},
             {"classificacao": "B", "minimo": "180,00", "maximo": "349,99"},
             {"classificacao": "C", "minimo": "0,00", "maximo": "179,99"}]
    res = worker.put("/api/configuracoes/curva-abc", json={"faixas": novas})
    assert res.status_code == 200 and res.json["reclassificados"] == 1
    kappa = worker.get("/api/clientes", query_string={"q": "kappa", "mes": "2026-10"}).json["clientes"][0]
    assert kappa["classificacao"] == "A"


def test_mapeamento_padrao_salvo_pelo_worker(worker):
    con = _upload(worker, contratos_xlsx(), "contratos.xlsx", "contratos")
    res = worker.post("/api/importacao/configuracoes/do-arquivo",
                      json={"papel": "contratos", "token": con["token"], "planilha": con["planilha"],
                            "mapeamento": con["mapeamento"], "chave": "cnpj"})
    assert res.status_code == 200, res.json
    de_novo = _upload(worker, contratos_xlsx(), "contratos.xlsx", "contratos")
    assert de_novo["configuracao"]["padrao"] and de_novo["pendencias"] == {}


def test_csv_de_inconsistencias_e_modelo(worker):
    modelo = worker.get("/api/importacao/modelo.csv?tipo=contratos")
    assert modelo.status_code == 200 and modelo.mimetype == "text/csv"
    cli = _upload(worker, clientes_xlsx(), "clientes.xlsx", "clientes")
    con = _upload(worker, contratos_xlsx(), "contratos.xlsx", "contratos")
    body = {"chave": "cnpj", "mes_padrao": "2026-11",
            "clientes": {"token": cli["token"], "planilha": cli["planilha"], "mapeamento": cli["mapeamento"]},
            "contratos": {"token": con["token"], "planilha": con["planilha"], "mapeamento": con["mapeamento"]}}
    res = worker.post("/api/importacao/inconsistencias.csv", json=body)
    assert res.status_code == 200 and "C-900" in res.get_data(as_text=True)


def test_csv_simples_tambem_e_aceito(worker):
    conteudo = ("Código;Razão Social;CNPJ;Valor;Mês\n"
                f"7;Ômega Ltda;{CNPJ_B};450,00;11/2026\n").encode("utf-8")
    info = _upload(worker, conteudo, "clientes.csv", "clientes")
    body = {"clientes": {"token": info["token"], "planilha": info["planilha"], "mapeamento": info["mapeamento"]},
            "arquivo_clientes": "clientes.csv", "mes_padrao": "2026-11"}
    assert worker.post("/api/importacao/confirmar", json=body).status_code == 200
    omega = worker.get("/api/clientes", query_string={"q": "omega"}).json["clientes"][0]
    assert omega["classificacao"] == "A"
