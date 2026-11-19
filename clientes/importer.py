"""Importação de planilhas (.xlsx e .csv): leitura, mapeamento de colunas, análise e gravação.

Fluxo: read_table -> suggest_mapping -> analyze (prévia, sem gravar) -> apply (grava).
`analyze` simula o resultado linha a linha, inclusive clientes que aparecem mais de uma vez
na mesma planilha (por exemplo, uma linha por cliente/mês).
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from types import SimpleNamespace

from .classification import classify
from .contracts import TIPOS, process as process_contracts
from .repository import (find_by_cnpj, find_by_codigo, find_contract, insert_client, insert_contract,
                         load_faixas, now, update_client, update_contract, upsert_contract_value, upsert_payment)
from .validation import (CLIENT_FIELDS, CONTRACT_LABELS, FIELD_LABELS, display_name, format_cnpj,
                         format_money, is_blank, key_cnpj, key_codigo, merge_note, month_label, normalize,
                         parse_month, strip_accents, to_text)

MAX_ROWS = 50_000
MAX_PREVIEW_ROWS = 1_000


class ImportFailure(Exception):
    """Erro com mensagem apropriada para exibir ao usuário."""


# --------------------------------------------------------------------------- leitura

SPREADSHEETML_NS = "urn:schemas-microsoft-com:office:spreadsheet"
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # formato binário antigo (.xls BIFF)


def read_table(data: bytes, ext: str, sheet: str | None = None, header_row: int | None = None) -> dict:
    """Lê o arquivo e devolve {sheets, sheet, header, rows=[(linha, [células])]}.

    `header_row` (1 = primeira linha da planilha) força a linha do cabeçalho; sem ele, o cabeçalho é
    a primeira linha não vazia (linhas vazias no início são ignoradas).
    """
    if ext == ".csv":
        return _read_csv(data, header_row)
    if ext == ".xls":
        return _read_xls(data, sheet, header_row)
    return _read_xlsx(data, sheet, header_row)


def _read_xls(data: bytes, sheet: str | None, header_row: int | None) -> dict:
    """Aceita o XML do Excel 2003 (SpreadsheetML), que alguns sistemas exportam com extensão .xls.

    Usa só a biblioteca padrão. O formato binário antigo (BIFF) não é suportado.
    """
    import xml.etree.ElementTree as ET

    from defusedxml import DefusedXmlException
    from defusedxml.ElementTree import fromstring as safe_fromstring

    if data.startswith(_OLE_SIGNATURE):
        raise ImportFailure("Este .xls é o formato binário antigo do Excel, que não é aceito. "
                            "Abra no Excel e salve como .xlsx ou CSV.")
    try:
        # defusedxml recusa DTD e entidades externas/expansivas (ataques de "billion laughs" e XXE).
        root = safe_fromstring(data)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ImportFailure("Não foi possível ler o arquivo .xls. Verifique se ele não está corrompido "
                            "ou salve como .xlsx ou CSV.") from exc
    if root.tag != f"{{{SPREADSHEETML_NS}}}Workbook":
        raise ImportFailure("O arquivo .xls não está em um formato de planilha reconhecido. "
                            "Salve como .xlsx ou CSV.")

    ns = f"{{{SPREADSHEETML_NS}}}"
    worksheets = root.findall(f"{ns}Worksheet")
    names = [w.get(f"{ns}Name") or f"Planilha {i + 1}" for i, w in enumerate(worksheets)]
    if not worksheets:
        raise ImportFailure("A planilha está vazia.")
    active = sheet if sheet in names else names[0]
    ws = worksheets[names.index(active)]
    table = ws.find(f"{ns}Table")

    def rows():
        row_no = 0
        for row in (table.findall(f"{ns}Row") if table is not None else []):
            idx = row.get(f"{ns}Index")
            row_no = int(idx) if idx else row_no + 1
            values: list = []
            col = 0
            for cell in row.findall(f"{ns}Cell"):
                cidx = cell.get(f"{ns}Index")
                col = int(cidx) if cidx else col + 1
                while len(values) < col - 1:
                    values.append(None)
                values.append(_spreadsheetml_value(cell, ns))
            yield row_no, values

    return _build(rows(), sheets=names, sheet=active, header_row=header_row)


def _spreadsheetml_value(cell, ns: str):
    node = cell.find(f"{ns}Data")
    if node is None or node.text is None:
        return None
    text = node.text
    kind = node.get(f"{ns}Type")
    if kind == "Number":
        number = float(text)
        return int(number) if number.is_integer() else number
    if kind == "DateTime":
        try:
            return datetime.fromisoformat(text[:19])
        except ValueError:
            return text
    if kind == "Boolean":
        return text.strip() in ("1", "true", "True")
    return text


def _read_csv(data: bytes, header_row: int | None = None) -> dict:
    text = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = max(";,\t|", key=lambda d: first.count(d)) if any(d in first for d in ";,\t|") else ";"
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)

    def lines():
        try:
            for cells in reader:
                yield reader.line_num, cells
        except csv.Error as exc:
            raise ImportFailure(f"Não foi possível ler o CSV: {exc}") from exc

    return _build(lines(), sheets=[], sheet=None, header_row=header_row)


def _read_xlsx(data: bytes, sheet: str | None, header_row: int | None = None) -> dict:
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError) as exc:
        raise ImportFailure("Não foi possível abrir o arquivo. Verifique se é uma planilha .xlsx válida.") from exc
    try:
        names = [ws.title for ws in wb.worksheets if ws.sheet_state == "visible"] or wb.sheetnames
        active = sheet if sheet in names else names[0]
        ws = wb[active]
        if hasattr(ws, "reset_dimensions"):
            ws.reset_dimensions()
        rows = ((i, list(cells)) for i, cells in enumerate(ws.iter_rows(values_only=True), start=1))
        return _build(rows, sheets=names, sheet=active, header_row=header_row)
    finally:
        wb.close()


def _build(raw_rows, sheets: list[str], sheet: str | None, header_row: int | None = None) -> dict:
    """Separa o cabeçalho das linhas de dados.

    Sem `header_row`, o cabeçalho é a primeira linha com pelo menos duas células preenchidas (títulos de
    uma célula acima da tabela são ignorados); se nenhuma linha tiver duas células, vale a primeira não vazia.
    Com `header_row`, o cabeçalho é exatamente essa linha da planilha.
    """
    materialized = [(line, list(cells)) for line, cells in raw_rows]
    filled = lambda cells: sum(0 if is_blank(c) else 1 for c in cells)

    if header_row is not None:
        pos = next((i for i, (line, _) in enumerate(materialized) if line == header_row), None)
        if pos is None:
            raise ImportFailure(f"A planilha não tem a linha {header_row}.")
        if filled(materialized[pos][1]) == 0:
            raise ImportFailure(f"A linha {header_row} está vazia. Escolha a linha que contém os títulos das colunas.")
    else:
        pos = next((i for i, (_, cells) in enumerate(materialized) if filled(cells) >= 2), None)
        if pos is None:
            pos = next((i for i, (_, cells) in enumerate(materialized) if filled(cells) >= 1), None)
        if pos is None:
            raise ImportFailure("A planilha está vazia.")

    line, head = materialized[pos]
    last = max(i for i, c in enumerate(head) if not is_blank(c))
    names, seen = [], {}
    for i, cell in enumerate(head[: last + 1]):
        name = to_text(cell) or f"Coluna {i + 1}"
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    header = names

    rows: list[tuple[int, list]] = []
    for row_line, cells in materialized[pos + 1:]:
        if all(is_blank(c) for c in cells):
            continue
        rows.append((row_line, cells))
        if len(rows) > MAX_ROWS:
            raise ImportFailure(f"A planilha tem mais de {MAX_ROWS:,} linhas. Divida o arquivo em partes menores.".replace(",", "."))
    if not rows:
        raise ImportFailure("A planilha tem apenas o cabeçalho, sem linhas de dados.")
    return {"sheets": sheets, "sheet": sheet, "header": header, "rows": rows}


# --------------------------------------------------------------------------- mapeamento sugerido

_ALIASES = {
    "codigo": ["codigo", "codigodocliente", "codcliente", "codigocliente", "cod", "idcliente", "id"],
    "razao_social": ["razaosocial", "razao", "nomeempresarial"],
    "nome_fantasia": ["nomefantasia", "fantasia"],
    "cnpj": ["cnpj", "cnpjcpf", "cpfcnpj", "documento", "documentocliente", "documentodocliente"],
    "telefone": ["telefone", "fone", "tel", "celular", "whatsapp", "telefonecontato"],
    "email": ["email", "emailcontato"],
    "cep": ["cep", "codigopostal"],
    "endereco": ["endereco", "logradouro", "rua"],
    "numero": ["numero", "num", "nro"],
    "complemento": ["complemento", "compl"],
    "bairro": ["bairro", "distrito"],
    "cidade": ["cidade", "municipio", "localidade"],
    "estado": ["estado", "uf"],
    "segmento": ["segmento", "ramo", "ramodeatividade", "atividade", "categoria"],
    "vendedor": ["vendedor", "vendedorresponsavel", "responsavel", "representante", "consultor"],
    "valor": ["valor", "valorpago", "valortotalpagonomes", "valortotalpago", "totalpago",
              "valortotal", "valordomes", "valorpagonomes", "faturamento", "totalnomes",
              "valoratual"],
    "mes": ["mes", "mesreferencia", "mesdereferencia", "referencia", "competencia", "periodo", "mesref"],
    # Singular "Observacao" fica com os contratos (é onde as planilhas reais a trazem).
    "observacoes": ["observacoes", "observacoesdocliente", "observacaogeral", "observacoesgerais", "obs"],
}
_FALLBACK_ALIASES = {"razao_social": ["cliente", "nome", "nomecliente", "empresa"]}


_CONTRACT_ALIASES = {
    "chave_cliente": ["documentocliente", "documentodocliente", "doccliente", "documento", "cnpjcliente",
                      "cnpjdocliente", "cnpj", "cnpjcpf", "cpfcnpj", "codigocliente", "codigodocliente",
                      "codcliente", "idcliente"],
    # "Nº" vira "n" após a normalização (o símbolo º é removido)
    "numero_contrato": ["numerodocontrato", "numerocontrato", "nrocontrato", "nrodocontrato", "ncontrato",
                        "contrato", "codigocontrato", "codcontrato", "idcontrato", "n"],  # "Numero" (endereço) NÃO é contrato
    "produto": ["produto", "plano", "servico", "nomedoproduto", "produtoplano", "descricao"],
    "situacao": ["situacao", "situacaocontrato", "statuscontrato", "status"],
    "data_inicio": ["datainicio", "datadeinicio", "inicio", "datadeadesao", "dataadesao", "adesao",
                    "datacontrato", "iniciovigencia"],
    "data_fim": ["datafim", "datadefim", "datatermino", "datadetermino", "termino", "fimvigencia"],
    # Atenção: "valor parcela futuro" NUNCA é sugerido como valor mensal pago.
    # "Valor Atual" é a sugestão inicial (relação com "Quantidade Parcelas" e "Valor Parcela Futuro"
    # confirmada nos arquivos reais). O usuário pode trocar a coluna na associação.
    "valor_mensal": ["valoratual", "valormensal", "mensalidade", "valorpago", "valorpagonomes",
                     "valortotalpagonomes", "valortotalpago", "totalpago", "valordomes", "valormes",
                     "valorpagomes"],
    "valor_parcela_futuro": ["valorparcelafuturo", "parcelafuturo", "valorparcelasfuturas", "parcelasfuturas",
                             "valordaparcelafutura", "valorparcelafutura"],
    "mes": _ALIASES["mes"],
    "segmento": _ALIASES["segmento"],
    "vendedor": _ALIASES["vendedor"],
    "observacoes": ["observacao", "observacoes", "observacaodocontrato", "obs"],
}


def slug(text: str) -> str:
    """Forma comparável de um cabeçalho: sem acentos, maiúsculas, espaços ou pontuação."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(text).lower())


