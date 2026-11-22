"""Login com Supabase Auth: tela de login, validação do JWT no backend, usuários autorizados, sessão e saída.

O Supabase Auth é simulado (tests/supabase_helper.py): mesmos endpoints e o mesmo tipo de token (ES256).
"""
import base64
import hashlib
import json

import pytest

import supabase_helper as sh
from clientes import autenticacao as auth
from clientes import create_app

USUARIO, SENHA = "vendas@delpim.com.br", "senha-de-teste-123"


@pytest.fixture(autouse=True)
def _limpo():
    sh.limpar_caches()
    yield
    sh.limpar_caches()


@pytest.fixture()
def supabase(monkeypatch):
    falso = sh.SupabaseFalso({USUARIO: SENHA, "sem.permissao@delpim.com.br": SENHA,
                              "desativado@delpim.com.br": SENHA})
    monkeypatch.setattr(auth, "_http", falso)
    return falso


@pytest.fixture()
def app_login(pg_url, schema, monkeypatch, supabase, consulta_factory=None):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("SUPABASE_URL", sh.URL)
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", sh.CHAVE_PUBLICAVEL)
    app = create_app(pg_url, schema=schema)
    from clientes.db import connect
    conn = connect(pg_url, schema)
    conn.execute("INSERT INTO usuarios_autorizados (email, nome) VALUES (%s, 'Vendas')", (USUARIO,))
    conn.execute("INSERT INTO usuarios_autorizados (email, ativo) VALUES ('desativado@delpim.com.br', false)")
    conn.commit()
    conn.close()
    return app


def entrar(c, email=USUARIO, senha=SENHA):
    return c.post("/api/auth/login", json={"email": email, "senha": senha})


# ============================================================================ tela de login e acesso

def test_sem_sessao_a_pagina_principal_vai_para_o_login(app_login):
    c = app_login.test_client()
    res = c.get("/")
    assert res.status_code == 302 and res.headers["Location"].startswith("/login")
    pagina = c.get("/login")
    assert pagina.status_code == 200 and b'id="login-form"' in pagina.data
    assert b"sb_publishable" not in pagina.data and b"service_role" not in pagina.data   # nenhuma chave no frontend


@pytest.mark.parametrize("metodo,rota", [("get", "/api/clientes"), ("get", "/api/meta"), ("post", "/api/clientes"),
                                         ("post", "/api/importacao/lote"), ("post", "/api/importacao/previa"),
                                         ("post", "/api/importacao/confirmar"), ("put", "/api/configuracoes/curva-abc"),
                                         ("get", "/api/importacao/configuracoes"), ("get", "/api/auth/sessao")])
def test_apis_sem_sessao_respondem_401(app_login, metodo, rota):
    res = getattr(app_login.test_client(), metodo)(rota)
    assert res.status_code == 401 and res.json["login"] == "/login"


def test_login_correto_abre_a_tela_de_clientes(app_login, supabase):
    c = app_login.test_client()
    res = entrar(c)
    assert res.status_code == 200 and res.json["email"] == USUARIO
    cookies = res.headers.getlist("Set-Cookie")
    sessao = next(x for x in cookies if x.startswith(auth.COOKIE_ACESSO + "="))
    assert "HttpOnly" in sessao and "SameSite=Lax" in sessao and "Path=/" in sessao
    assert c.get("/").status_code == 200
    assert c.get("/api/clientes").status_code == 200
    assert c.get("/api/auth/sessao").json == {"email": USUARIO, "login": True}
    # o backend só usou a chave publicável e só falou com o projeto Supabase configurado
    assert all(h.get("apikey") in (None, sh.CHAVE_PUBLICAVEL) for _, _, h in supabase.chamadas)
    assert any(h.get("apikey") == sh.CHAVE_PUBLICAVEL for _, url, h in supabase.chamadas if "grant_type=password" in url)


