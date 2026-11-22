"""Configuração do Cloudflare (Hyperdrive + Worker), sem expor senhas na tela nem no repositório.

Pré-requisitos: Node.js (npx), Python, `npx wrangler login` feito uma vez (abre o navegador).

Passos (rode da raiz do projeto):
    python cloudflare/configurar.py senha-banco   # define a senha do usuário app_clientes no Supabase
    (alternativa: python cloudflare/configurar.py sql-senha  -> SQL para colar no SQL Editor do Supabase)
    python cloudflare/configurar.py hyperdrive    # cria o Hyperdrive (conexão Direct) e grava o id no wrangler.jsonc
    python cloudflare/configurar.py pacotes       # baixa as dependências Python (Python puro) para python_modules/
    python cloudflare/configurar.py acesso dev <time>.cloudflareaccess.com <AUD>   # Cloudflare Access (validação)
    python cloudflare/configurar.py deploy-dev    # publica a versão de VALIDAÇÃO (sistema-clientes-dev)
    python cloudflare/configurar.py verificar     # testa ponta a ponta (WORKER_URL=...; Service Token do Access)
    python cloudflare/configurar.py acesso producao <time>.cloudflareaccess.com <AUD>
    python cloudflare/configurar.py deploy --producao   # só depois de validar: substitui a produção

Não há usuário e senha no sistema: o acesso é feito pelo Cloudflare Access (e-mails autorizados).

As credenciais do usuário app_clientes ficam em um arquivo FORA do repositório
(padrão: ../credenciais-cloudflare.env, ao lado da pasta do projeto; outra pasta: CREDENCIAIS_CLOUDFLARE=...).
"""
from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
WRANGLER = AQUI / "wrangler.jsonc"
PROJETO_SUPABASE = "qbsaiaqmhglotnhvrnqd"
POOLER_HOST = "aws-1-us-east-2.pooler.supabase.com"   # região us-east-2 do projeto
NOME_HYPERDRIVE = "sistema-clientes-supabase"
NPX = shutil.which("npx") or shutil.which("npx.cmd") or "npx"


def credenciais() -> dict:
    caminho = Path(os.environ.get("CREDENCIAIS_CLOUDFLARE", RAIZ.parent / "credenciais-cloudflare.env"))
    if not caminho.is_file():
        sys.exit(f"Arquivo de credenciais não encontrado: {caminho}")
    dados = {}
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        if "=" in linha and not linha.lstrip().startswith("#"):
            k, v = linha.split("=", 1)
            dados[k.strip()] = v.strip()
    return dados


def wrangler(*args, capturar=False, entrada=None):
    cmd = [NPX, "--yes", "wrangler@4.166.0", *args]
    return subprocess.run(cmd, cwd=AQUI, text=True, input=entrada,
                          capture_output=capturar, encoding="utf-8", errors="replace")


def senha_banco() -> None:
    """ALTER ROLE app_clientes PASSWORD ... com o usuário postgres (a senha é pedida sem eco)."""
    import psycopg
    from psycopg import sql

    cred = credenciais()
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        admin = getpass.getpass("Senha do usuário 'postgres' do Supabase (não aparece na tela): ")
        url = _url(f"postgres.{PROJETO_SUPABASE}", admin, POOLER_HOST, "5432", "postgres")
    with psycopg.connect(url, autocommit=True, prepare_threshold=None) as conn:
        conn.execute(sql.SQL("ALTER ROLE app_clientes PASSWORD {}").format(sql.Literal(cred["APP_DB_PASSWORD"])))
    print("Senha do usuário app_clientes definida no Supabase.")


def sql_senha() -> None:
    """Alternativa sem a senha do postgres: grava o comando SQL num arquivo fora do repositório,
    para colar no SQL Editor do Supabase (painel web, já autenticado)."""
    cred = credenciais()
    senha = cred["APP_DB_PASSWORD"].replace("'", "''")
    destino = RAIZ.parent / "senha-app-clientes.sql"
    destino.write_text("-- Cole no Supabase: SQL Editor > New query > Run. Depois APAGUE este arquivo.\n"
                       f"ALTER ROLE app_clientes PASSWORD '{senha}';\n", encoding="utf-8")
    print(f"Comando gravado em {destino} (fora do repositório). Cole no SQL Editor do Supabase, rode e apague o arquivo.")


def _criar(conexao: str) -> tuple[int, str]:
    res = wrangler("hyperdrive", "create", NOME_HYPERDRIVE, f"--connection-string={conexao}",
                   "--caching-disabled", capturar=True)
    saida = (res.stdout or "") + (res.stderr or "")
    return res.returncode, saida.replace(conexao, "<conexão oculta>")


def _url(usuario: str, senha: str, host: str, porta: str, banco: str) -> str:
    """Monta a conexão em memória (a senha nunca é gravada em arquivo nem exibida)."""
    credencial = quote(usuario, safe=".") + ":" + quote(senha, safe="")
    return "postgres://" + credencial + "@" + host + ":" + str(porta) + "/" + banco + "?sslmode=require"


