"""Cria/atualiza as tabelas do sistema (migração de esquema), separado da aplicação.

No Cloudflare, o Worker conecta com um usuário sem permissão de DDL e nunca altera o esquema.
Quando uma nova versão do sistema trouxer tabelas ou colunas novas, rode este script uma vez,
com o usuário de migração (o "postgres" do Supabase), ANTES de publicar o Worker:

    set MIGRATION_DATABASE_URL=postgresql://postgres:SENHA@db.<projeto>.supabase.co:5432/postgres
    python scripts/migrar_schema.py

É idempotente (CREATE ... IF NOT EXISTS / ADD COLUMN IF NOT EXISTS) e nunca apaga dados.
Depois de criar tabelas novas, rode também scripts/supabase_cloudflare.sql (permissões e RLS).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clientes.db import init_db  # noqa: E402


def main() -> int:
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        print("Defina MIGRATION_DATABASE_URL com a conexão do usuário de migração (postgres do Supabase).")
        return 2
    init_db(url)
    print("Esquema conferido/atualizado. Lembre de aplicar scripts/supabase_cloudflare.sql se houver tabela nova.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
