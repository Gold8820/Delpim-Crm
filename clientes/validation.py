"""Normalização e validação dos dados de clientes.

Usado tanto pelo cadastro manual quanto pela importação de planilhas, para que as
duas entradas sigam exatamente as mesmas regras.
"""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CLIENT_FIELDS = (
    "codigo", "razao_social", "nome_fantasia", "cnpj", "telefone", "email",
    "cep", "endereco", "numero", "complemento", "bairro", "cidade", "estado",
    "segmento", "vendedor", "observacoes",
)
IMPORT_FIELDS = CLIENT_FIELDS + ("valor", "mes")

FIELD_LABELS = {
    "codigo": "Código do cliente",
    "razao_social": "Razão social",
    "nome_fantasia": "Nome fantasia",
    "cnpj": "CNPJ / CPF (documento)",
    "telefone": "Telefone",
    "email": "E-mail",
    "cep": "CEP",
    "endereco": "Endereço",
    "numero": "Número",
    "complemento": "Complemento",
    "bairro": "Bairro",
    "cidade": "Cidade",
    "estado": "Estado",
    "segmento": "Segmento",
    "vendedor": "Vendedor responsável",
    "observacoes": "Observações",
    "valor": "Valor total pago no mês",
    "mes": "Mês de referência",
}

# Demais colunas reconhecidas nas planilhas reais. Ficam em "dados adicionais" (coluna dados_extras), sem
# regra de negócio própria: são guardadas, aparecem na ficha e nunca são apagadas por célula vazia.
CLIENT_EXTRA_LABELS = {
    "pessoa": "Pessoa (física / jurídica)",
    "endereco_padrao": "Endereço padrão",
    "referencia": "Referência (endereço)",
    "codigo_ibge": "Código IBGE da cidade",
    "contato_padrao": "Contato padrão",
    "contato_cobranca": "Contato de cobrança",
    "contato": "Contato",
    "inscricao_estadual": "Inscrição estadual",
    "estado_inscricao_estadual": "Estado da inscrição estadual",
    "data_nascimento": "Data de nascimento",
    "optante_simples": "Optante pelo Simples Nacional",
    "dias_antecedencia_faturamento": "Dias de antecedência do faturamento",
    "dia_faturamento": "Dia do faturamento",
    "configuracao_faturamento": "Configuração do faturamento",
    "inscricao_municipal": "Inscrição municipal",
}
FIELD_LABELS.update(CLIENT_EXTRA_LABELS)
EXTRA_MAX_LEN = 500

MAX_LEN = {"codigo": 40, "numero": 20, "complemento": 100, "segmento": 100, "vendedor": 100,
           "cidade": 100, "bairro": 100, "telefone": 40}
DEFAULT_MAX_LEN = 200
MAX_NOTE = 4000          # observações (cliente e contrato): texto longo, com quebras de linha

UFS = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia",
    "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás",
    "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais",
    "PA": "Pará", "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco", "PI": "Piauí",
    "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul",
    "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo",
    "SE": "Sergipe", "TO": "Tocantins",
}


# --------------------------------------------------------------------------- texto

def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def sort_key(text: str | None) -> str:
    """Chave de ordenação sem acentos e sem diferença de maiúsculas/minúsculas."""
    return strip_accents(text or "").lower()


def norm_search(text: str | None) -> str:
    """Texto de busca: sem acentos, minúsculo e sem pontuação de CNPJ (. / -)."""
    return re.sub(r"[./\-]", "", sort_key(text)).strip()


def is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and not value.strip()


