"""Copia os dados de um banco SQLite do sistema para o PostgreSQL (Supabase).

Uso (a conexão fica na variável de ambiente, nunca na linha de comando):

    DATABASE_URL="postgresql://..." python scripts/migrar_sqlite_para_postgres.py data/clientes.db

Garantias:
- O arquivo SQLite de origem NÃO é alterado: o script trabalha numa cópia temporária.
- Os ids são preservados (relacionamentos continuam válidos) e as sequências são ajustadas depois.
- Se o destino já tiver dados, o script para. Use --substituir para apagar o destino e copiar de novo.
- Tudo ou nada: a transação só é confirmada se as contagens e a soma dos valores forem iguais.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clientes.db import _seed_curvas, connect, init_db  # noqa: E402

# Ordem respeita as chaves estrangeiras (pai antes do filho).
TABELAS = ["clientes", "pagamentos_mensais", "contratos", "contrato_valores",
           "importacoes", "configuracoes_importacao", "curvas_abc", "identificadores_externos"]


class MigracaoError(RuntimeError):
    pass


def _copia_origem(path: Path, pasta: Path) -> Path:
    """Copia o SQLite (e o -wal, se existir) para a pasta temporária. A origem não é tocada."""
    destino = pasta / path.name
    shutil.copy2(path, destino)
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        shutil.copy2(wal, pasta / wal.name)
    return destino


def _colunas_sqlite(lite: sqlite3.Connection, tabela: str) -> list[str]:
    return [r[1] for r in lite.execute(f"PRAGMA table_info({tabela})")]


def _tabelas_sqlite(lite: sqlite3.Connection) -> set[str]:
    return {r[0] for r in lite.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _colunas_destino(pg, tabela: str) -> list[str]:
    rows = pg.execute("SELECT column_name FROM information_schema.columns "
                      "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
                      (tabela,)).fetchall()
    return [r[0] for r in rows]


def _contagem(pg, tabela: str) -> int:
    return pg.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]


def migrar(origem: str | os.PathLike, url: str, schema: str | None = None,
           substituir: bool = False) -> dict:
    """Executa a migração e devolve um relatório com as contagens por tabela."""
    origem = Path(origem)
    if not origem.is_file():
        raise MigracaoError(f"Arquivo não encontrado: {origem}")

    init_db(url, schema)  # cria/atualiza as tabelas de destino (idempotente)
    report: dict = {"tabelas": {}, "valor_total_centavos": {}}

    with tempfile.TemporaryDirectory() as pasta:
        lite = sqlite3.connect(_copia_origem(origem, Path(pasta)))
        pg = connect(url, schema)
        try:
            tabelas_origem = _tabelas_sqlite(lite)
            # Destino com dados? (a Curva ABC é sempre substituída pela da origem, quando existir)
            ocupadas = [t for t in TABELAS if t != "curvas_abc" and _contagem(pg, t) > 0]
            if ocupadas and not substituir:
                raise MigracaoError("O banco de destino já tem dados em: " + ", ".join(ocupadas) +
                                    ". Para apagar esse conteúdo e copiar de novo, use --substituir.")
            pg.execute("TRUNCATE " + ", ".join(TABELAS) + " RESTART IDENTITY CASCADE")

            for tabela in TABELAS:
                if tabela not in tabelas_origem:
                    report["tabelas"][tabela] = {"origem": None, "destino": _contagem(pg, tabela)}
                    continue
                destino_cols = set(_colunas_destino(pg, tabela))
                colunas = [c for c in _colunas_sqlite(lite, tabela) if c in destino_cols]
                if not colunas:
                    raise MigracaoError(f"Nenhuma coluna de '{tabela}' pôde ser copiada.")
                lista = ", ".join(colunas)
                linhas = lite.execute(f"SELECT {lista} FROM {tabela} ORDER BY rowid").fetchall()
                if linhas:
                    marcas = ", ".join(["%s"] * len(colunas))
                    with pg.cursor() as cur:
                        cur.executemany(f"INSERT INTO {tabela} ({lista}) VALUES ({marcas})", linhas)
                if "id" in colunas:
                    pg.execute(
                        f"SELECT setval(pg_get_serial_sequence('{tabela}', 'id'), "
                        f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {tabela}")
                report["tabelas"][tabela] = {"origem": len(linhas), "destino": _contagem(pg, tabela)}

            if "curvas_abc" not in tabelas_origem:  # origem antiga, sem Curva ABC: volta aos valores iniciais
                _seed_curvas(pg)
                report["tabelas"]["curvas_abc"] = {"origem": None, "destino": _contagem(pg, "curvas_abc")}

            # Verificação: contagens e soma dos valores precisam bater antes de confirmar.
            for tabela, info in report["tabelas"].items():
                if info["origem"] is not None and info["origem"] != info["destino"]:
                    raise MigracaoError(f"Contagem diferente em '{tabela}': origem {info['origem']}, "
                                        f"destino {info['destino']}. Nada foi gravado.")
            for tabela, coluna in (("pagamentos_mensais", "valor_centavos"), ("contrato_valores", "valor_centavos")):
                if report["tabelas"].get(tabela, {}).get("origem") is None:
                    continue
                soma_lite = lite.execute(f"SELECT COALESCE(SUM({coluna}), 0) FROM {tabela}").fetchone()[0]
                soma_pg = pg.execute(f"SELECT COALESCE(SUM({coluna}), 0) FROM {tabela}").fetchone()[0]
                report["valor_total_centavos"][tabela] = {"origem": soma_lite, "destino": int(soma_pg)}
                if int(soma_lite) != int(soma_pg):
                    raise MigracaoError(f"Soma de '{tabela}' diferente. Nada foi gravado.")
            pg.commit()
        except Exception:
            pg.rollback()
            raise
        finally:
            pg.close()
            lite.close()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("origem", help="caminho do arquivo SQLite (ex.: data/clientes.db)")
    parser.add_argument("--schema", default=None, help="schema de destino (padrão: public)")
    parser.add_argument("--substituir", action="store_true",
                        help="apaga os dados do destino antes de copiar (destrutivo)")
    args = parser.parse_args(argv)
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("Defina a variável de ambiente DATABASE_URL.", file=sys.stderr)
        return 2
    try:
        report = migrar(args.origem, url, args.schema, args.substituir)
    except MigracaoError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    print("Migração concluída. Linhas por tabela (origem -> destino):")
    for tabela, info in report["tabelas"].items():
        print(f"  {tabela}: {info['origem']} -> {info['destino']}")
    for tabela, info in report["valor_total_centavos"].items():
        print(f"  soma de valores em {tabela}: {info['origem']} -> {info['destino']} (centavos)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
