"""Mapeamentos de colunas salvos (configurações de importação).

Cada configuração guarda, para cada campo do sistema, o NOME do cabeçalho da planilha
(e não a posição da coluna). Assim, se as colunas mudarem de ordem, a associação continua valendo;
se um cabeçalho mudar de nome ou sumir, só aquele campo precisa ser corrigido.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
import psycopg

from .importer import fuzzy_suggest, labels_for, slug, suggest_mapping
from .repository import now

TIPOS = ("clientes", "contratos")


class ConfigError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _row(r: Mapping) -> dict:
    mapping = json.loads(r["mapeamento"])
    return {"id": r["id"], "tipo": r["tipo"], "nome": r["nome"], "mapeamento": mapping,
            "chave": r["chave_relacionamento"], "cabecalhos": json.loads(r["cabecalhos"]),
            "padrao": bool(r["padrao"]), "campos_associados": sum(1 for v in mapping.values() if v),
            "criado_em": r["criado_em"], "atualizado_em": r["atualizado_em"]}


def list_configs(db, tipo: str | None = None) -> list[dict]:
    sql = "SELECT * FROM configuracoes_importacao"
    rows = db.execute(sql + (" WHERE tipo = %s" if tipo else "") + " ORDER BY tipo, padrao DESC, lower(nome) COLLATE \"C\", nome COLLATE \"C\"",
                      (tipo,) if tipo else ()).fetchall()
    return [_row(r) for r in rows]


def get_config(db, config_id) -> dict | None:
    r = db.execute("SELECT * FROM configuracoes_importacao WHERE id = %s", (config_id,)).fetchone()
    return _row(r) if r else None


def default_config(db, tipo: str) -> dict | None:
    r = db.execute("SELECT * FROM configuracoes_importacao WHERE tipo = %s AND padrao = 1", (tipo,)).fetchone()
    return _row(r) if r else None


def _clean(tipo: str, nome, mapeamento, chave, cabecalhos):
    if tipo not in TIPOS:
        raise ConfigError("Tipo de planilha inválido.")
    nome = " ".join(str(nome or "").split())
    if not nome:
        raise ConfigError("Informe um nome para a configuração.")
    if len(nome) > 80:
        raise ConfigError("O nome pode ter no máximo 80 caracteres.")
    labels = labels_for(tipo)
    if not isinstance(mapeamento, dict):
        raise ConfigError("Mapeamento inválido.")
    mapping = {}
    for key, value in mapeamento.items():
        if key not in labels:
            raise ConfigError(f"Campo desconhecido: {key}.")
        text = " ".join(str(value).split()) if value not in (None, "") else ""
        mapping[key] = text or None
    if not any(mapping.values()):
        raise ConfigError("Associe pelo menos um campo a uma coluna.")
    if tipo == "contratos":
        chave = chave or "cnpj"
        if chave not in ("cnpj", "codigo"):
            raise ConfigError("Campo de relacionamento inválido (use CNPJ ou código).")
        if not mapping.get("chave_cliente"):
            raise ConfigError("Informe a coluna que identifica o cliente na planilha de contratos.")
    else:
        chave = None
    heads = [str(h) for h in (cabecalhos or []) if h][:200]
    return nome, mapping, chave, heads


def save_config(db, tipo, nome, mapeamento, chave=None, cabecalhos=None, padrao=False,
                config_id: int | None = None) -> dict:
    nome, mapping, chave, heads = _clean(tipo, nome, mapeamento, chave, cabecalhos)
    stamp = now()
    try:
        if config_id is None:
            cur = db.execute(
                "INSERT INTO configuracoes_importacao (tipo, nome, mapeamento, chave_relacionamento, cabecalhos, "
                "padrao, criado_em, atualizado_em) VALUES (%s, %s, %s, %s, %s, 0, %s, %s) RETURNING id",
                (tipo, nome, json.dumps(mapping, ensure_ascii=False), chave,
                 json.dumps(heads, ensure_ascii=False), stamp, stamp))
            config_id = cur.fetchone()["id"]
        else:
            current = get_config(db, config_id)
            if current is None or current["tipo"] != tipo:
                raise ConfigError("Configuração não encontrada.", 404)
            db.execute(
                "UPDATE configuracoes_importacao SET nome = %s, mapeamento = %s, chave_relacionamento = %s, "
                "cabecalhos = %s, atualizado_em = %s WHERE id = %s",
                (nome, json.dumps(mapping, ensure_ascii=False), chave,
                 json.dumps(heads or current["cabecalhos"], ensure_ascii=False), stamp, config_id))
        if padrao:
            _set_default(db, config_id)
        db.commit()
    except psycopg.IntegrityError:
        db.rollback()
        raise ConfigError("Já existe uma configuração com esse nome.", 409) from None
    return get_config(db, config_id)


def _set_default(db, config_id: int) -> None:
    tipo = db.execute("SELECT tipo FROM configuracoes_importacao WHERE id = %s", (config_id,)).fetchone()[0]
    db.execute("UPDATE configuracoes_importacao SET padrao = 0 WHERE tipo = %s AND padrao = 1", (tipo,))
    db.execute("UPDATE configuracoes_importacao SET padrao = 1, atualizado_em = %s WHERE id = %s", (now(), config_id))


def set_default(db, config_id: int) -> dict:
    if get_config(db, config_id) is None:
        raise ConfigError("Configuração não encontrada.", 404)
    _set_default(db, config_id)
    db.commit()
    return get_config(db, config_id)


def delete_config(db, config_id: int) -> None:
    if get_config(db, config_id) is None:
        raise ConfigError("Configuração não encontrada.", 404)
    db.execute("DELETE FROM configuracoes_importacao WHERE id = %s", (config_id,))
    db.commit()


def unique_name(db, tipo: str, base: str) -> str:
    names = {c["nome"].lower() for c in list_configs(db, tipo)}
    name, n = base, 2
    while name.lower() in names:
        name, n = f"{base} ({n})", n + 1
    return name


# --------------------------------------------------------------------------- aplicar a uma planilha

def names_from_indexes(header: list[str], mapping: dict) -> dict:
    """{campo: índice} -> {campo: nome do cabeçalho}, para salvar."""
    return {k: (header[i] if isinstance(i, int) and 0 <= i < len(header) else None) for k, i in mapping.items()}


def apply_to_header(config: dict, header: list[str], tipo: str) -> dict:
    """Aplica uma configuração salva aos cabeçalhos de uma planilha, pelo NOME das colunas.

    Retorna {"mapeamento": {campo: índice|None}, "pendencias": {campo: {...}}}. Só entram em
    `pendencias` os campos cuja coluna salva não existe mais; os demais continuam associados.
    """
    labels = labels_for(tipo)
    index = {}
    for i, h in reversed(list(enumerate(header))):   # a primeira coluna com o nome vence
        index[slug(h)] = i
    mapping = {k: None for k in labels}
    pendencias: dict = {}
    for key, name in config["mapeamento"].items():
        if key not in labels or not name:
            continue
        i = index.get(slug(name))
        if i is None:
            pendencias[key] = {"coluna": name, "sugestao": None,
                               "motivo": f"A coluna “{name}” não foi encontrada nesta planilha."}
        else:
            mapping[key] = i
    if pendencias:
        used = {v for v in mapping.values() if v is not None}
        guess = suggest_mapping(header, tipo)
        for key, info in pendencias.items():
            g = guess.get(key)
            if g is None or g in used:
                g = fuzzy_suggest(header, tipo, key, info["coluna"], used)
            if g is not None and g not in used:
                info["sugestao"] = g
                mapping[key] = g          # pré-seleciona, mas continua destacado até o usuário confirmar
                used.add(g)
    return {"mapeamento": mapping, "pendencias": pendencias}