def to_text(value) -> str | None:
    """Converte qualquer valor de célula em texto limpo (ou None se vazio)."""
    if is_blank(value):
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return " ".join(str(value).split()) or None


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def to_note(value) -> str | None:
    """Texto livre (observações): preserva acentos, símbolos e quebras de linha.

    Normaliza só as quebras (\r\n -> \n), remove caracteres de controle e espaços no fim das linhas.
    """
    if is_blank(value):
        return None
    text = value if isinstance(value, str) else to_text(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return text or None


def to_note_checked(value) -> tuple[str | None, str | None]:
    note = to_note(value)
    if note and len(note) > MAX_NOTE:
        return None, f"Máximo de {MAX_NOTE:,} caracteres.".replace(",", ".")
    return note, None


def merge_note(current: str | None, incoming: str | None) -> tuple[str | None, str | None]:
    """Regra de importação para observações. Nunca apaga texto cadastrado.

    - Célula vazia na planilha: mantém o que já está cadastrado.
    - Cadastro sem observação: preenche com o texto da planilha.
    - Texto diferente do cadastrado: mantém o cadastrado e devolve um aviso.
    Retorna (valor_final, aviso).
    """
    if incoming is None or incoming == current:
        return current, None
    if current is None:
        return incoming, None
    return current, ("Observações: a planilha traz um texto diferente do cadastrado; "
                     "o texto cadastrado foi mantido. Edite manualmente se quiser trocá-lo.")


def _digits(value) -> str:
    return re.sub(r"\D", "", to_text(value) or "")


# --------------------------------------------------------------------------- CNPJ

def _cnpj_check_digit(base: str, weights: list[int]) -> int:
    # Funciona para CNPJ numérico e alfanumérico (valor do caractere = ASCII - 48).
    total = sum((ord(ch) - 48) * w for ch, w in zip(base, weights))
    rest = total % 11
    return 0 if rest < 2 else 11 - rest


def valid_cnpj(value: str) -> bool:
    if len(value) != 14 or not re.fullmatch(r"[0-9A-Z]{12}[0-9]{2}", value):
        return False
    if value == value[0] * 14:
        return False
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6] + w1
    d1 = _cnpj_check_digit(value[:12], w1)
    d2 = _cnpj_check_digit(value[:12] + str(d1), w2)
    return value[12:] == f"{d1}{d2}"


def valid_cpf(value: str) -> bool:
    if len(value) != 11 or not value.isdigit() or value == value[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(d) * w for d, w in zip(value[:size], range(size + 1, 1, -1)))
        digit = (total * 10) % 11 % 10
        if digit != int(value[size]):
            return False
    return True


def _documento(raw: str) -> tuple[str, str | None]:
    """Forma canônica do documento: CPF com 11 dígitos ou CNPJ com 14 caracteres.

    O Excel remove zeros à esquerda de números: com até 11 dígitos, vale o CPF se ele for válido (e o
    mesmo número não for um CNPJ válido); senão, completa-se até 14 (CNPJ). Retorna (documento, tipo).
    """
    s = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    if s.isdigit() and len(s) <= 11:
        cpf, cnpj = s.zfill(11), s.zfill(14)
        if valid_cpf(cpf) and (len(s) == 11 or not valid_cnpj(cnpj)):
            return cpf, "cpf"
        return cnpj, "cnpj"
    if s.isdigit() and len(s) < 14:
        return s.zfill(14), "cnpj"
    return s, "cnpj"


def parse_cnpj(value) -> tuple[str | None, str | None]:
    """Documento do cliente: CNPJ (14, inclusive o alfanumérico) ou CPF (11), sem pontuação."""
    raw = to_text(value)
    if raw is None:
        return None, None
    s, tipo = _documento(raw)
    if tipo == "cpf":
        return s, None
    if len(s) != 14:
        return None, "CNPJ deve ter 14 caracteres (ou CPF com 11 dígitos)."
    if not valid_cnpj(s):
        return None, "CNPJ inválido."
    return s, None


def format_cnpj(value: str | None) -> str | None:
    if value and len(value) == 11 and value.isdigit():
        return f"{value[:3]}.{value[3:6]}.{value[6:9]}-{value[9:]}"   # CPF
    if not value or len(value) != 14:
        return value
    return f"{value[:2]}.{value[2:5]}.{value[5:8]}/{value[8:12]}-{value[12:]}"