_slug = slug


def labels_for(tipo: str) -> dict:
    return CONTRACT_LABELS if tipo == "contratos" else FIELD_LABELS


def suggest_mapping(header: list[str], tipo: str = "clientes") -> dict:
    slugs = [slug(h) for h in header]
    mapping: dict = {field_key: None for field_key in labels_for(tipo)}
    used: set[int] = set()
    groups = (_CONTRACT_ALIASES,) if tipo == "contratos" else (_ALIASES, _FALLBACK_ALIASES)
    for aliases in groups:
        for key, names in aliases.items():
            if mapping[key] is not None:
                continue
            for i, s in enumerate(slugs):
                if i not in used and s in names:
                    mapping[key] = i
                    used.add(i)
                    break
    return mapping


def fuzzy_suggest(header: list[str], tipo: str, key: str, old_name: str, used: set[int]) -> int | None:
    """Sugere uma coluna para um campo cujo cabeçalho salvo sumiu (cabeçalho renomeado).

    Aceita nomes parecidos com o antigo ou que contenham um apelido conhecido do campo. Nunca sugere
    uma coluna de "parcela futura" para o valor mensal pago.
    """
    old = slug(old_name)
    aliases = [a for a in (_CONTRACT_ALIASES if tipo == "contratos" else _ALIASES).get(key, []) if len(a) >= 6]
    for i, name in enumerate(header):
        cand = slug(name)
        if i in used or len(cand) < 4:
            continue
        if key == "valor_mensal" and "parcelafutur" in cand.replace("parcelasfutur", "parcelafutur"):
            continue
        if (len(old) >= 4 and (old in cand or cand in old)) or any(a in cand for a in aliases):
            return i
    return None


