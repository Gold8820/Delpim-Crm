"""Acesso aos dados de clientes e pagamentos mensais."""
from __future__ import annotations

from datetime import datetime
from collections.abc import Mapping

from .classification import LABELS, classify, ordenar, validate_faixas
from .validation import (CLIENT_FIELDS, display_name, format_cep, format_cnpj,
                         norm_search, search_blob)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def serialize(row: Mapping) -> dict:
    data = {key: row[key] for key in ("id",) + CLIENT_FIELDS}
    data["cnpj"] = format_cnpj(data["cnpj"])
    data["cep"] = format_cep(data["cep"])
    data["nome"] = display_name(data)
    keys = row.keys()
    if "valor_centavos" in keys:
        data["valor_centavos"] = row["valor_centavos"]
        data["classificacao"] = row["classificacao"]
    return data


# --------------------------------------------------------------------------- consultas

# Ordenação: COLLATE "C" reproduz a ordem binária do SQLite. Textos "de nome" são ordenados
# sem acentos e sem diferença de maiúsculas (unaccent + lower), como a antiga collation PTBR.
SORTS = {
    "codigo": ("CASE WHEN c.codigo ~ '[^0-9]' THEN 1 ELSE 0 END {d}, "
               "CASE WHEN c.codigo ~ '[^0-9]' THEN NULL ELSE COALESCE(NULLIF(c.codigo, '')::numeric, 0) END {d}, "
               "c.codigo COLLATE \"C\" {d}"),
    "nome": "unaccent(lower(COALESCE(c.nome_fantasia, c.razao_social))) COLLATE \"C\" {d}",
    "cnpj": "c.cnpj IS NULL, c.cnpj COLLATE \"C\" {d}",
    "cidade": "c.cidade IS NULL, unaccent(lower(c.cidade)) COLLATE \"C\" {d}",
    "estado": "c.estado IS NULL, c.estado COLLATE \"C\" {d}",
    "segmento": "c.segmento IS NULL, unaccent(lower(c.segmento)) COLLATE \"C\" {d}",
    "vendedor": "c.vendedor IS NULL, unaccent(lower(c.vendedor)) COLLATE \"C\" {d}",
    "valor": "p.valor_centavos IS NULL, p.valor_centavos {d}",
    "classificacao": "p.valor_centavos IS NULL, p.valor_centavos {d}",
}


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def latest_month(db) -> str | None:
    return db.execute("SELECT MAX(mes_referencia) FROM pagamentos_mensais").fetchone()[0]


def list_months(db) -> list[str]:
    rows = db.execute("SELECT DISTINCT mes_referencia FROM pagamentos_mensais ORDER BY 1 DESC").fetchall()
    return [r[0] for r in rows]


def list_segments(db) -> list[str]:
    rows = db.execute(
        "SELECT MIN(segmento) FROM clientes WHERE segmento IS NOT NULL "
        "GROUP BY lower(segmento) ORDER BY unaccent(lower(MIN(segmento))) COLLATE \"C\"").fetchall()
    return [r[0] for r in rows]


def list_clients(db, *, q="", classificacao="", segmento="", mes=None,
                 sort="codigo", direction="asc", page=1, per_page=25) -> dict:
    mes = mes or latest_month(db)
    where = ["1=1"]
    params: dict = {"mes": mes}

    for i, token in enumerate(norm_search(q).split()):
        where.append(f"c.busca LIKE %(q{i})s ESCAPE '\\'")
        params[f"q{i}"] = f"%{_escape_like(token)}%"
    if classificacao in LABELS:
        where.append("p.classificacao = %(cls)s")
        params["cls"] = classificacao
    elif classificacao == "none":
        where.append("p.classificacao IS NULL")
    if segmento:
        where.append("lower(c.segmento) = lower(%(seg)s)")
        params["seg"] = segmento

    direction = "DESC" if str(direction).lower() == "desc" else "ASC"
    order = SORTS.get(sort, SORTS["codigo"]).format(d=direction) + ", c.id ASC"
    per_page = max(5, min(int(per_page), 200))
    page = max(1, int(page))

    base = ("FROM clientes c LEFT JOIN pagamentos_mensais p "
            "ON p.cliente_id = c.id AND p.mes_referencia = %(mes)s "
            "WHERE " + " AND ".join(where))
    total = db.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
    rows = db.execute(
        f"SELECT c.*, p.valor_centavos, p.classificacao {base} ORDER BY {order} "
        "LIMIT %(limit)s OFFSET %(offset)s",
        {**params, "limit": per_page, "offset": (page - 1) * per_page}).fetchall()
    return {
        "clientes": [serialize(r) for r in rows],
        "total": total,
        "total_geral": db.execute("SELECT COUNT(*) FROM clientes").fetchone()[0],
        "pagina": page,
        "por_pagina": per_page,
        "mes": mes,
    }