# --------------------------------------------------------------------------- demais campos

def parse_cep(value) -> tuple[str | None, str | None]:
    if is_blank(value):
        return None, None
    d = _digits(value)
    if 5 <= len(d) < 8:
        d = d.zfill(8)
    if len(d) != 8:
        return None, "CEP deve ter 8 dígitos."
    return d, None


def format_cep(value: str | None) -> str | None:
    if not value or len(value) != 8:
        return value
    return f"{value[:5]}-{value[5:]}"


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_email(value) -> tuple[str | None, str | None]:
    text = to_text(value)
    if text is None:
        return None, None
    text = text.lower()
    if len(text) > DEFAULT_MAX_LEN or not _EMAIL_RE.match(text):
        return None, "E-mail inválido."
    return text, None


def parse_phone(value) -> tuple[str | None, str | None]:
    text = to_text(value)
    if text is None:
        return None, None
    if not re.fullmatch(r"[0-9\s()+\-./]+", text) or not 8 <= len(_digits(text)) <= 15:
        return None, "Telefone inválido (use DDD + número)."
    return text, None


def parse_uf(value) -> tuple[str | None, str | None]:
    text = to_text(value)
    if text is None:
        return None, None
    upper = text.upper()
    if upper in UFS:
        return upper, None
    by_name = {sort_key(name): uf for uf, name in UFS.items()}
    uf = by_name.get(sort_key(text))
    if uf:
        return uf, None
    return None, "Estado inválido (use a sigla, por exemplo SP)."


# --------------------------------------------------------------------------- valor e mês

def parse_money(value) -> tuple[int | None, str | None]:
    """Converte valores como 1234,5 | 1.234,56 | R$ 1.234,56 | 1234.56 em centavos."""
    if is_blank(value):
        return None, None
    err = "Valor inválido (use, por exemplo, 1.234,56)."
    if isinstance(value, bool):
        return None, err
    try:
        if isinstance(value, (int, float, Decimal)):
            amount = Decimal(str(value))
        else:
            s = re.sub(r"(?i)r\$|\s", "", str(value))
            if s.startswith("-") or s.startswith("("):
                return None, "O valor não pode ser negativo."
            if not re.fullmatch(r"[0-9.,]+", s):
                return None, err
            if "." in s and "," in s:
                dec, thou = ("," , ".") if s.rfind(",") > s.rfind(".") else (".", ",")
                s = s.replace(thou, "").replace(dec, ".")
            elif "," in s:
                if s.count(",") > 1:
                    return None, err
                s = s.replace(",", ".")
            elif "." in s:
                if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
                    s = s.replace(".", "")
                elif s.count(".") > 1:
                    return None, err
            amount = Decimal(s)
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None, err
    if amount < 0:
        return None, "O valor não pode ser negativo."
    if amount > Decimal("1000000000"):
        return None, "Valor muito alto."
    return int(amount * 100), None


_MONTH_NAMES = {
    "jan": 1, "janeiro": 1, "january": 1, "fev": 2, "fevereiro": 2, "feb": 2, "february": 2,
    "mar": 3, "marco": 3, "march": 3, "abr": 4, "abril": 4, "apr": 4, "april": 4,
    "mai": 5, "maio": 5, "may": 5, "jun": 6, "junho": 6, "june": 6,
    "jul": 7, "julho": 7, "july": 7, "ago": 8, "agosto": 8, "aug": 8, "august": 8,
    "set": 9, "setembro": 9, "sep": 9, "september": 9, "out": 10, "outubro": 10, "oct": 10,
    "october": 10, "nov": 11, "novembro": 11, "november": 11, "dez": 12, "dezembro": 12,
    "dec": 12, "december": 12,
}