def suggest_chave(header: list[str], mapping: dict) -> str:
    """CNPJ é o padrão; só sugere 'código' se a coluna de relacionamento parecer um código."""
    idx = mapping.get("chave_cliente")
    if idx is None:
        return "cnpj"
    s = slug(header[idx])
    return "codigo" if ("cod" in s or s.startswith("id")) and "cnpj" not in s and "documento" not in s else "cnpj"


def describe_table(table: dict, tipo: str = "clientes") -> dict:
    columns = []
    for i, name in enumerate(table["header"]):
        samples = []
        for _, cells in table["rows"][:200]:
            text = to_text(cells[i]) if i < len(cells) else None
            if text and text not in samples:
                samples.append(text[:40])
            if len(samples) == 3:
                break
        columns.append({"indice": i, "nome": name, "exemplos": samples})
    suggested = suggest_mapping(table["header"], tipo)
    return {
        "planilhas": table["sheets"],
        "planilha": table["sheet"],
        "colunas": columns,
        "total_linhas": len(table["rows"]),
        "mapeamento_sugerido": suggested,
        "chave_sugerida": suggest_chave(table["header"], suggested) if tipo == "contratos" else None,
    }


def _check_indexes(mapping: dict, labels: dict, ncols: int) -> None:
    for key, idx in mapping.items():
        if key not in labels:
            raise ImportFailure(f"Campo desconhecido: {key}.")
        if idx is not None and not (isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < ncols):
            raise ImportFailure("Mapeamento de colunas inválido.")


