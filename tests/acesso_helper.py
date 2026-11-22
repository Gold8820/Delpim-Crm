"""Tokens do Cloudflare Access para os testes: chave RSA gerada na hora e JWT RS256 assinado.

Tudo com a biblioteca padrão (sem rede e sem dependências). Chave de 1024 bits para os testes ficarem
rápidos; a verificação é a mesma usada com as chaves de 2048 bits do Access.
"""
import base64
import hashlib
import json
import random
import time

TIME = "delpim-teste.cloudflareaccess.com"
AUD = "aud-de-teste-0123456789abcdef"
_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _primo(bits: int, rnd: random.Random) -> int:
    while True:
        n = rnd.getrandbits(bits) | (1 << (bits - 1)) | 1
        if all(n % p for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)) and _miller_rabin(n, rnd):
            return n


def _miller_rabin(n: int, rnd: random.Random, rodadas: int = 24) -> bool:
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rodadas):
        x = pow(rnd.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


class ChaveRSA:
    def __init__(self, kid: str, semente: int, bits: int = 1024):
        rnd = random.Random(semente)
        e = 65537
        while True:
            p, q = _primo(bits // 2, rnd), _primo(bits // 2, rnd)
            phi = (p - 1) * (q - 1)
            if p != q and phi % e:
                break
        self.kid, self.n, self.e, self.d = kid, p * q, e, pow(e, -1, phi)

    def jwk(self) -> dict:
        def b64(i: int) -> str:
            return base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
        return {"kid": self.kid, "kty": "RSA", "alg": "RS256", "use": "sig", "e": b64(self.e), "n": b64(self.n)}

    def assinar(self, mensagem: bytes) -> bytes:
        k = (self.n.bit_length() + 7) // 8
        t = _DIGEST_INFO + hashlib.sha256(mensagem).digest()
        em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(k, "big")


CHAVE = ChaveRSA("chave-1", semente=1)
OUTRA_CHAVE = ChaveRSA("chave-1", semente=2)   # mesmo kid, chave diferente (assinatura falsa)


def jwks(*chaves: ChaveRSA) -> dict:
    return {"keys": [c.jwk() for c in (chaves or (CHAVE,))]}


def token(chave: ChaveRSA = CHAVE, **mudancas) -> str:
    agora = int(time.time())
    corpo = {"aud": [AUD], "email": "vendas@delpim.com.br", "iss": f"https://{TIME}", "iat": agora,
             "nbf": agora, "exp": agora + 3600, "sub": "usuario-1", "type": "app"}
    corpo.update(mudancas)
    corpo = {k: v for k, v in corpo.items() if v is not None}
    cab = {"alg": "RS256", "kid": chave.kid, "typ": "JWT"}

    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    entrada = f"{b64(cab)}.{b64(corpo)}"
    assinatura = base64.urlsafe_b64encode(chave.assinar(entrada.encode())).rstrip(b"=").decode()
    return f"{entrada}.{assinatura}"


def cabecalho(tok: str | None = None) -> dict:
    return {"Cf-Access-Jwt-Assertion": tok or token()}
