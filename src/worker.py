"""Entrada do Cloudflare Worker (Python Workers + WSGI).

A mesma aplicação Flask do `python run.py`, criada no modo Cloudflare:
  - banco pelo binding HYPERDRIVE (uma conexão por requisição, fechada no fim dela);
  - sem alteração de esquema na inicialização (migrações: scripts/migrar_schema.py);
  - sem login próprio: o Cloudflare Access fica na frente e a aplicação confere o token dele
    (variáveis CF_ACCESS_TEAM_DOMAIN e CF_ACCESS_AUD; sem elas, nada é servido);
  - CSS/JS pelo Workers Static Assets.
"""
# Importados aqui, no início, para entrarem no "memory snapshot" que a Cloudflare grava no deploy.
# Sem isso, cada nova instância importaria esses módulos na primeira requisição (CPU cobrada nela,
# o que estoura o limite de CPU do plano gratuito, especialmente na importação de planilhas).
import pg8000.dbapi  # noqa: F401  driver PostgreSQL usado com o Hyperdrive
import openpyxl  # noqa: F401  leitura de .xlsx
import defusedxml.ElementTree  # noqa: F401  leitura segura de .xls (XML)
from workers import wsgi

from clientes import create_app
from clientes import acesso, cloudflare, configs, contracts, importer  # noqa: F401

app = create_app(cloudflare=True)

Default = wsgi.entrypoint(app)
