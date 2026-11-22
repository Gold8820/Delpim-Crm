"""Sistema de Inteligência Geográfica Comercial — cadastro de clientes, importação e Curva ABC.

Configuração por variáveis de ambiente (ver .env.example):
  DATABASE_URL     conexão do PostgreSQL (Supabase). Obrigatória no ambiente local.
  ACCESS_PASSWORD  senha do acesso ao sistema (HTTP Basic). Obrigatória no Render e no Cloudflare.
  ACCESS_USER      usuário do acesso (padrão: admin).

No Cloudflare Workers (src/worker.py, `create_app(cloudflare=True)`):
  - o banco vem do binding HYPERDRIVE, a cada requisição (ver clientes/db.py);
  - ACCESS_USER / ACCESS_PASSWORD vêm das variáveis e Secrets do Worker, lidos a cada requisição;
  - o esquema do banco não é alterado ao iniciar (migrações: scripts/migrar_schema.py);
  - CSS e JS vêm do Workers Static Assets (ver clientes/cloudflare.py).
"""
from __future__ import annotations

import hmac
import os

from flask import Flask, Response, jsonify, request

from . import db as dbmod
from .db import close_db, get_db, init_db, worker_env


def _exige_senha_configurada() -> None:
    """Falha ao iniciar se o sistema estiver no Render sem senha: nunca sobe com dados abertos."""
    if os.environ.get("RENDER") and not os.environ.get("ACCESS_PASSWORD"):
        raise RuntimeError("ACCESS_PASSWORD não foi definida. No Render, o sistema não inicia sem senha de acesso "
                           "(os dados dos clientes não podem ficar públicos). Defina a variável no painel do serviço.")


def _acesso_autorizado(password: str, user: str) -> bool:
    auth = request.authorization
    if auth is None or auth.type is None or auth.type.lower() != "basic":
        return False
    usuario_ok = hmac.compare_digest((auth.username or "").encode(), user.encode())
    senha_ok = hmac.compare_digest((auth.password or "").encode(), password.encode())
    return usuario_ok and senha_ok


def _credenciais(app: Flask) -> tuple[str | None, str]:
    """(senha, usuário) esperados: Secrets do Worker no Cloudflare; variáveis de ambiente no local/Render."""
    env = worker_env()
    if env is not None:
        password = getattr(env, "ACCESS_PASSWORD", None)
        user = getattr(env, "ACCESS_USER", None)
        return (str(password) if password else None), (str(user) if user else "admin")
    return app.config["ACCESS_PASSWORD"], app.config["ACCESS_USER"]


def create_app(database_url: str | None = None, schema: str | None = None, *, cloudflare: bool = False) -> Flask:
    """Cria a aplicação. `database_url` padrão: variável de ambiente DATABASE_URL.

    `schema` é usado apenas pelos testes, para isolar cada teste em um schema próprio.
    `cloudflare=True` é usado só por src/worker.py (ver a descrição do módulo).
    """
    _exige_senha_configurada()
    url = database_url or os.environ.get("DATABASE_URL")
    if not url and not cloudflare:
        raise RuntimeError("Defina a variável de ambiente DATABASE_URL com a conexão do PostgreSQL "
                           "(Supabase: Project Settings > Database > Connection string).")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(__name__, template_folder=os.path.join(root, "templates"),
                static_folder=None if cloudflare else os.path.join(root, "static"))
    app.config.update(
        DATABASE_URL=None if cloudflare else url,
        DB_SCHEMA=schema,
        ACCESS_PASSWORD=os.environ.get("ACCESS_PASSWORD") or None,
        ACCESS_USER=os.environ.get("ACCESS_USER") or "admin",
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        JSON_AS_ASCII=False,
        CLOUDFLARE=cloudflare,
        STATIC_VERSION="cf" if cloudflare else None,
    )
    if not cloudflare:
        init_db(url, schema)
    app.teardown_appcontext(close_db)

    @app.before_request
    def exigir_acesso():
        if request.path == "/healthz":
            return None
        password, user = _credenciais(app)
        if not password:
            if cloudflare:  # falha fechada: sem senha configurada no Worker, nada é servido
                return Response("Acesso bloqueado: configure o secret ACCESS_PASSWORD no Worker.", 503,
                                {"Cache-Control": "no-store"}, mimetype="text/plain")
            return None
        if _acesso_autorizado(password, user):
            return None
        return Response("Acesso restrito. Informe usuário e senha.", 401,
                        {"WWW-Authenticate": 'Basic realm="Sistema de clientes", charset="UTF-8"'})

    @app.get("/healthz")
    def healthz():
        """Verificação de saúde (Render/Cloudflare): serviço e banco respondem. Não expõe dados."""
        try:
            get_db().execute("SELECT 1")
        except dbmod.Error:
            return jsonify({"status": "indisponivel"}), 503
        return jsonify({"status": "ok"})

    from .routes import bp
    app.register_blueprint(bp)

    if cloudflare:
        from .cloudflare import register_static
        register_static(app)

    @app.errorhandler(413)
    def too_large(_):
        return jsonify({"erro": "Arquivo muito grande (limite de 25 MB)."}), 413

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"erro": "Não encontrado."}), 404

    @app.errorhandler(500)
    def server_error(_):
        return jsonify({"erro": "Erro interno. Tente novamente."}), 500

    return app