def validate_mapping(mapping: dict, default_month: str | None, ncols: int) -> None:
    _check_indexes(mapping, FIELD_LABELS, ncols)
    if mapping.get("codigo") is None and mapping.get("cnpj") is None:
        raise ImportFailure("Associe pelo menos uma coluna de Código do cliente ou de CNPJ, "
                            "pois é por elas que o sistema identifica clientes já cadastrados.")
    if mapping.get("valor") is not None and mapping.get("mes") is None and not default_month:
        raise ImportFailure("Informe o mês de referência dos valores ou associe a coluna de mês da planilha.")


def validate_contract_mapping(mapping: dict, default_month: str | None, ncols: int) -> None:
    _check_indexes(mapping, CONTRACT_LABELS, ncols)
    if mapping.get("numero_contrato") is None:
        raise ImportFailure("Planilha de contratos: associe a coluna do número do contrato. "
                            "É por ele que o sistema evita duplicar contratos nas importações semanais.")
    if mapping.get("chave_cliente") is None:
        raise ImportFailure("Planilha de contratos: associe a coluna que identifica o cliente "
                            "(por exemplo, “Documento Cliente”).")
    if mapping.get("valor_mensal") is not None and mapping.get("mes") is None and not default_month:
        raise ImportFailure("Informe o mês de referência dos valores ou associe a coluna de mês da planilha de contratos.")


# --------------------------------------------------------------------------- análise

@dataclass
class _Entity:
    db_id: int | None
    values: dict
    is_new: bool = False
    original: dict = field(default_factory=dict)
    touched: bool = False
    fields_changed: bool = False
    payments_changed: bool = False
    contracts_changed: bool = False
    from_sheet: bool = False                 # veio da planilha de clientes desta importação
    first_line: int | None = None
    row: dict | None = None                  # linha exibida na prévia (planilha de clientes)
    months_seen: dict = field(default_factory=dict)   # mês -> linha
    pending: dict = field(default_factory=dict)       # mês -> centavos a gravar
    sheet_fields: set = field(default_factory=set)    # campos preenchidos pela planilha de clientes
    contract_recs: list = field(default_factory=list)
    contract_client: dict = field(default_factory=dict)       # segmento/vendedor vindos dos contratos
    import_month_values: dict = field(default_factory=dict)   # mês -> {contrato: centavos}
    _db_payments: dict | None = None

    @property
    def name(self) -> str:
        return display_name(self.values) or self.values.get("codigo") or ""


_money = format_money
_month_label = month_label


def _field_error(key: str, message: str) -> str:
    """Prefixa o nome do campo, exceto quando a própria mensagem já o menciona."""
    label = FIELD_LABELS[key]
    return message if message.lower().startswith(label.split()[0].lower().rstrip("-")) else f"{label}: {message}"


def _content(header, cells) -> str:
    parts = [f"{h}: {t}" for h, c in zip(header, cells) if (t := to_text(c))]
    return "; ".join(parts)[:600]


