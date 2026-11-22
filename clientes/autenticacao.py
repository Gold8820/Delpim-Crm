"""Login com Supabase Auth (e-mail e senha), validado no backend.

Fluxo:
  - /login mostra a tela de entrada. O navegador envia e-mail e senha para /api/auth/login (este servidor),
    que pede o token ao Supabase Auth (POST /auth/v1/token?grant_type=password) com a chave PUBLICÁVEL
    do projeto. Nenhuma chave vai para o navegador; a chave service_role não é usada em lugar nenhum.
  - Com o login aceito, a sessão fica em cookies HttpOnly + Secure + SameSite=Lax (o JavaScript não lê).
  - Em cada requisição o backend valida o access token (JWT ES256) com as chaves públicas do projeto
    (/auth/v1/.well-known/jwks.json): assinatura, emissor, audiência "authenticated", validade.
    Token vencido é renovado com o refresh token, sem o usuário perceber.
  - Só entra quem está ATIVO na tabela usuarios_autorizados (e-mail), além de existir no Supabase Auth.
    Cadastro público não dá acesso: mesmo que alguém crie uma conta, sem autorização o login é recusado.
  - /api/auth/logout encerra a sessão no Supabase (revoga o refresh token) e apaga os cookies.

Configuração (não são segredos): SUPABASE_URL e SUPABASE_PUBLISHABLE_KEY.
  - Cloudflare: variáveis do Worker (wrangler.jsonc). Login obrigatório.
  - Render: variáveis de ambiente. Login obrigatório.
  - Local (python run.py): se as variáveis existirem, o login é exigido; sem elas, abre direto (rede interna).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from urllib.parse import quote, urlsplit

from flask import Blueprint, current_app, g, jsonify, redirect, render_template, request

from . import db as dbmod
from .db import get_db, worker_env

bp = Blueprint("autenticacao", __name__)

COOKIE_ACESSO = "sc_sessao"
COOKIE_RENOVACAO = "sc_renovacao"
ROTAS_PUBLICAS = {"/login", "/healthz", "/api/auth/login", "/api/auth/logout"}
_CACHE_CHAVES = 3600          # chaves públicas do Supabase: relidas a cada hora (ou se aparecer chave nova)
_CACHE_AUTORIZACAO = 60       # usuários autorizados: revogação vale em até 1 minuto
_TOLERANCIA = 30


class NaoAutenticado(Exception):
    pass


# ============================================================================ ECDSA P-256 (ES256), só biblioteca padrão
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
      0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _no_curva(x: int, y: int) -> bool:
    return 0 <= x < _P and 0 <= y < _P and (y * y - (x * x * x - 3 * x + _B)) % _P == 0


def _dobrar(pt):
    x, y, z = pt
    if y == 0 or z == 0:
        return (0, 1, 0)
    delta = z * z % _P
    gamma = y * y % _P
    beta = x * gamma % _P
    alpha = 3 * (x - delta) * (x + delta) % _P
    x3 = (alpha * alpha - 8 * beta) % _P
    z3 = ((y + z) ** 2 - gamma - delta) % _P
    y3 = (alpha * (4 * beta - x3) - 8 * gamma * gamma) % _P
    return (x3, y3, z3)


def _somar(p1, p2):
    if p1[2] == 0:
        return p2
    if p2[2] == 0:
        return p1
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    z1z1, z2z2 = z1 * z1 % _P, z2 * z2 % _P
    u1, u2 = x1 * z2z2 % _P, x2 * z1z1 % _P
    s1, s2 = y1 * z2 * z2z2 % _P, y2 * z1 * z1z1 % _P
    if u1 == u2:
        return _dobrar(p1) if s1 == s2 else (0, 1, 0)
    h, r = (u2 - u1) % _P, (s2 - s1) % _P
    hh = h * h % _P
    hhh = h * hh % _P
    v = u1 * hh % _P
    x3 = (r * r - hhh - 2 * v) % _P
    y3 = (r * (v - x3) - s1 * hhh) % _P
    z3 = h * z1 * z2 % _P
    return (x3, y3, z3)


def _afim(pt):
    if pt[2] == 0:
        return None
    zi = pow(pt[2], -1, _P)
    return (pt[0] * zi * zi % _P, pt[1] * zi * zi * zi % _P)


def multiplicar(k: int, ponto) -> tuple | None:
    """k * ponto (coordenadas afins). Usado também pelos testes para gerar chaves."""
    r, q = (0, 1, 0), (ponto[0], ponto[1], 1)
    while k:
        if k & 1:
            r = _somar(r, q)
        q = _dobrar(q)
        k >>= 1
    return _afim(r)


def verificar_es256(x: int, y: int, mensagem: bytes, assinatura: bytes) -> bool:
    """Assinatura ECDSA P-256 com SHA-256 no formato JWS (r || s, 64 bytes)."""
    if len(assinatura) != 64 or not _no_curva(x, y):
        return False
    r, s = int.from_bytes(assinatura[:32], "big"), int.from_bytes(assinatura[32:], "big")
    if not (0 < r < _N and 0 < s < _N):
        return False
    e = int.from_bytes(hashlib.sha256(mensagem).digest(), "big")
    w = pow(s, -1, _N)
    u1, u2 = e * w % _N, r * w % _N
    # Truque de Shamir: u1*G + u2*Q numa passada só.
    g_, q_ = (_G[0], _G[1], 1), (x, y, 1)
    gq = _somar(g_, q_)
    acc = (0, 1, 0)
    for i in range(max(u1.bit_length(), u2.bit_length()) - 1, -1, -1):
        acc = _dobrar(acc)
        b1, b2 = (u1 >> i) & 1, (u2 >> i) & 1
        if b1 and b2:
            acc = _somar(acc, gq)
        elif b1:
            acc = _somar(acc, g_)
        elif b2:
            acc = _somar(acc, q_)
    ponto = _afim(acc)
    return ponto is not None and ponto[0] % _N == r


# ============================================================================ configuração e HTTP
def configuracao() -> tuple[str | None, str | None]:
    """(SUPABASE_URL, chave publicável): variáveis do Worker no Cloudflare; ambiente/config no local."""
    env = worker_env()
    if env is not None:
        url, chave = getattr(env, "SUPABASE_URL", None), getattr(env, "SUPABASE_PUBLISHABLE_KEY", None)
    else:
        url, chave = current_app.config.get("SUPABASE_URL"), current_app.config.get("SUPABASE_PUBLISHABLE_KEY")
    url = str(url).strip().rstrip("/") if url else None
    return url or None, (str(chave).strip() if chave else None)


def login_exigido() -> bool:
    return bool(current_app.config.get("LOGIN_OBRIGATORIO")) or configuracao()[0] is not None


def _http(metodo: str, url: str, cabecalhos: dict, corpo: dict | None = None) -> tuple[int, dict]:
    """Chamada ao Supabase Auth. No Worker usa o fetch do runtime; fora dele, urllib."""
    dados = json.dumps(corpo).encode() if corpo is not None else None
    try:
        from js import Object, fetch  # Pyodide (Cloudflare Workers)
        from pyodide.ffi import run_sync, to_js
    except ImportError:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, data=dados, method=metodo,
                                     headers={**cabecalhos, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as res:   # noqa: S310 (URL do projeto Supabase)
                texto = res.read()
                return res.status, (json.loads(texto) if texto else {})
        except urllib.error.HTTPError as exc:
            texto = exc.read()
            try:
                return exc.code, json.loads(texto) if texto else {}
            except ValueError:
                return exc.code, {}
    opcoes = {"method": metodo, "headers": {**cabecalhos, "Content-Type": "application/json"}}
    if dados is not None:
        opcoes["body"] = dados.decode()
    resposta = run_sync(fetch(url, to_js(opcoes, dict_converter=Object.fromEntries)))
    texto = run_sync(resposta.text())
    try:
        return resposta.status, (json.loads(texto) if texto else {})
    except ValueError:
        return resposta.status, {}


def _b64url(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


_chaves: dict = {"url": None, "lidas_em": 0.0, "chaves": {}}
_tokens: dict[str, tuple[float, dict]] = {}          # hash do token -> (exp, claims), por instância
_autorizados: dict[str, tuple[float, bool]] = {}     # e-mail -> (lido em, autorizado)
_falhas: dict[str, list[float]] = {}                 # e-mail -> horários de senhas erradas (por instância)


def _chaves_publicas(url: str, forcar: bool = False) -> dict:
    agora = time.time()
    if not forcar and _chaves["url"] == url and _chaves["chaves"] and agora - _chaves["lidas_em"] < _CACHE_CHAVES:
        return _chaves["chaves"]
    fornecidas = current_app.config.get("SUPABASE_JWKS")   # só nos testes (sem rede)
    if fornecidas is not None:
        dados = fornecidas
    else:
        status, dados = _http("GET", f"{url}/auth/v1/.well-known/jwks.json", {})
        if status != 200:
            raise NaoAutenticado(f"chaves do Supabase indisponíveis ({status})")
    chaves = {}
    for k in dados.get("keys", []):
        if k.get("kty") == "EC" and k.get("crv") == "P-256" and k.get("kid"):
            chaves[k["kid"]] = (int.from_bytes(_b64url(k["x"]), "big"), int.from_bytes(_b64url(k["y"]), "big"))
    _chaves.update(url=url, lidas_em=agora, chaves=chaves)
    return chaves


def validar_token(token: str, url: str) -> dict:
    """Claims do access token do Supabase, ou NaoAutenticado (assinatura, emissor, audiência, validade)."""
    chave_cache = hashlib.sha256(token.encode()).hexdigest()
    agora = time.time()
    em_cache = _tokens.get(chave_cache)
    if em_cache and em_cache[0] > agora:
        return em_cache[1]
    try:
        cab_b64, corpo_b64, ass_b64 = token.split(".")
        cabecalho, corpo = json.loads(_b64url(cab_b64)), json.loads(_b64url(corpo_b64))
        assinatura = _b64url(ass_b64)
    except (ValueError, TypeError, UnicodeDecodeError):
        raise NaoAutenticado("token malformado")
    if cabecalho.get("alg") != "ES256":
        raise NaoAutenticado("algoritmo não aceito")
    chaves = _chaves_publicas(url)
    if cabecalho.get("kid") not in chaves:
        chaves = _chaves_publicas(url, forcar=True)
    chave = chaves.get(cabecalho.get("kid"))
    if chave is None or not verificar_es256(chave[0], chave[1], f"{cab_b64}.{corpo_b64}".encode(), assinatura):
        raise NaoAutenticado("assinatura inválida")
    auds = corpo.get("aud")
    auds = [auds] if isinstance(auds, str) else (auds or [])
    if "authenticated" not in auds or corpo.get("role") != "authenticated":
        raise NaoAutenticado("token sem sessão de usuário")
    if corpo.get("iss") != f"{url}/auth/v1":
        raise NaoAutenticado("emissor diferente")
    exp = corpo.get("exp")
    if not isinstance(exp, (int, float)) or exp < agora - _TOLERANCIA:
        raise NaoAutenticado("token expirado")
    if len(_tokens) > 500:
        _tokens.clear()
    _tokens[chave_cache] = (float(exp), corpo)
    return corpo


def autorizado(email: str | None) -> bool:
    """E-mail ativo na tabela usuarios_autorizados (cache de 1 minuto por instância)."""
    if not email:
        return False
    email = email.strip().lower()
    agora = time.time()
    em_cache = _autorizados.get(email)
    if em_cache and agora - em_cache[0] < _CACHE_AUTORIZACAO:
        return em_cache[1]
    row = get_db().execute("SELECT ativo FROM usuarios_autorizados WHERE email = %s", (email,)).fetchone()
    resultado = bool(row and row["ativo"])
    _autorizados[email] = (agora, resultado)
    return resultado


# ============================================================================ cookies e sessão
def _seguro() -> bool:
    return request.is_secure or bool(current_app.config.get("CLOUDFLARE")) or \
        request.headers.get("X-Forwarded-Proto") == "https"


def _gravar_cookies(resposta, sessao: dict) -> None:
    validade = int(sessao.get("expires_in") or 3600)
    comum = {"httponly": True, "secure": _seguro(), "samesite": "Lax", "path": "/"}
    resposta.set_cookie(COOKIE_ACESSO, sessao["access_token"], max_age=validade + 60, **comum)
    if sessao.get("refresh_token"):
        resposta.set_cookie(COOKIE_RENOVACAO, sessao["refresh_token"], max_age=30 * 24 * 3600, **comum)


def _apagar_cookies(resposta) -> None:
    for nome in (COOKIE_ACESSO, COOKIE_RENOVACAO):
        resposta.delete_cookie(nome, path="/", secure=_seguro(), httponly=True, samesite="Lax")


def _cabecalhos(chave: str, token: str | None = None) -> dict:
    c = {"apikey": chave}
    if token:
        c["Authorization"] = f"Bearer {token}"
    return c


def _renovar(url: str, chave: str, refresh: str) -> dict | None:
    status, dados = _http("POST", f"{url}/auth/v1/token?grant_type=refresh_token", _cabecalhos(chave),
                          {"refresh_token": refresh})
    return dados if status == 200 and dados.get("access_token") else None


def _nao_autorizado(mensagem: str = "Sessão expirada. Entre novamente."):
    if request.path.startswith("/api/"):
        resposta = jsonify({"erro": mensagem, "login": "/login"})
        resposta.status_code = 401
    else:
        destino = request.full_path.rstrip("?") if request.path != "/" else "/"
        resposta = redirect(f"/login?proximo={quote(destino, safe='/')}")
    resposta.headers["Cache-Control"] = "no-store"
    _apagar_cookies(resposta)
    return resposta


def _origem_confere() -> bool:
    """Requisições que alteram dados precisam vir do próprio sistema (proteção extra contra CSRF)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    origem = request.headers.get("Origin")
    if not origem:
        return True   # navegadores sempre enviam Origin em POST/PUT/DELETE de outro site; sem ele, não é navegador
    return urlsplit(origem).netloc == request.host


