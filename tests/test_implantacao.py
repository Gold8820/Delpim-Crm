"""Preparação para hospedagem (Cloudflare/Render + Supabase): armazenamento no banco, acesso pelo Cloudflare Access,
verificação de saúde, proteção do XML de importação e higiene do repositório (segredos e .gitignore)."""
import io
import re
from pathlib import Path

import pytest
from openpyxl import Workbook

import acesso_helper as ah
from clientes import acesso, create_app

ROOT = Path(__file__).resolve().parent.parent


def xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload(client, content, name, papel="clientes"):
    return client.post("/api/importacao/arquivo", data={"arquivo": (io.BytesIO(content), name), "papel": papel},
                       content_type="multipart/form-data")


# ============================================================================ armazenamento no banco

def test_arquivo_enviado_fica_no_banco_e_nao_no_disco(client, consulta, reabrir):
    res = upload(client, xlsx_bytes([["Código", "Razão Social", "Valor"], [1, "Alfa", 100]]), "c.xlsx")
    assert res.status_code == 200
    assert consulta("SELECT COUNT(*) FROM arquivos_importacao")[0][0] == 1
    token = res.json["token"]
    # outra instância da aplicação (como outro worker ou réplica) enxerga o mesmo arquivo
    outra = reabrir().test_client()
    previa = outra.post("/api/importacao/planilha", json={"token": token, "planilha": None})
    assert previa.status_code == 200, previa.json


def test_upload_com_erro_de_formato_nao_deixa_arquivo_guardado(client, consulta):
    res = upload(client, b"isto nao e uma planilha", "c.xlsx")
    assert res.status_code == 400
    assert consulta("SELECT COUNT(*) FROM arquivos_importacao")[0][0] == 0


def test_arquivo_expirado_nao_e_mais_aceito(client, consulta):
    token = upload(client, xlsx_bytes([["Código", "Valor"], [1, 100]]), "c.xlsx").json["token"]
    consulta("UPDATE arquivos_importacao SET criado_em = '2000-01-01T00:00:00' WHERE token = %s", (token,))
    res = client.post("/api/importacao/planilha", json={"token": token})
    assert res.status_code == 400 and "expirou" in res.json["erro"]


def test_token_invalido_e_recusado(client):
    res = client.post("/api/importacao/planilha", json={"token": "../../etc/passwd"})
    assert res.status_code == 400 and "expirou" in res.json["erro"]


def test_limpeza_remove_arquivos_antigos_no_proximo_envio(client, consulta):
    upload(client, xlsx_bytes([["Código", "Valor"], [1, 100]]), "a.xlsx")
    consulta("UPDATE arquivos_importacao SET criado_em = '2000-01-01T00:00:00'")
    upload(client, xlsx_bytes([["Código", "Valor"], [2, 100]]), "b.xlsx")
    assert consulta("SELECT COUNT(*) FROM arquivos_importacao")[0][0] == 1


