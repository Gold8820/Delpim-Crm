"""Supabase Auth simulado para os testes (sem rede): chave P-256, tokens ES256 e os endpoints usados.

O backend fala com o Supabase Auth só por `clientes.autenticacao._http`; aqui ele é trocado por um
servidor falso que se comporta como o Supabase (login por senha, renovação, logout, JWKS).
"""
import base64
import hashlib
import json
import random
import time
import uuid

from clientes import autenticacao as auth

URL = "https://projeto-teste.supabase.co"
CHAVE_PUBLICAVEL = "sb_publishable_teste"


def _b64(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode()


class ChaveEC:
    def __init__(self, kid: str, semente: int):
        self.kid = kid
        self.d = random.Random(semente).randrange(1, auth._N)
        self.x, self.y = auth.multiplicar(self.d, auth._G)
        self._rnd = random.Random(semente + 1000)

    def jwk(self) -> dict:
        return {"kid": self.kid, "kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig",
                "x": _b64(self.x.to_bytes(32, "big")), "y": _b64(self.y.to_bytes(32, "big"))}

    def assinar(self, mensagem: bytes) -> bytes:
        e = int.from_bytes(hashlib.sha256(mensagem).digest(), "big")
        while True:
            k = self._rnd.randrange(1, auth._N)
            r = auth.multiplicar(k, auth._G)[0] % auth._N
            s = pow(k, -1, auth._N) * (e + r * self.d) % auth._N
            if r and s:
                return r.to_bytes(32, "big") + s.to_bytes(32, "big")


CHAVE = ChaveEC("chave-supabase-1", 7)
OUTRA = ChaveEC("chave-supabase-1", 8)     # mesmo kid, outra chave (assinatura forjada)


def jwks() -> dict:
    return {"keys": [CHAVE.jwk()]}


def token(email="vendas@delpim.com.br", chave=CHAVE, alg="ES256", **mudancas) -> str:
    agora = int(time.time())
    corpo = {"aud": "authenticated", "role": "authenticated", "iss": f"{URL}/auth/v1", "sub": str(uuid.uuid4()),
             "email": email, "iat": agora, "exp": agora + 3600, "session_id": str(uuid.uuid4())}
    corpo.update(mudancas)
    corpo = {k: v for k, v in corpo.items() if v is not None}
    cab = {"alg": alg, "kid": chave.kid, "typ": "JWT"}
    entrada = f"{_b64(json.dumps(cab).encode())}.{_b64(json.dumps(corpo).encode())}"
    return f"{entrada}.{_b64(chave.assinar(entrada.encode()))}"


class SupabaseFalso:
    """Endpoints do Supabase Auth usados pelo sistema. `usuarios` = {email: senha} existentes no Auth."""

    def __init__(self, usuarios: dict):
        self.usuarios = {e.lower(): s for e, s in usuarios.items()}
        self.refresh: dict[str, str] = {}      # refresh token -> e-mail
        self.revogados: set[str] = set()
        self.chamadas: list[tuple[str, str, dict]] = []

    def sessao(self, email: str) -> dict:
        rt = uuid.uuid4().hex
        self.refresh[rt] = email
        return {"access_token": token(email), "refresh_token": rt, "expires_in": 3600, "token_type": "bearer"}

    def __call__(self, metodo, url, cabecalhos, corpo=None):
        self.chamadas.append((metodo, url, dict(cabecalhos)))
        assert url.startswith(URL), url
        if url.endswith("/auth/v1/.well-known/jwks.json"):   # público, como no Supabase (sem chave)
            return 200, jwks()
        assert cabecalhos.get("apikey") == CHAVE_PUBLICAVEL       # só a chave publicável, nunca service_role
        if "grant_type=password" in url:
            email = (corpo or {}).get("email", "").lower()
            if self.usuarios.get(email) and self.usuarios[email] == corpo.get("password"):
                return 200, self.sessao(email)
            return 400, {"error": "invalid_grant", "error_description": "Invalid login credentials"}
        if "grant_type=refresh_token" in url:
            rt = (corpo or {}).get("refresh_token")
            if rt in self.refresh and rt not in self.revogados:
                email = self.refresh.pop(rt)
                return 200, self.sessao(email)
            return 400, {"error": "invalid_grant"}
        if "/auth/v1/logout" in url:
            self.revogados.update(self.refresh)
            return 204, {}
        return 404, {}


def limpar_caches():
    auth._chaves.update(url=None, lidas_em=0.0, chaves={})
    auth._tokens.clear()
    auth._autorizados.clear()
    auth._falhas.clear()