def exigir_login():
    """before_request: só passa quem tem sessão válida de usuário autorizado."""
    if not _origem_confere():
        return jsonify({"erro": "Origem não permitida."}), 403
    if request.path in ROTAS_PUBLICAS or request.path.startswith("/static/") or not login_exigido():
        return None
    url, chave = configuracao()
    if not (url and chave):
        return _nao_autorizado("Login indisponível: configure SUPABASE_URL e SUPABASE_PUBLISHABLE_KEY.")
    token = request.cookies.get(COOKIE_ACESSO)
    claims = None
    if token:
        try:
            claims = validar_token(token, url)
        except NaoAutenticado:
            claims = None
    if claims is None:
        refresh = request.cookies.get(COOKIE_RENOVACAO)
        nova = _renovar(url, chave, refresh) if refresh else None
        if nova:
            try:
                claims = validar_token(nova["access_token"], url)
                g.sessao_renovada = nova
            except NaoAutenticado:
                claims = None
    if claims is None:
        return _nao_autorizado()
    try:
        ok = autorizado(claims.get("email"))
    except dbmod.Error:
        get_db().rollback()
        return jsonify({"erro": "Não foi possível verificar o acesso. Tente novamente."}), 503
    if not ok:
        return _nao_autorizado("Seu usuário não está autorizado a acessar o sistema.")
    g.usuario_email = claims.get("email")
    return None