def hyperdrive() -> None:
    cred = credenciais()
    senha = cred["APP_DB_PASSWORD"]
    user, host, porta, banco = cred["APP_DB_USER"], cred["APP_DB_HOST"], cred["APP_DB_PORT"], cred["APP_DB_NAME"]
    # Conexão Direct (recomendada pela Cloudflare para Supabase; o Hyperdrive faz o pool).
    direta = _url(user, senha, host, porta, banco)
    codigo, saida = _criar(direta)
    modo = "Direct"
    if codigo != 0:
        print("A conexão Direct falhou (o host Direct do Supabase só tem IPv6). Saída do wrangler:\n" + saida)
        # Alternativa: pooler em modo SESSÃO (porta 5432), não o Transaction pooler (6543).
        sessao = _url(f"{user}.{PROJETO_SUPABASE}", senha, POOLER_HOST, "5432", banco)
        codigo, saida = _criar(sessao)
        modo = "Session pooler (porta 5432)"
    if codigo != 0:
        sys.exit("Não foi possível criar o Hyperdrive:\n" + saida)
    ids = re.findall(r"\b[0-9a-f]{32}\b", saida)
    if not ids:
        sys.exit("Hyperdrive criado, mas o id não foi encontrado na saída:\n" + saida)
    texto = WRANGLER.read_text(encoding="utf-8")
    texto = re.sub(r'("binding": "HYPERDRIVE", "id": ")[0-9a-f]{32}(")', rf"\g<1>{ids[0]}\g<2>", texto)
    WRANGLER.write_text(texto, encoding="utf-8", newline="\n")
    print(f"Hyperdrive criado ({modo}), cache de consultas desligado. id={ids[0]} (gravado no wrangler.jsonc)")


def pacotes() -> None:
    """O mesmo que `pywrangler sync` (que não funciona no Windows): dependências em Python puro."""
    destino = AQUI / "python_modules"
    shutil.rmtree(destino, ignore_errors=True)
    import tomllib

    deps = tomllib.loads((AQUI / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(destino), "--no-compile",
                    *deps], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(destino), "--no-compile",
                    "--no-deps", "workers-runtime-sdk==1.9.6"], check=True)
    for extra in list(destino.rglob("*.pyd")) + list(destino.rglob("*.so")):
        extra.unlink()   # aceleradores nativos opcionais (ex.: markupsafe); no Pyodide usa-se a versão Python
    shutil.rmtree(destino / "bin", ignore_errors=True)
    print(f"Dependências prontas em {destino}")


def _publicar(*extra) -> None:
    if '"id": "00000000000000000000000000000000"' in WRANGLER.read_text(encoding="utf-8"):
        sys.exit("Crie o Hyperdrive antes (python cloudflare/configurar.py hyperdrive).")
    if not (AQUI / "python_modules").is_dir():
        pacotes()
    sys.exit(wrangler("deploy", *extra).returncode)


def deploy_dev() -> None:
    """Versão de validação: Worker sistema-clientes-dev (a produção não muda)."""
    _publicar("--env", "dev")


def deploy() -> None:
    """Produção (sistema-clientes). Exige --producao, para não substituir a produção por engano."""
    if "--producao" not in sys.argv:
        sys.exit("Para publicar na PRODUÇÃO use: python cloudflare/configurar.py deploy --producao "
                 "(valide antes com deploy-dev).")
    _publicar()


def acesso() -> None:
    """Grava no wrangler.jsonc o domínio do time e o AUD da aplicação do Cloudflare Access (não são segredos)."""
    if len(sys.argv) != 5 or sys.argv[2] not in ("dev", "producao"):
        sys.exit("Uso: python cloudflare/configurar.py acesso dev|producao <time>.cloudflareaccess.com <AUD>")
    ambiente, dominio, aud = sys.argv[2], sys.argv[3].removeprefix("https://").strip("/"), sys.argv[4].strip()
    if not re.fullmatch(r"[a-z0-9-]+\.cloudflareaccess\.com", dominio) or not re.fullmatch(r"[0-9a-f]{64}", aud):
        sys.exit("Domínio esperado: <time>.cloudflareaccess.com; AUD: 64 caracteres hexadecimais (Application Audience Tag).")
    texto = WRANGLER.read_text(encoding="utf-8")
    novo = f'"vars": {{ "CF_ACCESS_TEAM_DOMAIN": "{dominio}", "CF_ACCESS_AUD": "{aud}" }}'
    padrao = r'"vars": \{ "CF_ACCESS_TEAM_DOMAIN": "[^"]*", "CF_ACCESS_AUD": "[^"]*" \}'
    ocorrencias = list(re.finditer(padrao, texto))   # 1ª = produção, 2ª = env.dev
    alvo = ocorrencias[1] if ambiente == "dev" else ocorrencias[0]
    texto = texto[:alvo.start()] + novo + texto[alvo.end():]
    WRANGLER.write_text(texto, encoding="utf-8", newline="\n")
    print(f"Cloudflare Access ({ambiente}) gravado no wrangler.jsonc. Publique de novo para valer.")


def verificar() -> None:
    """Roda scripts/verificar_worker.py contra um Worker publicado (WORKER_URL).

    Para passar pelo Cloudflare Access, defina CF_ACCESS_CLIENT_ID e CF_ACCESS_CLIENT_SECRET (Service Token
    incluído na política da aplicação) ou coloque-os no arquivo de credenciais (fora do repositório)."""
    cred = credenciais()
    url = os.environ.get("WORKER_URL") or sys.exit("Defina WORKER_URL (ex.: https://sistema-clientes-dev.<conta>.workers.dev).")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for chave in ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"):
        if cred.get(chave) and not env.get(chave):
            env[chave] = cred[chave]
    res = subprocess.run([sys.executable, str(RAIZ / "scripts" / "verificar_worker.py"), url, "--exige-bloqueio"], env=env)
    sys.exit(res.returncode)


if __name__ == "__main__":
    passos = {"senha-banco": senha_banco, "sql-senha": sql_senha, "hyperdrive": hyperdrive, "pacotes": pacotes,
              "acesso": acesso, "deploy-dev": deploy_dev, "deploy": deploy, "verificar": verificar}
    if len(sys.argv) < 2 or sys.argv[1] not in passos:
        sys.exit(__doc__)
    passos[sys.argv[1]]()