def get_client(db, client_id: int) -> dict | None:
    return db.execute("SELECT * FROM clientes WHERE id = %s", (client_id,)).fetchone()


def find_by_codigo(db, codigo: str | None, exclude_id: int | None = None):
    if not codigo:
        return None
    return db.execute("SELECT * FROM clientes WHERE lower(codigo) = lower(%s) AND id IS DISTINCT FROM %s",
                      (codigo, exclude_id)).fetchone()


def find_by_cnpj(db, cnpj: str | None, exclude_id: int | None = None):
    if not cnpj:
        return None
    return db.execute("SELECT * FROM clientes WHERE cnpj = %s AND id IS DISTINCT FROM %s",
                      (cnpj, exclude_id)).fetchone()


def list_payments(db, client_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT mes_referencia, valor_centavos, classificacao, origem FROM pagamentos_mensais "
        "WHERE cliente_id = %s ORDER BY mes_referencia DESC", (client_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- escrita
# Estas funções NÃO fazem commit: quem chama controla a transação.

def insert_client(db, values: dict) -> int:
    stamp = now()
    columns = list(CLIENT_FIELDS) + ["busca", "criado_em", "atualizado_em"]
    data = {**{k: values.get(k) for k in CLIENT_FIELDS}, "busca": search_blob(values),
            "criado_em": stamp, "atualizado_em": stamp}
    cursor = db.execute(
        f"INSERT INTO clientes ({', '.join(columns)}) VALUES ({', '.join('%(' + c + ')s' for c in columns)}) "
        "RETURNING id", data)
    return cursor.fetchone()["id"]


def update_client(db, client_id: int, values: dict) -> None:
    data = {k: values.get(k) for k in CLIENT_FIELDS}
    data.update(busca=search_blob(values), atualizado_em=now(), id=client_id)
    assignments = ", ".join(f"{k} = %({k})s" for k in list(CLIENT_FIELDS) + ["busca", "atualizado_em"])
    db.execute(f"UPDATE clientes SET {assignments} WHERE id = %(id)s", data)


def load_faixas(db) -> list[dict]:
    """Faixas da Curva ABC salvas no banco, da maior para a menor classificação."""
    rows = db.execute("SELECT classificacao, minimo_centavos, maximo_centavos FROM curvas_abc").fetchall()
    return ordenar([{"classificacao": r["classificacao"], "minimo_centavos": r["minimo_centavos"],
                     "maximo_centavos": r["maximo_centavos"]} for r in rows])


def save_faixas(db, faixas: list[dict]) -> int:
    """Grava as faixas (já validadas) e reclassifica todos os pagamentos mensais. Não faz commit.

    Retorna quantos pagamentos mudaram de classificação.
    """
    errors = validate_faixas(faixas)
    if errors:
        raise ValueError("Faixas inválidas: " + "; ".join(errors.values()))
    stamp = now()
    for f in faixas:
        db.execute(
            "INSERT INTO curvas_abc (classificacao, minimo_centavos, maximo_centavos, atualizado_em) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (classificacao) DO UPDATE SET "
            "minimo_centavos = excluded.minimo_centavos, maximo_centavos = excluded.maximo_centavos, "
            "atualizado_em = excluded.atualizado_em",
            (f["classificacao"], f["minimo_centavos"], f["maximo_centavos"], stamp))
    return reclassify_all(db, ordenar(faixas))


def reclassify_all(db, faixas: list[dict]) -> int:
    """Recalcula a classificação de todos os pagamentos mensais. Não faz commit."""
    changed = 0
    stamp = now()
    rows = db.execute("SELECT id, valor_centavos, classificacao FROM pagamentos_mensais").fetchall()
    for row in rows:
        new = classify(row["valor_centavos"], faixas)
        if new != row["classificacao"]:
            db.execute("UPDATE pagamentos_mensais SET classificacao = %s, atualizado_em = %s WHERE id = %s",
                       (new, stamp, row["id"]))
            changed += 1
    return changed


def upsert_payment(db, client_id: int, month: str, amount_cents: int, origem: str,
                   faixas: list[dict] | None = None) -> str:
    """Grava o valor do mês. Se já existir, SUBSTITUI (nunca soma). Recalcula a classificação."""
    stamp = now()
    faixas = faixas if faixas is not None else load_faixas(db)
    classe = classify(amount_cents, faixas)
    db.execute(
        "INSERT INTO pagamentos_mensais "
        "(cliente_id, mes_referencia, valor_centavos, classificacao, origem, criado_em, atualizado_em) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (cliente_id, mes_referencia) DO UPDATE SET "
        "valor_centavos = excluded.valor_centavos, classificacao = excluded.classificacao, "
        "origem = excluded.origem, atualizado_em = excluded.atualizado_em",
        (client_id, month, amount_cents, classe, origem, stamp, stamp))
    return classe


def delete_client(db, client_id: int) -> None:
    db.execute("DELETE FROM clientes WHERE id = %s", (client_id,))


# --------------------------------------------------------------------------- contratos

def find_contract(db, numero: str | None):
    if not numero:
        return None
    return db.execute("SELECT * FROM contratos WHERE lower(numero) = lower(%s)", (numero,)).fetchone()


def list_client_contracts(db, client_id: int) -> list[dict]:
    """Contratos do cliente, com o último valor mensal registrado de cada um."""
    rows = db.execute(
        "SELECT ct.id, ct.numero, ct.produto, ct.situacao, ct.data_inicio, ct.data_fim, ct.observacoes, "
        "ct.valor_parcela_futuro_centavos AS parcela_futuro_centavos, "
        "(SELECT mes_referencia FROM contrato_valores WHERE contrato_id = ct.id ORDER BY mes_referencia DESC LIMIT 1) AS ultimo_mes, "
        "(SELECT valor_centavos FROM contrato_valores WHERE contrato_id = ct.id ORDER BY mes_referencia DESC LIMIT 1) AS ultimo_valor_centavos "
        "FROM contratos ct WHERE ct.cliente_id = %s ORDER BY lower(ct.numero) COLLATE \"C\", ct.numero COLLATE \"C\"",
        (client_id,)).fetchall()
    return [dict(r) for r in rows]


def insert_contract(db, client_id: int, numero: str, values: dict) -> int:
    stamp = now()
    cursor = db.execute(
        "INSERT INTO contratos (cliente_id, numero, produto, situacao, data_inicio, data_fim, "
        "valor_parcela_futuro_centavos, observacoes, criado_em, atualizado_em) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (client_id, numero, values.get("produto"), values.get("situacao"), values.get("data_inicio"),
         values.get("data_fim"), values.get("parcela_centavos"), values.get("observacoes"), stamp, stamp))
    return cursor.fetchone()["id"]


def update_contract(db, contract_id: int, values: dict) -> None:
    db.execute(
        "UPDATE contratos SET produto = %s, situacao = %s, data_inicio = %s, data_fim = %s, "
        "valor_parcela_futuro_centavos = %s, observacoes = %s, atualizado_em = %s WHERE id = %s",
        (values.get("produto"), values.get("situacao"), values.get("data_inicio"), values.get("data_fim"),
         values.get("parcela_centavos"), values.get("observacoes"), now(), contract_id))


def set_contract_note(db, client_id: int, contract_id: int, note: str | None) -> bool:
    """Edição manual da observação de um contrato do cliente. Não faz commit."""
    cursor = db.execute("UPDATE contratos SET observacoes = %s, atualizado_em = %s WHERE id = %s AND cliente_id = %s",
                        (note, now(), contract_id, client_id))
    return cursor.rowcount == 1


def upsert_contract_value(db, contract_id: int, month: str, cents: int) -> None:
    """Valor do contrato no mês: se já existir, é SUBSTITUÍDO (nunca somado)."""
    db.execute(
        "INSERT INTO contrato_valores (contrato_id, mes_referencia, valor_centavos, atualizado_em) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (contrato_id, mes_referencia) DO UPDATE SET "
        "valor_centavos = excluded.valor_centavos, atualizado_em = excluded.atualizado_em",
        (contract_id, month, cents, now()))