def test_senha_errada_e_usuario_inexistente(app_login):
    c = app_login.test_client()
    assert entrar(c, senha="errada").status_code == 401
    assert entrar(c, email="ninguem@delpim.com.br").status_code == 401
    assert c.get("/api/clientes").status_code == 401


def test_usuario_do_supabase_sem_autorizacao_da_empresa_e_recusado(app_login, supabase):
    """Conta existe no Supabase Auth (ex.: cadastro feito por fora), mas não está em usuarios_autorizados."""
    c = app_login.test_client()
    res = entrar(c, email="sem.permissao@delpim.com.br")
    assert res.status_code == 403 and "não está autorizado" in res.json["erro"]
    assert not any(x.startswith(auth.COOKIE_ACESSO + "=") and "Max-Age=0" not in x
                   for x in res.headers.getlist("Set-Cookie"))
    assert any("/auth/v1/logout" in url for _, url, _ in supabase.chamadas)   # a sessão criada foi encerrada
    assert c.get("/api/clientes").status_code == 401


def test_usuario_desativado_nao_entra(app_login):
    assert entrar(app_login.test_client(), email="desativado@delpim.com.br").status_code == 403


def test_token_valido_de_usuario_nao_autorizado_e_recusado_nas_apis(app_login):
    c = app_login.test_client()
    c.set_cookie(auth.COOKIE_ACESSO, sh.token("sem.permissao@delpim.com.br"))
    res = c.get("/api/clientes")
    assert res.status_code == 401 and "não está autorizado" in res.json["erro"]


def test_desautorizar_bloqueia_mesmo_com_sessao_aberta(app_login, consulta):
    c = app_login.test_client()
    entrar(c)
    assert c.get("/api/clientes").status_code == 200
    consulta("UPDATE usuarios_autorizados SET ativo = false WHERE email = %s RETURNING email", (USUARIO,))
    auth._autorizados.clear()   # em produção: vale em até 1 minuto (cache por instância)
    assert c.get("/api/clientes").status_code == 401


@pytest.mark.parametrize("tok", [
    "nao.e.jwt",
    sh.token(chave=sh.OUTRA),                       # assinatura de outra chave (forjado)
    sh.token(exp=1),                                # expirado
    sh.token(aud="anon", role="anon"),              # token anônimo (chave publicável), não é sessão
    sh.token(iss="https://outro-projeto.supabase.co/auth/v1"),
    sh.token(role="service_role"),                  # nem um token de serviço serve como sessão de usuário
], ids=["malformado", "forjado", "expirado", "anonimo", "outro_projeto", "service_role"])
def test_tokens_invalidos_sao_recusados(app_login, tok):
    c = app_login.test_client()
    c.set_cookie(auth.COOKIE_ACESSO, tok)
    assert c.get("/api/clientes").status_code == 401


def test_token_com_algoritmo_none_ou_hs256_e_recusado(app_login):
    b64 = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    corpo = {"aud": "authenticated", "role": "authenticated", "iss": f"{sh.URL}/auth/v1", "email": USUARIO,
             "exp": 4_000_000_000}
    for alg in ("none", "HS256"):
        falso = f"{b64({'alg': alg, 'kid': sh.CHAVE.kid})}.{b64(corpo)}.{b64({}) if alg != 'none' else ''}"
        c = app_login.test_client()
        c.set_cookie(auth.COOKIE_ACESSO, falso)
        assert c.get("/api/clientes").status_code == 401, alg


def test_token_vencido_e_renovado_com_o_refresh_token(app_login, supabase):
    c = app_login.test_client()
    sessao = supabase.sessao(USUARIO)
    c.set_cookie(auth.COOKIE_ACESSO, sh.token(USUARIO, exp=1))
    c.set_cookie(auth.COOKIE_RENOVACAO, sessao["refresh_token"])
    res = c.get("/api/clientes")
    assert res.status_code == 200
    assert any(x.startswith(auth.COOKIE_ACESSO + "=") for x in res.headers.getlist("Set-Cookie"))
    assert c.get("/api/clientes").status_code == 200


