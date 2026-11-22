"""Sistema de Inteligência Geográfica Comercial — cadastro de clientes, importação e Curva ABC.

Configuração por variáveis de ambiente (ver .env.example):
  DATABASE_URL           conexão do PostgreSQL (Supabase). Obrigatória no ambiente local.
  SUPABASE_URL              endereço do projeto Supabase (ex.: https://<ref>.supabase.co).
  SUPABASE_PUBLISHABLE_KEY  chave publicável do projeto (não é segredo; a service_role NUNCA é usada).

Login com Supabase Auth (e-mail e senha), validado no backend; só entra quem está ativo na tabela
usuarios_autorizados (ver clientes/autenticacao.py).

No Cloudflare Workers (src/worker.py, `create_app(cloudflare=True)`):
  - o banco vem do binding HYPERDRIVE, a cada requisição (ver clientes/db.py);
  - SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY vêm das variáveis do Worker; o login é obrigatório;
  - o esquema do banco não é alterado ao iniciar (migrações: scripts/migrar_schema.py);
  - CSS e JS vêm do Workers Static Assets (ver clientes/cloudflare.py).
"""
from __future__ import annotations

import os

from flask import Flask, jsonify

from . import db as dbmod
from .autenticacao import exige_login_no_render, exigir_login, renovar_cookies
from .db import close_db, get_db, init_db


def create_app(database_url: str | None = None, schema: str | None = None, *, cloudflare: bool = False) -> Flask:
    """Cria a aplicação. `database_url` padrão: variável de ambiente DATABASE_URL.

    `schema` é usado apenas pelos testes, para isolar cada teste em um schema próprio.
    `cloudflare=True` é usado só por src/worker.py (ver a descrição do módulo).
    """
    exige_login_no_render()
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
        SUPABASE_URL=os.environ.get("SUPABASE_URL") or None,
        SUPABASE_PUBLISHABLE_KEY=os.environ.get("SUPABASE_PUBLISHABLE_KEY") or None,
        LOGIN_OBRIGATORIO=cloudflare or bool(os.environ.get("RENDER")),
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        JSON_AS_ASCII=False,
        CLOUDFLARE=cloudflare,
        STATIC_VERSION="cf" if cloudflare else None,
    )
    if not cloudflare:
        init_db(url, schema)
    app.teardown_appcontext(close_db)

    app.before_request(exigir_login)
    app.after_request(renovar_cookies)

    @app.get("/healthz")
    def healthz():
        """Verificação de saúde (Render/Cloudflare): serviço e banco respondem. Não expõe dados."""
        try:
            get_db().execute("SELECT 1")
        except dbmod.Error:
            return jsonify({"status": "indisponivel"}), 503
        return jsonify({"status": "ok"})

    from .autenticacao import bp as bp_auth
    from .routes import bp
    app.register_blueprint(bp_auth)
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
