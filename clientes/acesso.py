"""Proteção de acesso pelo Cloudflare Access (login com e-mail da empresa, fora da aplicação).

O sistema não tem mais usuário e senha próprios. O Cloudflare Access fica na frente do Worker e só deixa
passar quem está na política (e-mails autorizados). A cada requisição autorizada, o Access envia um token
assinado (JWT) no cabeçalho `Cf-Access-Jwt-Assertion`. Esta camada confere o token, para que o sistema
nunca responda a quem chegou sem passar pelo Access (por exemplo, se o Access for desligado por engano):

  - assinatura RS256 com as chaves públicas do time (https://<time>.cloudflareaccess.com/cdn-cgi/access/certs);
  - "aud" igual ao Application Audience (AUD) da aplicação do Access;
  - "iss" igual a https://<time>.cloudflareaccess.com;
  - validade (exp / nbf).

Configuração (variáveis, não são segredos): CF_ACCESS_TEAM_DOMAIN (ex.: empresa.cloudflareaccess.com) e
CF_ACCESS_AUD. No Cloudflare elas são obrigatórias: sem elas, nada é servido (503, falha fechada).
No ambiente local (python run.py) a verificação só é feita se as variáveis forem definidas.
Só /healthz fica aberto (não devolve dados).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from flask import Response, current_app, g, request

from .db import worker_env

PUBLIC_PATHS = {"/healthz"}
_CACHE_SEGUNDOS = 3600
_TOLERANCIA = 60   # segundos de diferença de relógio aceitos em exp/nbf
_DIGEST_INFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")
_chaves: dict = {"time": None, "lidas_em": 0.0, "chaves": {}}


class AcessoNegado(Exception):
    pass


def _b64url(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def verificar_rs256(n: int, e: int, mensagem: bytes, assinatura: bytes) -> bool:
    """Assinatura RSASSA-PKCS1-v1_5 com SHA-256 (o "RS256" dos JWT), só com a biblioteca padrão."""
    k = (n.bit_length() + 7) // 8
    if len(assinatura) != k or int.from_bytes(assinatura, "big") >= n:
        return False
    obtido = pow(int.from_bytes(assinatura, "big"), e, n).to_bytes(k, "big")
    t = _DIGEST_INFO_SHA256 + hashlib.sha256(mensagem).digest()
    if k < len(t) + 11:
        return False
    esperado = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(obtido, esperado)


def _baixar_json(url: str) -> dict:
    """No Worker usa o fetch do runtime; fora dele, urllib."""
    try:
        from js import fetch  # disponível só no Pyodide (Cloudflare Workers)
        from pyodide.ffi import run_sync
    except ImportError:
        import urllib.request
        with urllib.request.urlopen(url, timeout=10) as res:   # noqa: S310 (URL fixa do Access)
            return json.loads(res.read())
    resposta = run_sync(fetch(url))
    if resposta.status != 200:
        raise AcessoNegado(f"chaves do Access indisponíveis ({resposta.status})")
    return json.loads(run_sync(resposta.text()))


def _chaves_publicas(time_dominio: str, forcar: bool = False) -> dict:
    agora = time.time()
    if (not forcar and _chaves["time"] == time_dominio and _chaves["chaves"]
            and agora - _chaves["lidas_em"] < _CACHE_SEGUNDOS):
        return _chaves["chaves"]
    fornecidas = current_app.config.get("CF_ACCESS_JWKS")   # usado só pelos testes
    dados = fornecidas if fornecidas is not None else _baixar_json(f"https://{time_dominio}/cdn-cgi/access/certs")
    chaves = {}
    for k in dados.get("keys", []):
        if k.get("kty") == "RSA" and k.get("kid") and k.get("n") and k.get("e"):
            chaves[k["kid"]] = (int.from_bytes(_b64url(k["n"]), "big"), int.from_bytes(_b64url(k["e"]), "big"))
    _chaves.update(time=time_dominio, lidas_em=agora, chaves=chaves)
    return chaves


def validar_token(token: str, time_dominio: str, aud: str) -> dict:
    """Devolve as declarações (claims) do token, ou levanta AcessoNegado."""
    try:
        cab_b64, corpo_b64, ass_b64 = token.split(".")
        cabecalho = json.loads(_b64url(cab_b64))
        corpo = json.loads(_b64url(corpo_b64))
        assinatura = _b64url(ass_b64)
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise AcessoNegado("token malformado")
    if cabecalho.get("alg") != "RS256":
        raise AcessoNegado("algoritmo não aceito")
    chaves = _chaves_publicas(time_dominio)
    if cabecalho.get("kid") not in chaves:   # chave nova (rotação): relê uma vez
        chaves = _chaves_publicas(time_dominio, forcar=True)
    chave = chaves.get(cabecalho.get("kid"))
    if chave is None or not verificar_rs256(chave[0], chave[1], f"{cab_b64}.{corpo_b64}".encode(), assinatura):
        raise AcessoNegado("assinatura inválida")
    auds = corpo.get("aud")
    auds = [auds] if isinstance(auds, str) else (auds or [])
    if aud not in auds:
        raise AcessoNegado("aplicação (aud) diferente")
    if corpo.get("iss") != f"https://{time_dominio}":
        raise AcessoNegado("emissor diferente")
    agora = time.time()
    if not isinstance(corpo.get("exp"), (int, float)) or corpo["exp"] < agora - _TOLERANCIA:
        raise AcessoNegado("token expirado")
    if isinstance(corpo.get("nbf"), (int, float)) and corpo["nbf"] > agora + _TOLERANCIA:
        raise AcessoNegado("token ainda não vale")
    return corpo


def configuracao() -> tuple[str | None, str | None]:
    """(domínio do time, AUD): variáveis do Worker no Cloudflare; variáveis de ambiente no local."""
    env = worker_env()
    if env is not None:
        dominio, aud = getattr(env, "CF_ACCESS_TEAM_DOMAIN", None), getattr(env, "CF_ACCESS_AUD", None)
    else:
        dominio, aud = current_app.config.get("CF_ACCESS_TEAM_DOMAIN"), current_app.config.get("CF_ACCESS_AUD")
    dominio = str(dominio).strip().removeprefix("https://").strip("/") if dominio else None
    return dominio or None, (str(aud).strip() if aud else None)


def _texto(status: int, texto: str) -> Response:
    return Response(texto, status, {"Cache-Control": "no-store"}, mimetype="text/plain")


def exigir_acesso():
    """before_request: deixa passar só requisições com token válido do Cloudflare Access."""
    if request.path in PUBLIC_PATHS:
        return None
    dominio, aud = configuracao()
    if not (dominio and aud):
        if current_app.config.get("ACESSO_OBRIGATORIO"):
            return _texto(503, "Acesso bloqueado: configure o Cloudflare Access (CF_ACCESS_TEAM_DOMAIN e CF_ACCESS_AUD).")
        return None   # ambiente local sem Access configurado: uso na rede interna, como antes
    token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get("CF_Authorization")
    if not token:
        return _texto(403, "Acesso restrito: entre pelo Cloudflare Access com o e-mail da empresa.")
    try:
        claims = validar_token(token, dominio, aud)
    except AcessoNegado as exc:
        current_app.logger.warning("Cloudflare Access recusado: %s", exc)
        return _texto(403, "Acesso restrito: entre pelo Cloudflare Access com o e-mail da empresa.")
    g.usuario_email = claims.get("email")
    return None


def exige_access_no_render() -> None:
    """No Render (backend antigo) o sistema não sobe aberto: precisa do Access configurado."""
    if os.environ.get("RENDER") and not (os.environ.get("CF_ACCESS_TEAM_DOMAIN") and os.environ.get("CF_ACCESS_AUD")):
        raise RuntimeError("No Render, defina CF_ACCESS_TEAM_DOMAIN e CF_ACCESS_AUD (Cloudflare Access): "
                           "o sistema não inicia sem proteção de acesso.")
