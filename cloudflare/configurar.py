"""Configuração do Cloudflare (Hyperdrive + Worker), sem expor senhas na tela nem no repositório.

Pré-requisitos: Node.js (npx), Python, `npx wrangler login` feito uma vez (abre o navegador).

Passos (rode da raiz do projeto):
    python cloudflare/configurar.py senha-banco   # define a senha do usuário app_clientes no Supabase
    (alternativa: python cloudflare/configurar.py sql-senha  -> SQL para colar no SQL Editor do Supabase)
    python cloudflare/configurar.py hyperdrive    # cria o Hyperdrive (conexão Direct) e grava o id no wrangler.jsonc
    python cloudflare/configurar.py pacotes       # baixa as dependências Python (Python puro) para python_modules/
    python cloudflare/configurar.py deploy        # publica o Worker (workers.dev)
    npx wrangler secret put ACCESS_PASSWORD --config cloudflare/wrangler.jsonc   # senha de login do sistema

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


def deploy() -> None:
    if '"id": "00000000000000000000000000000000"' in WRANGLER.read_text(encoding="utf-8"):
        sys.exit("Crie o Hyperdrive antes (python cloudflare/configurar.py hyperdrive).")
    if not (AQUI / "python_modules").is_dir():
        pacotes()
    res = wrangler("deploy")
    sys.exit(res.returncode)


def _arquivo_credenciais() -> Path:
    return Path(os.environ.get("CREDENCIAIS_CLOUDFLARE", RAIZ.parent / "credenciais-cloudflare.env"))


def senha_acesso() -> None:
    """Senha do login do sistema (HTTP Basic): gera uma forte se ainda não houver, guarda no arquivo de
    credenciais (fora do repositório) e grava no Secret ACCESS_PASSWORD do Worker. Nada é exibido."""
    import secrets
    import string

    cred = credenciais()
    if not cred.get("ACCESS_PASSWORD"):
        senha = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
        with _arquivo_credenciais().open("a", encoding="utf-8") as f:
            f.write("# Login do sistema no Worker (usuário: ACCESS_USER do wrangler.jsonc, padrão admin):\n"
                    f"ACCESS_PASSWORD={senha}\n")
        cred["ACCESS_PASSWORD"] = senha
    res = wrangler("secret", "put", "ACCESS_PASSWORD", capturar=True, entrada=cred["ACCESS_PASSWORD"] + "\n")
    saida = ((res.stdout or "") + (res.stderr or "")).replace(cred["ACCESS_PASSWORD"], "<oculta>")
    if res.returncode != 0:
        sys.exit("Falha ao gravar o Secret:\n" + saida)
    print(f"Secret ACCESS_PASSWORD gravado no Worker. A senha está em {_arquivo_credenciais()} (linha ACCESS_PASSWORD).")


def verificar() -> None:
    """Roda scripts/verificar_worker.py contra o Worker publicado, com a senha do arquivo de credenciais."""
    cred = credenciais()
    url = os.environ.get("WORKER_URL") or sys.exit("Defina WORKER_URL (ex.: https://sistema-clientes.<conta>.workers.dev).")
    env = {**os.environ, "ACCESS_PASSWORD": cred["ACCESS_PASSWORD"], "PYTHONIOENCODING": "utf-8"}
    res = subprocess.run([sys.executable, str(RAIZ / "scripts" / "verificar_worker.py"), url], env=env)
    sys.exit(res.returncode)


if __name__ == "__main__":
    passos = {"senha-banco": senha_banco, "sql-senha": sql_senha, "hyperdrive": hyperdrive, "pacotes": pacotes,
              "deploy": deploy, "senha-acesso": senha_acesso, "verificar": verificar}
    if len(sys.argv) != 2 or sys.argv[1] not in passos:
        sys.exit(__doc__)
    passos[sys.argv[1]]()