def analyze(db, table: dict | None, mapping: dict | None, *, default_month: str | None = None,
            replace_empty: bool = False, contracts: dict | None = None, chave: str = "cnpj",
            mixed: bool = False) -> dict:
    """Simula a importação sem gravar nada. Devolve resumo, linhas e o plano de gravação.

    `table`/`mapping` são a planilha de clientes (opcional se houver `contracts`).
    `contracts` = {"table": ..., "mapping": ...} é a planilha de contratos (opcional).
    `chave` ('cnpj' ou 'codigo') é o campo que relaciona os contratos aos clientes.
    `mixed` = True quando a mesma planilha foi usada como clientes e como contratos (planilha única).
    """
    if table is None and contracts is None:
        raise ImportFailure("Selecione pelo menos uma planilha.")
    if chave not in ("cnpj", "codigo"):
        raise ImportFailure("Escolha o campo de relacionamento entre as planilhas (CNPJ ou código).")
    if default_month:
        default_month, err = parse_month(default_month)
        if err:
            raise ImportFailure(err)

    mapping = dict(mapping or {})
    faixas = load_faixas(db)          # faixas da Curva ABC vigentes (editáveis; nunca fixas no código)
    avisos: list[str] = []
    if mixed and contracts is not None and contracts["mapping"].get("numero_contrato") is None:
        # Planilha única sem número de contrato: não há identificador seguro para os contratos.
        # Importam-se só os clientes (cada linha é um cliente) e nenhum contrato é criado.
        contracts = None
        avisos.append("Planilha única sem coluna de número do contrato: os clientes foram importados, mas os "
                      "contratos não. Para importar contratos, associe a coluna de número do contrato (por exemplo, “Nº”). "
                      "O valor mensal só é usado se a coluna correspondente estiver associada.")
    if table is not None:
        if contracts is not None:
            # com planilha de contratos, o valor mensal vem dela (soma dos contratos do cliente)
            mapping["valor"] = None
            mapping["mes"] = None
        validate_mapping(mapping, default_month, len(table["header"]))
        if contracts is not None and mapping.get(chave) is None:
            raise ImportFailure("Para relacionar as planilhas por " + ("CNPJ" if chave == "cnpj" else "código")
                                + ", associe essa coluna também na planilha de clientes.")
    if contracts is not None:
        validate_contract_mapping(contracts["mapping"], default_month, len(contracts["table"]["header"]))

    mapped = {k: v for k, v in mapping.items() if v is not None}
    merge_fields = [f for f in CLIENT_FIELDS if f in mapped and f != "codigo"]

    db_by_code = {r["codigo"].lower(): dict(r) for r in db.execute("SELECT * FROM clientes")}
    db_by_cnpj = {r["cnpj"]: r for r in db_by_code.values() if r["cnpj"]}
    ent_by_code: dict[str, _Entity] = {}
    ent_by_cnpj: dict[str, _Entity] = {}
    entities: list[_Entity] = []
    rejected_keys: dict[tuple, int] = {}

    def register(entity: _Entity) -> None:
        entities.append(entity)
        if entity.values.get("codigo"):
            ent_by_code[entity.values["codigo"].lower()] = entity
        if entity.values.get("cnpj"):
            ent_by_cnpj[entity.values["cnpj"]] = entity

    def from_db(row: dict) -> _Entity:
        values = {k: row[k] for k in CLIENT_FIELDS}
        entity = _Entity(db_id=row["id"], values=dict(values), original=dict(values))
        register(entity)
        return entity

    def find_code(key: str | None):
        if not key:
            return None
        return ent_by_code.get(key) or (from_db(db_by_code[key]) if key in db_by_code else None)

    def find_cnpj(cnpj: str | None):
        if not cnpj:
            return None
        return ent_by_cnpj.get(cnpj) or (from_db(db_by_cnpj[cnpj]) if cnpj in db_by_cnpj else None)

    def db_payments(entity: _Entity) -> dict:
        if entity._db_payments is None:
            rows = db.execute("SELECT mes_referencia, valor_centavos FROM pagamentos_mensais "
                              "WHERE cliente_id = %s", (entity.db_id,)).fetchall() if entity.db_id else []
            entity._db_payments = {r[0]: r[1] for r in rows}
        return entity._db_payments

    out_rows: list[dict] = []
    incons: list[dict] = []
    stats = {"linhas_lidas": 0, "linhas_rejeitadas": 0,
             "pagamentos_novos": 0, "pagamentos_substituidos": 0, "pagamentos_iguais": 0}

    for line, cells in (table["rows"] if table is not None else []):
        raw = {key: (cells[idx] if idx < len(cells) else None) for key, idx in mapped.items()}
        if all(is_blank(v) for v in raw.values()):
            continue
        stats["linhas_lidas"] += 1
        clean, errors = normalize(raw)
        month = clean["mes"] or default_month
        row = {"linha": line, "status": None, "codigo": clean["codigo"], "nome": display_name(clean),
               "cnpj": format_cnpj(clean["cnpj"]), "mes": month if clean["valor_centavos"] is not None else None,
               "valor_centavos": clean["valor_centavos"],
               "classificacao": classify(clean["valor_centavos"], faixas), "mudancas": [], "avisos": [], "erros": []}

        def reject(kind: str, *messages: str) -> None:
            row["status"] = "rejeitado"
            row["erros"] = list(messages)
            stats["linhas_rejeitadas"] += 1
            out_rows.append(row)
            for ref in (("cnpj", key_cnpj(raw.get("cnpj"))), ("codigo", key_codigo(raw.get("codigo")))):
                if ref[1]:
                    rejected_keys.setdefault(ref, line)
            incons.append({"planilha": "clientes", "linha": line, "tipo": kind, "tipo_rotulo": TIPOS[kind],
                           "identificacao": clean["codigo"] or row["cnpj"] or "", "nome": row["nome"],
                           "motivo": " | ".join(messages), "conteudo": _content(table["header"], cells)})

        if errors:
            reject("invalido", *(_field_error(k, msg) for k, msg in errors.items()))
            continue
        if clean["valor_centavos"] is not None and not month:
            reject("invalido", "Mês de referência não informado para o valor desta linha.")
            continue
        if not clean["codigo"] and not clean["cnpj"]:
            reject("invalido", "Informe o código ou o CNPJ do cliente.")
            continue

        by_code = find_code(clean["codigo"].lower() if clean["codigo"] else None)
        by_cnpj = find_cnpj(clean["cnpj"])
        if by_code and by_cnpj and by_code is not by_cnpj:
            reject("conflito", f"O código {clean['codigo']} pertence a “{by_code.name}”, mas o CNPJ pertence a "
                               f"“{by_cnpj.name}”. Corrija a planilha ou o cadastro.")
            continue
        entity = by_code or by_cnpj

        if entity is None:
            if not clean["codigo"] and clean["cnpj"]:
                # Cliente novo sem código: o próprio CNPJ (só dígitos, único) vira o código.
                clean["codigo"] = clean["cnpj"]
                row["codigo"] = clean["cnpj"]
                row["avisos"].append("Código não informado na planilha: o CNPJ foi usado como código.")
            if not clean["codigo"]:
                reject("invalido", "Informe o código para cadastrar um cliente novo (este CNPJ ainda não existe no sistema).")
                continue
            if not (clean["razao_social"] or clean["nome_fantasia"]):
                reject("invalido", "Informe a razão social ou o nome fantasia para cadastrar um cliente novo.")
                continue
        else:
            if by_code and clean["cnpj"] and entity.values["cnpj"] and entity.values["cnpj"] != clean["cnpj"]:
                reject("conflito", f"O código {clean['codigo']} já está cadastrado com outro CNPJ "
                                   f"({format_cnpj(entity.values['cnpj'])}).")
                continue
            if entity.touched and entity.from_sheet:
                if clean["valor_centavos"] is None:
                    if mixed and all(clean[k] in (None, entity.values[k]) for k in merge_fields):
                        # planilha única: a mesma linha de cliente se repete para cada contrato dele
                        row["status"] = "sem_alteracao"
                        row["avisos"].append("Cliente repetido na planilha (outro contrato do mesmo cliente); "
                                             "o cadastro não foi duplicado.")
                        out_rows.append(row)
                        continue
                    reject("duplicado", f"Cliente repetido na planilha (já aparece na linha {entity.first_line}).")
                    continue
                if month in entity.months_seen:
                    reject("duplicado", f"Este cliente já aparece na linha {entity.months_seen[month]} "
                                        f"para o mês {_month_label(month)}.")
                    continue
            if by_cnpj and not by_code and clean["codigo"] \
                    and clean["codigo"].lower() != entity.values["codigo"].lower():
                row["avisos"].append(f"Código da planilha ({clean['codigo']}) é diferente do cadastrado "
                                     f"({entity.values['codigo']}); o cadastro foi mantido.")

        # --- campos do cliente
        if entity is None:
            merged = {k: clean[k] for k in CLIENT_FIELDS}
            changes: list[dict] = []
        else:
            merged = dict(entity.values)
            for key in merge_fields:
                new = clean[key]
                if key == "observacoes":
                    merged[key], note_warning = merge_note(entity.values[key], new)
                    if note_warning:
                        row["avisos"].append(note_warning)
                    continue
                if new is not None or replace_empty:
                    merged[key] = new
            if not (merged["razao_social"] or merged["nome_fantasia"]):
                reject("invalido", "A substituição por campos vazios deixaria o cliente sem nome.")
                continue
            changes = [{"campo": FIELD_LABELS[k], "de": entity.values[k], "para": merged[k]}
                       for k in merge_fields if merged[k] != entity.values[k]]

        # --- valor do mês
        pay_status = None
        if clean["valor_centavos"] is not None:
            existing = db_payments(entity).get(month) if entity is not None else None
            if existing is None:
                pay_status = "novo"
            elif existing != clean["valor_centavos"]:
                pay_status = "substituido"
                changes.append({"campo": f"Valor de {_month_label(month)}",
                                "de": _money(existing), "para": _money(clean["valor_centavos"])})
            else:
                pay_status = "igual"

        # --- aceita a linha
        if entity is None:
            entity = _Entity(db_id=None, values=merged, is_new=True)
            register(entity)
            row["status"] = "novo"
        else:
            entity.fields_changed = entity.fields_changed or bool(
                [k for k in merge_fields if merged[k] != entity.values[k]])
            entity.values = merged
            if merged.get("cnpj"):
                ent_by_cnpj[merged["cnpj"]] = entity
            changed = bool(changes) or pay_status == "novo"
            row["status"] = "novo" if entity.is_new else ("atualizar" if changed else "sem_alteracao")
        entity.touched = True
        entity.from_sheet = True
        entity.row = row
        entity.sheet_fields |= {k for k in merge_fields if clean[k] is not None}
        entity.first_line = entity.first_line or line
        if pay_status:
            entity.months_seen[month] = line
            if pay_status != "igual":
                entity.pending[month] = clean["valor_centavos"]
                entity.payments_changed = True
            stats[{"novo": "pagamentos_novos", "substituido": "pagamentos_substituidos",
                   "igual": "pagamentos_iguais"}[pay_status]] += 1
            if pay_status == "novo" and not entity.is_new:
                changes.append({"campo": f"Valor de {_month_label(month)}", "de": "—",
                                "para": _money(clean["valor_centavos"])})
        row["mudancas"] = changes
        out_rows.append(row)

    # --- planilha de contratos
    contract_out = None
    if contracts is not None:
        ctx = SimpleNamespace(
            table=contracts["table"], mapping=contracts["mapping"], chave=chave, default_month=default_month,
            faixas=faixas,
            replace_empty=replace_empty, db=db, db_payments=db_payments,
            find_entity=lambda kind, key: (find_cnpj(key) if kind == "cnpj" else find_code(key)) if key else None,
            rejected_line=lambda kind, key: rejected_keys.get((kind, key)),
            sheet_entities=lambda: [e for e in entities if e.from_sheet and e.touched])
        contract_out = process_contracts(ctx)
        for k, v in contract_out["stats"].items():
            stats[k] = stats.get(k, 0) + v
        incons.extend(contract_out["incons"])
        stats["linhas_rejeitadas"] += contract_out["stats"]["contratos_rejeitados"]
        ctable, cmap = contracts["table"], contracts["mapping"]
        vm = cmap.get("valor_mensal")
        if vm is None:
            avisos.append("Nenhuma coluna foi associada ao valor mensal pago: os contratos serão importados, "
                          "mas os valores do mês e a classificação dos clientes não serão atualizados.")
        elif "parcelafutur" in slug(ctable["header"][vm]):
            avisos.append(f"Atenção: a coluna escolhida como valor mensal (“{ctable['header'][vm]}”) parece ser a de "
                          "parcela futura. Confira se é realmente o valor pago no mês, pois ele define a classificação.")
        if cmap.get("valor_mensal") is not None and cmap.get("valor_mensal") == cmap.get("valor_parcela_futuro"):
            avisos.append("A mesma coluna foi associada ao valor mensal e à parcela futura.")

    for e in entities:   # linha do cliente muda de situação se só os contratos/valores dele mudaram
        r = e.row
        if r and r["status"] == "sem_alteracao" and (e.fields_changed or e.payments_changed or e.contracts_changed):
            r["status"] = "atualizar"
            dirty = sum(1 for rec in e.contract_recs if rec.dirty)
            if dirty:
                r["mudancas"].append({"campo": "Contratos", "de": "—", "para": f"{dirty} novo(s) ou atualizado(s)"})

    touched = [e for e in entities if e.touched]
    created = [e for e in touched if e.is_new]
    existing = [e for e in touched if not e.is_new]
    updated = [e for e in existing if e.fields_changed or e.payments_changed or e.contracts_changed]
    recs = contract_out["recs"] if contract_out else []
    contract_rows = contract_out["linhas"] if contract_out else []
    count = lambda kind: sum(1 for i in incons if i["tipo"] == kind)
    summary = {
        **stats,
        "tem_contratos": contracts is not None,
        "clientes_lidos": stats["linhas_lidas"],
        "clientes_novos": len(created),
        "clientes_atualizados": len(updated),
        "clientes_sem_alteracao": len(existing) - len(updated),
        "contratos_novos": sum(1 for r in recs if r.is_new),
        "contratos_atualizados": sum(1 for r in recs if not r.is_new and r.dirty),
        "contratos_sem_alteracao": sum(1 for r in recs if not r.dirty),
        "duplicados": count("duplicado"),
        "invalidos": count("invalido") + count("conflito"),
        "linhas_por_status": {s: sum(1 for r in out_rows if r["status"] == s)
                              for s in ("novo", "atualizar", "sem_alteracao", "rejeitado")},
        "contratos_por_status": {s: sum(1 for r in contract_rows if r["status"] == s)
                                 for s in ("novo", "atualizar", "sem_alteracao", "rejeitado")},
        "meses": sorted({m for e in touched for m in e.months_seen}),
    }
    for k in ("contratos_lidos", "contratos_relacionados", "contratos_sem_cliente", "contratos_rejeitados",
              "clientes_sem_contrato"):
        summary.setdefault(k, 0)

    def limited(rows: list[dict]) -> tuple[list[dict], bool]:
        shown, others = [], 0
        for r in rows:
            if r["status"] == "rejeitado" or others < MAX_PREVIEW_ROWS:
                shown.append(r)
                others += r["status"] != "rejeitado"
        return shown, len(shown) < len(rows)

    shown, cut_clients = limited(out_rows)
    shown_contracts, cut_contracts = limited(contract_rows)
    totals = contract_out["totais"] if contract_out else []
    return {"resumo": summary, "linhas": shown, "contratos_linhas": shown_contracts,
            "totais": totals[:MAX_PREVIEW_ROWS], "inconsistencias": incons[:MAX_PREVIEW_ROWS], "avisos": avisos,
            "truncado": cut_clients or cut_contracts or len(totals) > MAX_PREVIEW_ROWS or len(incons) > MAX_PREVIEW_ROWS,
            "_entidades": touched, "_inconsistencias": incons}