def _month_ok(year: int, month: int) -> str | None:
    if 1 <= month <= 12 and 1990 <= year <= 2100:
        return f"{year:04d}-{month:02d}"
    return None


def parse_month(value) -> tuple[str | None, str | None]:
    """Converte para 'AAAA-MM'. Aceita 11/2026, 2026-11, nov/2026, datas, etc."""
    if is_blank(value):
        return None, None
    err = "Mês de referência inválido (use MM/AAAA, por exemplo 11/2026)."
    if isinstance(value, (datetime, date)):
        return (_month_ok(value.year, value.month), None) if _month_ok(value.year, value.month) else (None, err)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool):
        return None, err
    s = sort_key(to_text(value) or "").strip()
    result = None
    if (m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})", s)):
        result = _month_ok(int(m[1]), int(m[2]))
    elif (m := re.fullmatch(r"(\d{1,2})[-/.](\d{4})", s)):
        result = _month_ok(int(m[2]), int(m[1]))
    elif (m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ t].*)?", s)):
        result = _month_ok(int(m[1]), int(m[2]))
    elif (m := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?:\s.*)?", s)):
        result = _month_ok(int(m[3]), int(m[2]))  # dd/mm/aaaa
    elif (m := re.fullmatch(r"(\d{4})(\d{2})", s)):
        result = _month_ok(int(m[1]), int(m[2]))
    elif (m := re.fullmatch(r"([a-z]+)[\s\-/.]*(?:de\s*)?(\d{2}|\d{4})", s)) and m[1] in _MONTH_NAMES:
        year = int(m[2])
        result = _month_ok(year + 2000 if year < 100 else year, _MONTH_NAMES[m[1]])
    return (result, None) if result else (None, err)


# --------------------------------------------------------------------------- registro completo

def normalize(raw: dict) -> tuple[dict, dict]:
    """Valida e normaliza um registro. Retorna (dados_limpos, erros_por_campo).

    `raw` usa as chaves de IMPORT_FIELDS. Campos ausentes ou vazios viram None.
    Os valores saem como: valor_centavos (int) e mes ('AAAA-MM').
    """
    clean: dict = {key: None for key in CLIENT_FIELDS}
    clean["valor_centavos"] = None
    clean["mes"] = None
    errors: dict[str, str] = {}

    for key in ("codigo", "razao_social", "nome_fantasia", "endereco", "numero",
                "complemento", "bairro", "cidade", "segmento", "vendedor"):
        text = to_text(raw.get(key))
        limit = MAX_LEN.get(key, DEFAULT_MAX_LEN)
        if text and len(text) > limit:
            errors[key] = f"Máximo de {limit} caracteres."
        else:
            clean[key] = text

    clean["observacoes"], note_error = to_note_checked(raw.get("observacoes"))
    if note_error:
        errors["observacoes"] = note_error

    parsers = {
        "cnpj": parse_cnpj, "cep": parse_cep, "email": parse_email,
        "telefone": parse_phone, "estado": parse_uf,
    }
    for key, parser in parsers.items():
        value, error = parser(raw.get(key))
        clean[key] = value
        if error:
            errors[key] = error

    clean["valor_centavos"], error = parse_money(raw.get("valor"))
    if error:
        errors["valor"] = error
    clean["mes"], error = parse_month(raw.get("mes"))
    if error:
        errors["mes"] = error

    return clean, errors


def display_name(values: dict) -> str:
    return values.get("nome_fantasia") or values.get("razao_social") or ""


def search_blob(values: dict) -> str:
    parts = [values.get("codigo"), values.get("razao_social"), values.get("nome_fantasia"), values.get("cnpj")]
    return norm_search(" ".join(p for p in parts if p))


# =========================================================================== contratos (planilha 2)

CONTRACT_FIELDS = ("chave_cliente", "numero_contrato", "produto", "situacao", "data_inicio",
                   "data_fim", "valor_mensal", "valor_parcela_futuro", "mes", "segmento", "vendedor",
                   "observacoes")