# ============================================================================ XML malicioso (.xls)

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet><Table><Row>
<Cell><Data>&lol3;</Data></Cell></Row></Table></Worksheet></Workbook>"""

XXE = b"""<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet><Table><Row>
<Cell><Data>&xxe;</Data></Cell></Row></Table></Worksheet></Workbook>"""


@pytest.mark.parametrize("conteudo", [BILLION_LAUGHS, XXE], ids=["entidades_expansivas", "entidade_externa"])
def test_xml_com_entidades_e_recusado_sem_ler_o_sistema(client, conteudo):
    res = upload(client, conteudo, "malicioso.xls", "contratos")
    assert res.status_code == 400
    assert "/etc/passwd" not in res.get_data(as_text=True)


# ============================================================================ acesso (Cloudflare Access, sem login próprio)

@pytest.fixture(autouse=True)
def _cache_limpo():
    acesso._chaves.update(time=None, lidas_em=0.0, chaves={})
    yield


@pytest.fixture()
def app_access(pg_url, schema, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", ah.TIME)
    monkeypatch.setenv("CF_ACCESS_AUD", ah.AUD)
    app = create_app(pg_url, schema=schema)
    app.config["CF_ACCESS_JWKS"] = ah.jwks()
    return app


def test_sem_login_proprio_a_pagina_principal_abre_direto(app, monkeypatch):
    """Não há mais tela/prompt de usuário e senha: ACCESS_PASSWORD não tem efeito algum."""
    monkeypatch.setenv("ACCESS_PASSWORD", "qualquer")
    c = app.test_client()
    res = c.get("/")
    assert res.status_code == 200 and b"<html" in res.data.lower()
    assert "WWW-Authenticate" not in res.headers
    assert c.get("/api/clientes").status_code == 200


def test_com_access_requisicao_sem_token_e_bloqueada(app_access):
    c = app_access.test_client()
    for rota in ("/", "/api/clientes", "/api/meta", "/api/importacao/configuracoes"):
        res = c.get(rota)
        assert res.status_code == 403, rota
        assert "WWW-Authenticate" not in res.headers   # nada de pedir senha no navegador
    assert c.post("/api/clientes", json={"codigo": "X", "razao_social": "Y"}).status_code == 403


def test_com_access_token_valido_vai_direto_para_a_tela_de_clientes(app_access):
    c = app_access.test_client()
    pagina = c.get("/", headers=ah.cabecalho())
    assert pagina.status_code == 200 and b"<html" in pagina.data.lower()
    assert c.get("/api/clientes", headers=ah.cabecalho()).status_code == 200


def test_cookie_do_access_tambem_e_aceito(app_access):
    c = app_access.test_client()
    c.set_cookie("CF_Authorization", ah.token())
    assert c.get("/api/clientes").status_code == 200


@pytest.mark.parametrize("tok", [
    "nao.e.um-jwt",
    ah.token(chave=ah.OUTRA_CHAVE),                  # assinatura de outra chave (forjado)
    ah.token(aud=["outra-aplicacao"]),               # token de outra aplicação do Access
    ah.token(iss="https://outro-time.cloudflareaccess.com"),
    ah.token(exp=1),                                 # expirado
    ah.token(nbf=4_000_000_000),                     # ainda não vale
], ids=["malformado", "assinatura_forjada", "outra_aud", "outro_emissor", "expirado", "futuro"])
def test_token_invalido_e_recusado_pelo_access(app_access, tok):
    assert app_access.test_client().get("/api/clientes", headers=ah.cabecalho(tok)).status_code == 403


def test_token_com_algoritmo_none_e_recusado(app_access):
    import base64, json
    b64 = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    falso = f"{b64({'alg': 'none', 'kid': 'chave-1'})}.{b64({'aud': [ah.AUD], 'iss': 'https://' + ah.TIME, 'exp': 4_000_000_000})}."
    assert app_access.test_client().get("/api/clientes", headers=ah.cabecalho(falso)).status_code == 403


def test_healthz_aberto_sem_dados(app_access):
    res = app_access.test_client().get("/healthz")
    assert res.status_code == 200 and res.json == {"status": "ok"}


def test_render_sem_access_nao_inicia(pg_url, schema, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    with pytest.raises(RuntimeError, match="CF_ACCESS"):
        create_app(pg_url, schema=schema)


def test_render_com_access_inicia_e_bloqueia_sem_token(pg_url, schema, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", ah.TIME)
    monkeypatch.setenv("CF_ACCESS_AUD", ah.AUD)
    app = create_app(pg_url, schema=schema)
    app.config["CF_ACCESS_JWKS"] = ah.jwks()
    assert app.test_client().get("/api/clientes").status_code == 403


def test_verificacao_rs256_confere_assinaturas():
    msg = b"cabecalho.corpo"
    assert acesso.verificar_rs256(ah.CHAVE.n, ah.CHAVE.e, msg, ah.CHAVE.assinar(msg))
    assert not acesso.verificar_rs256(ah.CHAVE.n, ah.CHAVE.e, msg + b"x", ah.CHAVE.assinar(msg))
    assert not acesso.verificar_rs256(ah.CHAVE.n, ah.CHAVE.e, msg, ah.OUTRA_CHAVE.assinar(msg))


def test_sem_database_url_falha_com_mensagem_clara(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        create_app()


def test_healthz_reporta_indisponivel_quando_o_banco_cai(app, monkeypatch):
    import psycopg

    class Quebrado:
        def execute(self, *a, **k):
            raise psycopg.OperationalError("banco fora do ar")

    monkeypatch.setattr("clientes.get_db", lambda: Quebrado())
    res = app.test_client().get("/healthz")
    assert res.status_code == 503 and res.json == {"status": "indisponivel"}


# ============================================================================ higiene do repositório

SEGREDO_PADRAO = re.compile(r"postgres(?:ql)?://[^\s:/@'\"]+:([^\s@'\"]+)@")
SENHAS_DE_EXEMPLO = {"SENHA", "senha", "<senha>", "senha-de-teste-123"}
EXTENSOES_DE_TEXTO = {".py", ".md", ".txt", ".yaml", ".yml", ".example", ".sql", ".js", ".html", ".css", ".json"}


# Pastas geradas por ferramentas (fora do Git, ver .gitignore): ambientes virtuais e pacotes do pywrangler.
PASTAS_GERADAS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", ".venv-workers", "python_modules",
                  "node_modules", ".wrangler"}


def _arquivos_do_projeto():
    ignorados = PASTAS_GERADAS
    for caminho in ROOT.rglob("*"):
        if caminho.is_file() and not any(parte in ignorados for parte in caminho.relative_to(ROOT).parts):
            yield caminho


def test_nenhuma_senha_de_banco_esta_escrita_no_projeto():
    achados = []
    for caminho in _arquivos_do_projeto():
        if caminho.suffix not in EXTENSOES_DE_TEXTO and caminho.name != ".env.example":
            continue
        texto = caminho.read_text(encoding="utf-8", errors="ignore")
        for m in SEGREDO_PADRAO.finditer(texto):
            if m.group(1) not in SENHAS_DE_EXEMPLO:
                achados.append(f"{caminho.relative_to(ROOT)}: {m.group(0)[:30]}...")
    assert not achados, "Senha escrita em arquivo versionável:\n" + "\n".join(achados)


def test_nao_ha_arquivo_de_ambiente_real_no_projeto():
    assert not any(p.name == ".env" for p in _arquivos_do_projeto())


def _padroes_do_gitignore():
    linhas = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    return {l.strip() for l in linhas if l.strip() and not l.strip().startswith("#")}


@pytest.mark.parametrize("padrao", [".env", "*.db", "*.sqlite", "*.sqlite3", "*.xlsx", "*.xls", "*.csv",
                                    "data/", "uploads/", "*.pem", "*.key"])
def test_gitignore_bloqueia_segredos_bancos_e_planilhas(padrao):
    assert padrao in _padroes_do_gitignore(), f"Falta '{padrao}' no .gitignore"


def test_gitignore_mantem_somente_o_fixture_sintetico_de_testes():
    padroes = _padroes_do_gitignore()
    assert "!tests/fixtures/etapa1.db" in padroes
    assert "!.env.example" in padroes


def test_planilhas_e_bancos_no_projeto_sao_somente_os_de_teste():
    """Inclui a pasta data/ (onde ficaria o SQLite real): só os fixtures de teste podem existir no repositório."""
    proibidos = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or any(parte in PASTAS_GERADAS for parte in p.relative_to(ROOT).parts):
            continue
        if p.suffix.lower() in {".xlsx", ".xls", ".csv", ".db", ".sqlite", ".sqlite3"} and "fixtures" not in p.parts:
            proibidos.append(p)
    assert not proibidos, [str(p.relative_to(ROOT)) for p in proibidos]