# --------------------------------------------------------------------------- gravação

def apply(db, analysis: dict, filename: str, filename_contracts: str | None = None) -> dict:
    """Grava o resultado de `analyze` em uma única transação (tudo ou nada)."""
    summary = analysis["resumo"]
    origin = "contratos" if summary.get("tem_contratos") else "importacao"
    payments_written = contracts_written = 0
    faixas = load_faixas(db)
    changed_msg = "Os dados mudaram desde a prévia (registro já cadastrado por outra pessoa). Refaça a importação."
    try:
        for entity in analysis["_entidades"]:
            if entity.is_new:
                if find_by_codigo(db, entity.values["codigo"]) or find_by_cnpj(db, entity.values["cnpj"]):
                    raise ImportFailure(changed_msg)
                entity.db_id = insert_client(db, entity.values)
            elif entity.fields_changed:
                update_client(db, entity.db_id, entity.values)
            for rec in entity.contract_recs:
                if rec.is_new:
                    if find_contract(db, rec.numero):
                        raise ImportFailure(changed_msg)
                    rec.db_id = insert_contract(db, entity.db_id, rec.numero, rec.values)
                elif rec.fields_changed:
                    update_contract(db, rec.db_id, rec.values)
                for month, cents in rec.changed_months.items():
                    upsert_contract_value(db, rec.db_id, month, cents)
                contracts_written += rec.dirty
            for month, cents in entity.pending.items():
                upsert_payment(db, entity.db_id, month, cents, origin, faixas)
                payments_written += 1
        db.execute(
            "INSERT INTO importacoes (arquivo, arquivo_contratos, realizada_em, linhas_lidas, clientes_criados, "
            "clientes_atualizados, clientes_sem_alteracao, linhas_rejeitadas, pagamentos_gravados, "
            "contratos_gravados, contratos_rejeitados) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (filename or filename_contracts or "planilha", filename_contracts if filename else None, now(),
             summary["linhas_lidas"] + summary["contratos_lidos"], summary["clientes_novos"],
             summary["clientes_atualizados"], summary["clientes_sem_alteracao"],
             summary["linhas_rejeitadas"], payments_written, contracts_written, summary["contratos_rejeitados"]))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {**summary, "pagamentos_gravados": payments_written, "contratos_gravados": contracts_written}


def inconsistencies_csv(analysis: dict) -> str:
    """Relatório de inconsistências (CSV com ';', compatível com Excel em português)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Planilha", "Linha", "Tipo", "Identificação", "Cliente", "Motivo", "Conteúdo original da linha"])
    for i in analysis["_inconsistencias"]:
        writer.writerow(["Clientes" if i["planilha"] == "clientes" else "Contratos", i["linha"] or "",
                         i["tipo_rotulo"], i["identificacao"], i["nome"], i["motivo"], i["conteudo"]])
    return buffer.getvalue()


_TEMPLATE_CONTRATOS = ["Documento Cliente", "Número do contrato", "Produto", "Situação", "Data de início",
                       "Data de término", "Valor mensal", "Valor parcela futuro", "Mês de referência",
                       "Observações"]


def template_csv(tipo: str = "clientes") -> str:
    """Modelo de planilha: apenas os títulos das colunas, sem dados de exemplo."""
    buffer = io.StringIO()
    headers = _TEMPLATE_CONTRATOS if tipo == "contratos" else [FIELD_LABELS[k] for k in FIELD_LABELS]
    csv.writer(buffer, delimiter=";").writerow(headers)
    return buffer.getvalue()