CONTRACT_LABELS = {
    "chave_cliente": "Documento / código do cliente (relacionamento)",
    "numero_contrato": "Número do contrato",
    "produto": "Produto / plano",
    "situacao": "Situação do contrato",
    "data_inicio": "Data de início",
    "data_fim": "Data de término",
    "valor_mensal": "Valor mensal pago (usado na classificação)",
    "valor_parcela_futuro": "Valor parcela futuro (não usado na classificação)",
    "mes": "Mês de referência",
    "segmento": "Segmento do cliente",
    "vendedor": "Vendedor responsável",
    "observacoes": "Observações do contrato",
}
CONTRACT_EXTRA_LABELS = {
    "cliente_nome": "Cliente (nome na planilha)",
    "empresa": "Empresa (contratada)",
    "documento_empresa": "Documento da empresa",
    "classificacao_cliente": "Classificação do cliente",
    "valor_original": "Valor original (contratual)",
    "vigencia": "Vigência",
    "encerrado": "Encerrado",
    "data_encerramento": "Data de encerramento",
    "vencimento": "Dia de vencimento",
    "quantidade_parcelas": "Quantidade de parcelas",
    "proximo_reajuste": "Próximo reajuste",
    "pagamento": "Forma de pagamento",
    "parcelas_quitadas": "Parcelas quitadas",
    "parcelas_atrasadas": "Parcelas em atraso",
    "valor_parcela_atrasado": "Valor das parcelas em atraso",
    "parcelas_futuras": "Quantidade de parcelas futuras",
    "periodo_reajuste": "Período de reajuste",
}
CONTRACT_LABELS.update(CONTRACT_EXTRA_LABELS)


def extras_from(raw: dict, keys) -> dict:
    """Dados adicionais preenchidos na linha (texto limpo, até 500 caracteres). Células vazias ficam de fora."""
    out = {}
    for key in keys:
        text = to_text(raw.get(key))
        if text:
            out[key] = text[:EXTRA_MAX_LEN]
    return out


def merge_extras(current: dict | None, incoming: dict) -> dict:
    """Valores novos substituem os antigos; célula vazia nunca apaga o que já estava guardado."""
    return {**(current or {}), **incoming}


def limpar_extras_editados(edicoes, atuais: dict | None) -> tuple[dict, dict]:
    """Edição manual dos dados adicionais (ficha do cliente). Aceita só os campos conhecidos da planilha de
    clientes ou os que já existem no cadastro. Retorna (alterações {chave: texto ou None para esvaziar}, erros)."""
    atuais = atuais or {}
    if not isinstance(edicoes, dict):
        return {}, {}
    if len(edicoes) > 60:
        return {}, {"dados_extras": "Dados adicionais inválidos."}
    mudancas, erros = {}, {}
    for chave, valor in edicoes.items():
        if not isinstance(chave, str) or (chave not in CLIENT_EXTRA_LABELS and chave not in atuais):
            continue
        if valor is not None and not isinstance(valor, (str, int, float)):
            erros[f"extra_{chave}"] = "Valor inválido."
            continue
        texto = re.sub(r"\s+", " ", str(valor if valor is not None else "")).strip()
        if len(texto) > EXTRA_MAX_LEN:
            erros[f"extra_{chave}"] = f"Use até {EXTRA_MAX_LEN} caracteres."
            continue
        mudancas[chave] = texto or None
    return mudancas, erros


def aplicar_extras_editados(atuais: dict | None, mudancas: dict) -> dict:
    """Novo conteúdo de dados_extras: altera só as chaves editadas (vazio = sem valor), mantém as demais."""
    novo = dict(atuais or {})
    for chave, valor in mudancas.items():
        if valor is None:
            novo.pop(chave, None)
        else:
            novo[chave] = valor
    return novo