def renovar_cookies(resposta):
    """after_request: grava a sessão renovada (quando o token venceu e foi trocado pelo refresh token)."""
    nova = g.pop("sessao_renovada", None)
    if nova:
        _gravar_cookies(resposta, nova)
    return resposta


# ============================================================================ rotas
@bp.get("/login")
def pagina_login():
    if not login_exigido():
        return redirect("/")
    return render_template("login.html")


def _muitas_falhas(email: str) -> bool:
    agora = time.time()
    recentes = [t for t in _falhas.get(email, []) if agora - t < 600]
    _falhas[email] = recentes
    return len(recentes) >= 8


@bp.post("/api/auth/login")
def login():
    corpo = request.get_json(silent=True) or {}
    email = str(corpo.get("email") or "").strip().lower()
    senha = str(corpo.get("senha") or "")
    if not email or not senha or len(email) > 254 or len(senha) > 200:
        return jsonify({"erro": "Informe e-mail e senha."}), 400
    url, chave = configuracao()
    if not (url and chave):
        return jsonify({"erro": "Login indisponível: autenticação não configurada no servidor."}), 503
    if _muitas_falhas(email):
        return jsonify({"erro": "Muitas tentativas. Aguarde alguns minutos e tente de novo."}), 429
    status, sessao = _http("POST", f"{url}/auth/v1/token?grant_type=password", _cabecalhos(chave),
                           {"email": email, "password": senha})
    if status != 200 or not sessao.get("access_token"):
        _falhas.setdefault(email, []).append(time.time())
        if status == 429:
            return jsonify({"erro": "Muitas tentativas. Aguarde alguns minutos e tente de novo."}), 429
        return jsonify({"erro": "E-mail ou senha incorretos."}), 401
    try:
        claims = validar_token(sessao["access_token"], url)
        permitido = autorizado(claims.get("email"))
    except NaoAutenticado:
        permitido = False
    if not permitido:
        # Conta existe no Supabase, mas a empresa não autorizou: encerra a sessão recém-criada.
        _http("POST", f"{url}/auth/v1/logout?scope=local", _cabecalhos(chave, sessao["access_token"]))
        return jsonify({"erro": "Seu usuário não está autorizado a acessar o sistema. Fale com o administrador."}), 403
    _falhas.pop(email, None)
    resposta = jsonify({"ok": True, "email": claims.get("email")})
    resposta.headers["Cache-Control"] = "no-store"
    _gravar_cookies(resposta, sessao)
    return resposta