def test_sair_encerra_a_sessao(app_login, supabase):
    c = app_login.test_client()
    entrar(c)
    assert c.get("/api/clientes").status_code == 200
    rt = next(iter(supabase.refresh))
    res = c.post("/api/auth/logout")
    assert res.status_code == 200
    assert all("Max-Age=0" in x or "expires=Thu, 01 Jan 1970" in x for x in res.headers.getlist("Set-Cookie"))
    assert rt in supabase.revogados                       # refresh token revogado no Supabase
    assert c.get("/api/clientes").status_code == 401
    assert c.get("/").status_code == 302


def test_post_de_outro_site_e_bloqueado(app_login):
    c = app_login.test_client()
    entrar(c)
    res = c.post("/api/clientes", json={"codigo": "X", "razao_social": "Y"}, headers={"Origin": "https://ataque.exemplo"})
    assert res.status_code == 403
    ok = c.post("/api/clientes", json={"codigo": "X", "razao_social": "Y"}, headers={"Origin": "http://localhost"})
    assert ok.status_code == 201


def test_muitas_senhas_erradas_bloqueiam_temporariamente(app_login):
    c = app_login.test_client()
    for _ in range(8):
        entrar(c, senha="errada")
    assert entrar(c).status_code == 429


def test_nao_existe_rota_de_cadastro_publico(app_login):
    c = app_login.test_client()
    for rota in ("/api/auth/cadastro", "/api/auth/signup", "/signup", "/cadastro"):
        res = c.post(rota, json={"email": "a@b.c", "senha": "x"})
        assert res.status_code in (401, 404, 405) or res.headers["Location"].startswith("/login")


def test_healthz_aberto_sem_dados(app_login):
    res = app_login.test_client().get("/healthz")
    assert res.status_code == 200 and res.json == {"status": "ok"}


def test_redirecionamento_do_login_so_para_o_proprio_sistema(app_login):
    pagina = app_login.test_client().get("/login?proximo=//ataque.exemplo").data.decode()
    assert "/^\\/(?!\\/)/.test(pedido)" in pagina   # destino externo (//, http://) é trocado por "/"


# ============================================================================ sem login configurado (uso local) e Render

def test_local_sem_supabase_abre_direto(app):
    c = app.test_client()
    assert c.get("/").status_code == 200 and c.get("/api/clientes").status_code == 200
    assert c.get("/login").status_code == 302
    assert c.get("/api/auth/sessao").json["login"] is False


def test_render_sem_supabase_nao_inicia(pg_url, schema, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        create_app(pg_url, schema=schema)


# ============================================================================ ES256 (assinatura do Supabase)

def test_es256_confere_assinaturas():
    msg = b"cabecalho.corpo"
    assert auth.verificar_es256(sh.CHAVE.x, sh.CHAVE.y, msg, sh.CHAVE.assinar(msg))
    assert not auth.verificar_es256(sh.CHAVE.x, sh.CHAVE.y, msg + b"x", sh.CHAVE.assinar(msg))
    assert not auth.verificar_es256(sh.CHAVE.x, sh.CHAVE.y, msg, sh.OUTRA.assinar(msg))
    assert not auth.verificar_es256(sh.CHAVE.x, sh.CHAVE.y, msg, b"\x00" * 64)
    assert not auth.verificar_es256(1, 2, msg, sh.CHAVE.assinar(msg))          # ponto fora da curva


def test_es256_igual_a_biblioteca_cryptography():
    """Conferência independente: assinaturas feitas pela biblioteca cryptography (OpenSSL)."""
    ec = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ec")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    chave = ec.generate_private_key(ec.SECP256R1())
    pub = chave.public_key().public_numbers()
    for i in range(5):
        msg = f"mensagem {i}".encode()
        r, s = decode_dss_signature(chave.sign(msg, ec.ECDSA(hashes.SHA256())))
        assinatura = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        assert auth.verificar_es256(pub.x, pub.y, msg, assinatura)
        assert not auth.verificar_es256(pub.x, pub.y, msg + b"!", assinatura)