def flag_sim(value) -> bool:
    """'Sim', 'S', 'Yes', 'True', '1' (com ou sem espaços/acentos) -> True."""
    text = strip_accents(to_text(value) or "").strip().lower()
    return text in ("sim", "s", "yes", "y", "true", "verdadeiro", "1", "x")

# Campos do contrato que são do cliente: só preenchem o cadastro se a planilha de clientes não os trouxe.
CONTRACT_CLIENT_FIELDS = ("segmento", "vendedor")
CONTRACT_TEXT_LIMITS = {"numero_contrato": 60, "produto": 200, "situacao": 100, "segmento": 100, "vendedor": 100}


def key_cnpj(value) -> str | None:
    """Chave de comparação de documentos: ignora pontos, barras, traços e espaços.

    Números do Excel perdem zeros à esquerda; por isso valores só com dígitos são completados até 14.
    Não valida dígitos verificadores: serve apenas para reconhecer que dois textos são o mesmo documento.
    """
    text = to_text(value)
    if not text:
        return None
    if not re.sub(r"[^0-9A-Za-z]", "", text):
        return None
    return _documento(text)[0]   # mesma forma canônica do cadastro (CPF 11 / CNPJ 14)


def key_codigo(value) -> str | None:
    text = to_text(value)
    return text.lower() if text else None


def parse_date(value) -> tuple[str | None, str | None]:
    """Converte para 'AAAA-MM-DD'. Aceita datas do Excel, 31/12/2026, 2026-12-31 e 31/12/26."""
    if is_blank(value):
        return None, None
    err = "Data inválida (use, por exemplo, 31/12/2026)."
    if isinstance(value, datetime):
        return value.date().isoformat(), None
    if isinstance(value, date):
        return value.isoformat(), None
    s = (to_text(value) or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?", s)
    parts = (int(m[1]), int(m[2]), int(m[3])) if m else None
    if not m and (m := re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2}|\d{4})(?:\s.*)?", s)):
        year = int(m[3])
        parts = (year + 2000 if year < 100 else year, int(m[2]), int(m[1]))
    if not parts:
        return None, err
    try:
        return date(*parts).isoformat(), None
    except ValueError:
        return None, err


def normalize_contract(raw: dict) -> tuple[dict, dict]:
    """Valida e normaliza uma linha da planilha de contratos. Retorna (dados, erros_por_campo)."""
    clean: dict = {"chave": to_text(raw.get("chave_cliente"))}
    errors: dict[str, str] = {}
    for key in ("numero_contrato", "produto", "situacao", "segmento", "vendedor"):
        text = to_text(raw.get(key))
        limit = CONTRACT_TEXT_LIMITS[key]
        if text and len(text) > limit:
            errors[key] = f"Máximo de {limit} caracteres."
        else:
            clean[key] = text
    for key in ("data_inicio", "data_fim"):
        clean[key], error = parse_date(raw.get(key))
        if error:
            errors[key] = error
    if clean.get("data_inicio") and clean.get("data_fim") and clean["data_fim"] < clean["data_inicio"]:
        errors["data_fim"] = "A data de término é anterior à data de início."
    clean["valor_centavos"], error = parse_money(raw.get("valor_mensal"))
    if error:
        errors["valor_mensal"] = error
    clean["parcela_centavos"], error = parse_money(raw.get("valor_parcela_futuro"))
    if error:
        errors["valor_parcela_futuro"] = error
    clean["mes"], error = parse_month(raw.get("mes"))
    if error:
        errors["mes"] = error
    clean["observacoes"], note_error = to_note_checked(raw.get("observacoes"))
    if note_error:
        errors["observacoes"] = note_error
    return clean, errors


def format_money(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"R$ {cents // 100:,}".replace(",", ".") + f",{cents % 100:02d}"


def month_label(month: str) -> str:
    return f"{month[5:]}/{month[:4]}"