@bp.post("/api/auth/logout")
def logout():
    url, chave = configuracao()
    token = request.cookies.get(COOKIE_ACESSO)
    if url and chave and token:
        try:   # revoga a sessão no Supabase (o refresh token deixa de valer)
            _http("POST", f"{url}/auth/v1/logout?scope=local", _cabecalhos(chave, token))
        except Exception:   # noqa: BLE001 - sair sempre apaga os cookies, mesmo sem resposta do Supabase
            current_app.logger.warning("Logout no Supabase falhou", exc_info=True)
        _tokens.pop(hashlib.sha256(token.encode()).hexdigest(), None)
    resposta = jsonify({"ok": True})
    resposta.headers["Cache-Control"] = "no-store"
    _apagar_cookies(resposta)
    return resposta


@bp.get("/api/auth/sessao")
def sessao():
    return jsonify({"email": g.get("usuario_email"), "login": login_exigido()})


def exige_login_no_render() -> None:
    """No Render (backend antigo) o sistema não sobe sem autenticação configurada."""
    if os.environ.get("RENDER") and not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_PUBLISHABLE_KEY")):
        raise RuntimeError("No Render, defina SUPABASE_URL e SUPABASE_PUBLISHABLE_KEY (Supabase Auth): "
                           "o sistema não inicia sem login.")
