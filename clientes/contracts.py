"""Planilha de contratos: relaciona cada contrato ao seu cliente dentro da análise da importação.

Regras principais
  * O cliente é encontrado pela chave escolhida (CNPJ ou código), ignorando pontos, barras e traços.
    Procura primeiro na planilha de clientes desta importação e depois no cadastro já existente.
  * Um cliente pode ter vários contratos; o contrato é identificado pelo seu número (único no sistema).
  * O valor mensal pago do contrato é gravado por mês (substitui, nunca soma). O total do cliente no mês
    é a soma dos valores dos seus contratos naquele mês, e é ele que define a classificação.
  * O campo "valor parcela futuro" é guardado só como informação; não entra no cálculo.
  * Fonte do valor pago (configuração "fonte_valor_pago"): com a opção ligada, contrato encerrado (e, se
    escolhido, contrato com parcelas em atraso) conta R$ 0,00 no mês; desligada, os valores do mês não mudam.
  * Demais colunas reconhecidas (Empresa, Vigência, Quantidade Parcelas...) ficam em dados_extras.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .classification import classify
from .repository import _json_obj
from .validation import (CONTRACT_CLIENT_FIELDS, CONTRACT_EXTRA_LABELS, CONTRACT_LABELS, display_name, extras_from,
                         flag_sim, merge_extras, format_cnpj,
                         format_money, is_blank, key_cnpj, key_codigo, merge_note, month_label,
                         normalize_contract, to_text)

TIPOS = {
    "invalido": "Informação inválida",
    "duplicado": "Registro duplicado",
    "conflito": "Conflito de identificação",
    "sem_cliente": "Contrato sem cliente correspondente",
    "cliente_sem_contrato": "Cliente sem contrato",
    "campo_ignorado": "Campo inválido ignorado (registro importado)",
}

# campo da planilha -> chave do valor normalizado
_FIELD_MAP = {"produto": "produto", "situacao": "situacao", "data_inicio": "data_inicio",
              "data_fim": "data_fim", "valor_parcela_futuro": "parcela_centavos", "observacoes": "observacoes"}
_VALUE_KEYS = tuple(_FIELD_MAP.values())
_VALUE_LABELS = {"produto": "Produto / plano", "situacao": "Situação", "data_inicio": "Data de início",
                 "data_fim": "Data de término", "parcela_centavos": "Valor parcela futuro",
                 "observacoes": "Observações"}


@dataclass
class ContractRec:
    numero: str
    entity: object
    db_id: int | None
    is_new: bool
    values: dict
    first_line: int
    months: dict = field(default_factory=dict)          # mês -> centavos lidos na planilha
    month_lines: dict = field(default_factory=dict)     # mês -> linha
    changed_months: dict = field(default_factory=dict)  # mês -> centavos que serão gravados
    fields_changed: bool = False
    extras: dict = field(default_factory=dict)          # dados adicionais (dados_extras)

    @property
    def dirty(self) -> bool:
        return self.is_new or self.fields_changed or bool(self.changed_months)


def _inteiro(value) -> int:
    text = to_text(value)
    try:
        return int(float(text.replace(",", "."))) if text else 0
    except ValueError:
        return 0


def _show(key: str, value) -> str:
    if value is None or value == "":
        return "(vazio)"
    if key == "parcela_centavos":
        return format_money(value)
    if key == "observacoes":
        return value if len(value) <= 80 else value[:77] + "..."
    if key in ("data_inicio", "data_fim"):
        return f"{value[8:]}/{value[5:7]}/{value[:4]}"
    return str(value)


def _content(header, cells) -> str:
    parts = [f"{h}: {t}" for h, c in zip(header, cells) if (t := to_text(c))]
    return "; ".join(parts)[:600]


def _label_error(key: str, message: str) -> str:
    label = CONTRACT_LABELS[key]
    short = label.split(" (")[0]
    return message if message.lower().startswith(short.split()[0].lower()) else f"{short}: {message}"


def process(ctx) -> dict:
    table, db = ctx.table, ctx.db
    header = table["header"]
    mapped = {k: v for k, v in ctx.mapping.items() if v is not None}
    by_cnpj = ctx.chave == "cnpj"
    extra_keys = [k for k in CONTRACT_EXTRA_LABELS if k in mapped]
    fonte = getattr(ctx, "fonte", None) or {"ativo": True, "excluir_encerrados": True, "excluir_atrasados": False}

    db_contracts = {r["numero"].lower(): dict(r) for r in db.execute(
        "SELECT ct.*, c.codigo AS cliente_codigo FROM contratos ct JOIN clientes c ON c.id = ct.cliente_id")}
    db_contract_values: dict[int, dict] = {}
    db_month: dict[tuple, dict] = {}   # (cliente_id, mês) -> {número do contrato: centavos}
    for contrato_id, cliente_id, numero, mes, cents in db.execute(
            "SELECT v.contrato_id, ct.cliente_id, ct.numero, v.mes_referencia, v.valor_centavos "
            "FROM contrato_valores v JOIN contratos ct ON ct.id = v.contrato_id"):
        db_contract_values.setdefault(contrato_id, {})[mes] = cents
        db_month.setdefault((cliente_id, mes), {})[numero.lower()] = cents

    recs: dict[str, ContractRec] = {}
    rows_out: list[dict] = []
    incons: list[dict] = []
    stats = {"contratos_lidos": 0, "contratos_relacionados": 0, "contratos_sem_cliente": 0,
             "contratos_rejeitados": 0, "clientes_sem_contrato": 0,
             "pagamentos_novos": 0, "pagamentos_substituidos": 0, "pagamentos_iguais": 0}

    for line, cells in table["rows"]:
        raw = {key: (cells[idx] if idx < len(cells) else None) for key, idx in mapped.items()}
        if all(is_blank(v) for v in raw.values()):
            continue
        stats["contratos_lidos"] += 1
        clean, errors = normalize_contract(raw)
        numero = clean.get("numero_contrato")
        month = clean["mes"] or ctx.default_month
        row = {"linha": line, "status": None, "contrato": numero, "cliente": None, "codigo": None,
               "cnpj": None, "produto": clean.get("produto"), "situacao": clean.get("situacao"),
               "mes": month if clean["valor_centavos"] is not None else None,
               "valor_centavos": clean["valor_centavos"], "parcela_centavos": clean["parcela_centavos"],
               "mudancas": [], "erros": [], "avisos": []}
        row_notes: list[str] = []

        def reject(kind: str, *messages: str) -> None:
            row["status"] = "rejeitado"
            row["erros"] = list(messages)
            stats["contratos_rejeitados"] += 1
            if kind == "sem_cliente":
                stats["contratos_sem_cliente"] += 1
            rows_out.append(row)
            incons.append({"planilha": "contratos", "linha": line, "tipo": kind, "tipo_rotulo": TIPOS[kind],
                           "identificacao": numero or "", "nome": row["cliente"] or clean.get("chave") or "",
                           "motivo": " | ".join(messages), "conteudo": _content(header, cells)})

        if errors:
            reject("invalido", *(_label_error(k, m) for k, m in errors.items()))
            continue
        if not numero:
            reject("invalido", "Informe o número do contrato.")
            continue
        if not clean["chave"]:
            reject("invalido", "Informe o " + ("CNPJ/documento" if by_cnpj else "código") + " do cliente.")
            continue
        if clean["valor_centavos"] is not None and not month:
            reject("invalido", "Mês de referência não informado para o valor desta linha.")
            continue

        key = key_cnpj(clean["chave"]) if by_cnpj else key_codigo(clean["chave"])
        entity = ctx.find_entity(ctx.chave, key)
        if entity is None:
            rejected_line = ctx.rejected_line(ctx.chave, key)
            if rejected_line:
                reject("sem_cliente", f"O cliente deste contrato foi rejeitado na planilha de clientes "
                                      f"(linha {rejected_line}). Corrija-o e importe novamente.")
            else:
                reject("sem_cliente", f"Nenhum cliente com {'CNPJ' if by_cnpj else 'código'} "
                                      f"“{clean['chave']}” foi encontrado na planilha de clientes nem no cadastro.")
            continue
        row.update(cliente=entity.name, codigo=entity.values.get("codigo"),
                   cnpj=format_cnpj(entity.values.get("cnpj")))

        numero_l = numero.lower()
        rec = recs.get(numero_l)
        db_row = db_contracts.get(numero_l)
        if rec is not None:
            if rec.entity is not entity:
                reject("conflito", f"O contrato {numero} já aparece na linha {rec.first_line} "
                                   f"vinculado a outro cliente ({rec.entity.name}).")
                continue
            if clean["valor_centavos"] is None or month in rec.months:
                suffix = f" para o mês {month_label(month)}" if clean["valor_centavos"] is not None else ""
                reject("duplicado", f"Contrato repetido na planilha (já aparece na linha {rec.first_line}{suffix}).")
                continue
        elif db_row is not None and db_row["cliente_id"] != entity.db_id:
            reject("conflito", f"O contrato {numero} já está cadastrado para outro cliente "
                               f"(código {db_row['cliente_codigo']}).")
            continue

        # --- campos do contrato
        incoming = {_FIELD_MAP[f]: clean[_FIELD_MAP[f]] for f in _FIELD_MAP if f in mapped}
        if rec is None:
            if db_row is not None:
                base = {"produto": db_row["produto"], "situacao": db_row["situacao"],
                        "data_inicio": db_row["data_inicio"], "data_fim": db_row["data_fim"],
                        "parcela_centavos": db_row["valor_parcela_futuro_centavos"],
                        "observacoes": db_row["observacoes"]}
                is_new, use_empty = False, ctx.replace_empty
            else:
                base = {k: None for k in _VALUE_KEYS}
                is_new, use_empty = True, False
            rec = ContractRec(numero=db_row["numero"] if db_row else numero, entity=entity,
                              db_id=db_row["id"] if db_row else None, is_new=is_new,
                              values=dict(base), first_line=line,
                              extras=_json_obj(db_row.get("dados_extras")) if db_row else {})
            recs[numero_l] = rec
            entity.contract_recs.append(rec)
        else:
            use_empty = False   # linha adicional do mesmo contrato (outro mês): só completa informações
        merged = dict(rec.values)
        if "observacoes" in incoming:
            # observações nunca são apagadas por célula vazia nem trocadas silenciosamente
            merged["observacoes"], note_warning = merge_note(rec.values.get("observacoes"),
                                                             incoming.pop("observacoes"))
            if note_warning:
                row_notes.append({"campo": "Observações", "de": "—",
                                  "para": "mantida a cadastrada (texto diferente na planilha)"})
        for k, new in incoming.items():
            if new is not None or use_empty:
                merged[k] = new
        changes = [{"campo": _VALUE_LABELS[k], "de": _show(k, rec.values[k]), "para": _show(k, merged[k])}
                   for k in _VALUE_KEYS if merged[k] != rec.values[k]] if not rec.is_new else []
        if merged != rec.values:
            rec.fields_changed = rec.fields_changed or not rec.is_new
            rec.values = merged

        # --- dados adicionais do contrato (nunca apagados por célula vazia)
        incoming_extras = extras_from(raw, extra_keys)
        merged_extras = merge_extras(rec.extras, incoming_extras)
        if merged_extras != rec.extras:
            if not rec.is_new:
                changes += [{"campo": CONTRACT_EXTRA_LABELS[k], "de": rec.extras.get(k) or "(vazio)",
                             "para": merged_extras[k]} for k in incoming_extras if rec.extras.get(k) != merged_extras[k]]
                rec.fields_changed = True
            rec.extras = merged_extras

        # --- valor pago no mês, conforme a fonte configurada
        if not fonte.get("ativo", True):
            clean["valor_centavos"] = None
        elif clean["valor_centavos"] is not None:
            motivo = None
            if fonte.get("excluir_encerrados") and flag_sim(raw.get("encerrado")):
                motivo = "contrato encerrado"
            elif fonte.get("excluir_atrasados") and _inteiro(raw.get("parcelas_atrasadas")) > 0:
                motivo = "parcelas em atraso"
            if motivo and clean["valor_centavos"]:
                valor_planilha = format_money(clean["valor_centavos"])
                row["avisos"].append(f"Valor do mês considerado R$ 0,00 ({motivo}); valor da planilha: {valor_planilha}.")
                clean["valor_centavos"] = 0

        # --- valor mensal do contrato
        if clean["valor_centavos"] is not None:
            previous = db_contract_values.get(rec.db_id, {}).get(month) if rec.db_id else None
            rec.months[month] = clean["valor_centavos"]
            rec.month_lines[month] = line
            if previous is None:
                rec.changed_months[month] = clean["valor_centavos"]
                if not rec.is_new:
                    changes.append({"campo": f"Valor de {month_label(month)}", "de": "—",
                                    "para": format_money(clean["valor_centavos"])})
            elif previous != clean["valor_centavos"]:
                rec.changed_months[month] = clean["valor_centavos"]
                changes.append({"campo": f"Valor de {month_label(month)}", "de": format_money(previous),
                                "para": format_money(clean["valor_centavos"])})
            entity.import_month_values.setdefault(month, {})[numero_l] = clean["valor_centavos"]

        for k in CONTRACT_CLIENT_FIELDS:
            if k in mapped and clean.get(k) is not None:
                entity.contract_client.setdefault(k, clean[k])

        changes += row_notes
        row["status"] = "novo" if rec.is_new else ("atualizar" if changes else "sem_alteracao")
        row["mudancas"] = changes
        entity.touched = True
        entity.first_line = entity.first_line or line
        entity.contracts_changed = entity.contracts_changed or bool(changes) or rec.is_new
        stats["contratos_relacionados"] += 1
        rows_out.append(row)

    # --- dados do cliente que vieram na planilha de contratos (só preenchem o que a de clientes não trouxe)
    for entity in {id(r.entity): r.entity for r in recs.values()}.values():
        for k, value in entity.contract_client.items():
            if k not in entity.sheet_fields and entity.values.get(k) != value:
                if entity.row is not None:
                    entity.row["mudancas"].append({"campo": k.capitalize(), "de": entity.values.get(k), "para": value})
                entity.values[k] = value
                entity.fields_changed = True

    # --- total do cliente no mês = soma dos valores dos seus contratos naquele mês
    totals: list[dict] = []
    for entity in {id(r.entity): r.entity for r in recs.values()}.values():
        for month, from_sheet in sorted(entity.import_month_values.items()):
            merged = {**db_month.get((entity.db_id, month), {}), **from_sheet} if entity.db_id else dict(from_sheet)
            total = sum(merged.values())
            existing = ctx.db_payments(entity).get(month)
            status = "novo" if existing is None else ("substituido" if existing != total else "igual")
            stats[{"novo": "pagamentos_novos", "substituido": "pagamentos_substituidos",
                   "igual": "pagamentos_iguais"}[status]] += 1
            if status != "igual":
                entity.pending[month] = total
                entity.payments_changed = True
            entity.months_seen.setdefault(month, entity.first_line)
            totals.append({"codigo": entity.values.get("codigo"), "cliente": entity.name,
                           "cnpj": format_cnpj(entity.values.get("cnpj")), "mes": month,
                           "contratos": len(merged), "valor_centavos": total,
                           "classificacao": classify(total, ctx.faixas), "anterior_centavos": existing, "status": status})

    # --- clientes da planilha de clientes que não tiveram nenhum contrato
    with_contracts = {id(r.entity) for r in recs.values()}
    for entity in ctx.sheet_entities():
        if id(entity) in with_contracts:
            continue
        stats["clientes_sem_contrato"] += 1
        incons.append({"planilha": "clientes", "linha": entity.first_line, "tipo": "cliente_sem_contrato",
                       "tipo_rotulo": TIPOS["cliente_sem_contrato"],
                       "identificacao": entity.values.get("codigo") or format_cnpj(entity.values.get("cnpj")) or "",
                       "nome": entity.name,
                       "motivo": "Nenhum contrato desta planilha corresponde a este cliente. "
                                 "O cadastro é importado normalmente.",
                       "conteudo": ""})

    return {"linhas": rows_out, "totais": totals, "incons": incons, "stats": stats, "recs": list(recs.values())}
