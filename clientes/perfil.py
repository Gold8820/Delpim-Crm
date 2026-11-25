"""Perfil comercial do cliente: classificação automática a partir dos padrões de nome do cadastro.

Campos (colunas da tabela clientes):
  tipo_cadastro        Cliente, Cliente sem contrato, Ex-cliente, Lead, Revenda, Contabilidade, Fornecedor
  escritorio_contabil  escritório de contabilidade VINCULADO ao cliente (não é a categoria do cliente)
  situacao_contabil    "Não utiliza" / "Solicitar contato" (CONTABILIDADE NAO UTILIZA, SOLICITAR CONTATO)
  segmento_nome        duas primeiras palavras significativas do nome fantasia (ADEGA PRONTO SOCORRO → ADEGA PRONTO)
  regiao1, regiao2     zona (Zona Leste...) e bairro/distrito, pelo mapeamento geográfico configurável
A situação contratual (Contratado / Não contratado) não é guardada: vem sempre do relacionamento com os contratos.
O vendedor responsável é o campo `vendedor` do cadastro (importado quando a planilha traz a coluna).

Padrão dos nomes nas planilhas reais: "RAZÃO SOCIAL * NOME FANTASIA * CONTABILIDADE", com prefixos entre
parênteses como (INATIVO) e (SEM CONTRATO). Quando a identificação é duvidosa, o campo fica vazio e o motivo vai
para o relatório de inconsistências.

Edição manual: o campo editado à mão entra em `perfil_manual` e não é sobrescrito em importações futuras, a não
ser que a informação da planilha que gerou o valor automático MUDE (por exemplo, o nome passou a ter (INATIVO)).
`perfil_auto` guarda o último valor automático de cada campo para essa comparação.
"""
from __future__ import annotations

import json
import re
import unicodedata

TIPOS = ("Cliente", "Cliente sem contrato", "Ex-cliente", "Lead", "Revenda", "Contabilidade", "Fornecedor")
SITUACOES_CONTABEIS = ("Não utiliza", "Solicitar contato")
CAMPOS = ("tipo_cadastro", "escritorio_contabil", "situacao_contabil", "segmento_nome", "regiao1", "regiao2")
ROTULOS = {
    "tipo_cadastro": "Tipo de cadastro",
    "escritorio_contabil": "Escritório de contabilidade",
    "situacao_contabil": "Atendimento contábil",
    "segmento_nome": "Segmento",
    "regiao1": "Região 1",
    "regiao2": "Região 2",
}
MAX_LEN = 120
# "Cliente" é deduzido só da existência de contrato: não substitui uma classificação manual.
_TIPOS_FRACOS = {"Cliente"}


# --------------------------------------------------------------------------- texto

def norm(texto) -> str:
    """Maiúsculas, sem acentos e com espaços simples."""
    s = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", s).strip()


def _chave(texto) -> str:
    return re.sub(r"[^A-Z0-9]", "", norm(texto))


_TAG = re.compile(r"^\(([^()]*)\)\s*")
# "CONTAB\w*" cobre os erros de digitação das planilhas reais (CONTABILDIADE, CONTABLIDADE, CONTABILIDAE...)
_CONTAB_INI = re.compile(r"^(CONTAB\w*|CONTADORA?|ESCRITORIO( DE)? CONTAB\w*|ORGANIZACAO CONTAB\w*|ASSESSORIA CONTAB\w*)\b")
_CONTAB_FIM = re.compile(r"\b(CONTADORA?|CONTAB\w*)$")    # "ERIVALDO CONTADOR", "AGEF CONTABILIDADE"
_CONTAB_PALAVRA = re.compile(r"\b(CONTAB\w*|CONTADORA?)\b")
_NAO_UTILIZA = re.compile(r"\b(CONTAB\w*|CONTADOR) NAO UTILIZA\b|\bNAO TEM CONTAB|\bSEM CONTAB")
_SOLICITAR = re.compile(r"\bSOLICITAR\b")
_PALAVRAS_CONTABEIS = {"CONTABILIDADE", "CONTABIL", "CONTADOR", "CONTADORA", "ESCRITORIO", "DE", "ORGANIZACAO",
                       "ASSESSORIA", "E", "N", "A", "NA"}
_TELEFONE = re.compile(r"\(?\b\d{2}\)?\s*\d{4,5}\s*-?\s*\d{4}\b-?|\b\d{4,5}-\d{4}\b-?")
_PALAVRAS_VAZIAS = {"DE", "DA", "DO", "DAS", "DOS", "E", "EM", "LTDA", "ME", "EPP", "EIRELI", "SA", "S/A", "S.A",
                    "S.A.", "S/S", "INATIVO", "INATIVA"}


def _sem_parenteses(seg: str) -> str:
    """Tira o conteúdo entre parênteses (informação auxiliar) e o '(' inicial sem fechamento."""
    s = seg.strip()
    if s.startswith("(") and s.count("(") > s.count(")"):
        s = s[1:]
    s = re.sub(r"\([^()]*\)", " ", s)
    s = re.sub(r"\([^)]*$", " ", s)          # "(REVENDA DIOGO" sem fechamento, no fim
    s = s.replace(")", " ")
    s = re.sub(r"^[^A-Z0-9]+", "", s)       # sujeira no início ("`CONTABILIDADE JMS")
    return re.sub(r"\s+", " ", s).strip(" -")


def _analisar(texto) -> dict:
    """Prefixos entre parênteses (tags) e partes separadas por asterisco, já limpas."""
    t = norm(texto)
    tags = []
    while (m := _TAG.match(t)):
        tags.append(m.group(1).strip())
        t = t[m.end():]
    partes = []
    for bruto in t.split("*"):
        for pedaco in re.split(r"\s-\s*(?=(?:CONTAB|CONTADOR|NAO TEM CONTAB))", bruto):   # "MERCADO X - CONTABILIDADE Y"
            limpo = _sem_parenteses(pedaco)
            if limpo:
                partes.append({"texto": limpo, "tipo": _tipo_parte(limpo)})
    return {"tags": tags, "partes": partes}


def _tipo_parte(s: str) -> str | None:
    if _NAO_UTILIZA.search(s):
        return "nao_utiliza"
    if _SOLICITAR.search(s):
        return "solicitar"
    sem_sufixo = re.sub(r"(\s+-?\s*(LTDA|ME|EPP|EIRELI|S/?S|S/?A)\.?)+$", "", s)
    if _CONTAB_INI.match(s) or _CONTAB_FIM.search(sem_sufixo):
        return "escritorio"
    return None


def _comeca(partes: list[dict], palavra: str) -> bool:
    return bool(partes) and re.match(rf"{palavra}\b", partes[0]["texto"]) is not None


def _nome_escritorio(texto: str) -> str | None:
    s = re.sub(r"\s+", " ", _TELEFONE.sub(" ", texto)).strip(" -.")
    resto = [w.replace(".", "") for w in re.findall(r"[A-Z0-9][A-Z0-9.]*", s)]   # "R.A", "W.B": iniciais
    resto = [w for w in resto if w not in _PALAVRAS_CONTABEIS and not w.isdigit()]
    return s[:MAX_LEN] if any(re.search(r"[A-Z].*[A-Z]", w) for w in resto) else None


def _segmento(fantasia: str | None) -> str | None:
    if not fantasia:
        return None
    palavras = []
    for w in re.findall(r"[A-Z0-9][A-Z0-9./']*", fantasia):
        w = w.strip(".'")
        if w in _PALAVRAS_VAZIAS or len(w) < 2 or not re.search(r"[A-Z]", w):
            continue
        palavras.append(w)
        if len(palavras) == 2:
            break
    return " ".join(palavras) or None


# --------------------------------------------------------------------------- regiões

_ZONAS = {"LESTE": "Zona Leste", "NORTE": "Zona Norte", "SUL": "Zona Sul", "OESTE": "Zona Oeste", "CENTRO": "Centro"}

# Mapeamento inicial (editável em Configurações > Regiões): os 96 distritos da cidade de São Paulo por zona.
# Região 2 = distrito. Um bairro do cadastro "pertence" ao distrito quando contém o nome dele como palavras
# inteiras (Parque da Mooca → Mooca). Outras cidades só têm região se forem cadastradas no mapeamento.
_DISTRITOS_SP = {
    "Centro": "Bela Vista, Bom Retiro, Cambuci, Consolação, Liberdade, República, Santa Cecília, Sé",
    "Zona Oeste": "Alto de Pinheiros, Barra Funda, Butantã, Itaim Bibi, Jaguara, Jaguaré, Jardim Paulista, Lapa, "
                  "Morumbi, Perdizes, Pinheiros, Raposo Tavares, Rio Pequeno, Vila Leopoldina, Vila Sônia",
    "Zona Norte": "Anhanguera, Brasilândia, Cachoeirinha, Casa Verde, Freguesia do Ó, Jaçanã, Jaraguá, Limão, "
                  "Mandaqui, Perus, Pirituba, Santana, São Domingos, Tremembé, Tucuruvi, Vila Guilherme, "
                  "Vila Maria, Vila Medeiros",
    "Zona Sul": "Campo Belo, Campo Grande, Campo Limpo, Capão Redondo, Cidade Ademar, Cidade Dutra, Cursino, "
                "Grajaú, Ipiranga, Jabaquara, Jardim Ângela, Jardim São Luís, Marsilac, Moema, Parelheiros, "
                "Pedreira, Sacomã, Santo Amaro, Saúde, Socorro, Vila Andrade, Vila Mariana",
    "Zona Leste": "Água Rasa, Aricanduva, Artur Alvim, Belém, Brás, Cangaíba, Carrão, Cidade Líder, "
                  "Cidade Tiradentes, Ermelino Matarazzo, Guaianases, Iguatemi, Itaim Paulista, Itaquera, "
                  "Jardim Helena, José Bonifácio, Lajeado, Mooca, Pari, Parque do Carmo, Penha, Ponte Rasa, "
                  "São Lucas, São Mateus, São Miguel, São Rafael, Sapopemba, Tatuapé, Vila Curuçá, Vila Formosa, "
                  "Vila Jacuí, Vila Matilde, Vila Prudente",
}
# Bairros frequentes no cadastro que não contêm o nome do distrito (Região 2 = o próprio bairro do cadastro).
_BAIRROS_SP = {
    "Zona Leste": "Belenzinho, Catumbi, Engenheiro Goulart, Vila Ré, Cidade Antônio Estevão de Carvalho, "
                  "Vila Carmosina, Vila Gomes Cardim, Burgo Paulista, Fazenda da Juta, Parque Císper, "
                  "Parque Savoy City, Jardim Adutora, Guaianazes",
    "Zona Norte": "Parada Inglesa, Itaberaba, Vila Penteado",
}
MAPA_PADRAO = [{"cidade": "São Paulo", "bairro": d.strip(), "regiao1": zona, "regiao2": d.strip()}
               for zona, lista in _DISTRITOS_SP.items() for d in lista.split(",")] + [
              {"cidade": "São Paulo", "bairro": b.strip(), "regiao1": zona, "regiao2": ""}
              for zona, lista in _BAIRROS_SP.items() for b in lista.split(",")]


def mapa_para_texto(mapa: list[dict]) -> str:
    return "\n".join(f"{m['cidade']}; {m.get('bairro') or ''}; {m['regiao1']}; {m.get('regiao2') or ''}".rstrip("; ")
                     for m in mapa)


def mapa_de_texto(texto: str) -> tuple[list[dict], list[str]]:
    """Linhas "Cidade; Bairro; Região 1; Região 2" (bairro vazio = a cidade toda). Retorna (mapa, erros)."""
    mapa, erros = [], []
    for n, linha in enumerate(str(texto or "").splitlines(), 1):
        if not linha.strip() or linha.lstrip().startswith("#"):
            continue
        partes = [p.strip() for p in linha.split(";")] + ["", "", "", ""]
        cidade, bairro, r1, r2 = partes[:4]
        if not cidade or not r1:
            erros.append(f"Linha {n}: informe ao menos a cidade e a Região 1.")
            continue
        if any(len(p) > MAX_LEN for p in (cidade, bairro, r1, r2)):
            erros.append(f"Linha {n}: texto muito longo.")
            continue
        mapa.append({"cidade": cidade, "bairro": bairro, "regiao1": r1, "regiao2": r2})
    if len(mapa) > 5000:
        erros.append("O mapeamento aceita até 5.000 linhas.")
    return mapa, erros


def _compilar_mapa(mapa: list[dict]) -> dict:
    por_cidade: dict = {}
    for m in mapa:
        c = por_cidade.setdefault(norm(m["cidade"]), {"cidade": None, "bairros": []})
        if m.get("bairro"):
            c["bairros"].append((norm(m["bairro"]), m["regiao1"], m.get("regiao2") or m["bairro"]))
        else:
            c["cidade"] = (m["regiao1"], m.get("regiao2") or None)
    return por_cidade


def _palavras(texto: str) -> str:
    return " " + re.sub(r"[^A-Z0-9]+", " ", texto).strip() + " "


def _contem(frase: str, trecho: str) -> bool:
    """`trecho` aparece em `frase` como palavras inteiras (Vila Maria ≠ Vila Mariana). Sem regex: CPU baixa."""
    if len(trecho) <= 2:
        return frase.strip() == trecho
    return trecho in frase and _palavras(trecho) in _palavras(frase)


def identificar_regiao(bairro, cidade, mapa_compilado: dict) -> tuple[str | None, str | None, str | None]:
    """(região 1, região 2, dúvida). Sem informação suficiente, a região fica vazia (nunca é inventada)."""
    b_orig = re.sub(r"\s+", " ", str(bairro or "")).strip()
    b = norm(b_orig)
    zona_explicita = None
    if (m := re.search(r"\(?\bZONA (LESTE|NORTE|SUL|OESTE)\b\)?", b)):
        zona_explicita = _ZONAS[m.group(1)]
        b = re.sub(r"\s+", " ", b.replace(m.group(0), " ")).strip()
        b_orig = re.sub(r"\s*\(?\b[Zz][Oo][Nn][Aa] \w+\)?", "", b_orig).strip()
    regras = mapa_compilado.get(norm(cidade))
    r1 = r2 = None
    if regras and b:
        achados = [x for x in regras["bairros"] if _contem(b, x[0])]
        if achados:
            maior = max(achados, key=lambda x: len(x[0]))
            conflito = [x for x in achados if x is not maior and x[0] not in maior[0] and x[1] != maior[1]]
            if conflito:
                return None, b_orig or None, (f"bairro “{b_orig}” corresponde a mais de uma região "
                                              f"({maior[1]} e {conflito[0][1]}).")
            r1, r2 = maior[1], maior[2]
    if r1 is None and regras and regras["cidade"]:
        r1 = regras["cidade"][0]
        r2 = regras["cidade"][1] or None
    if zona_explicita:   # "(Zona Leste)" escrito no bairro vale mais que o mapeamento
        if r1 != zona_explicita:
            r2 = None
        r1 = zona_explicita
    if r2 is None and b_orig:
        r2 = b_orig[:MAX_LEN]
    return r1, r2, None


# --------------------------------------------------------------------------- identificação

def identificar(razao, fantasia, *, bairro=None, cidade=None, ramo=None, tem_contrato=False,
                mapa_compilado: dict | None = None) -> tuple[dict, list[tuple[str, str]]]:
    """Valores automáticos dos campos do perfil e as dúvidas encontradas [(campo, motivo)]."""
    raz, fan = _analisar(razao), _analisar(fantasia)
    duvidas: list[tuple[str, str]] = []
    tags = raz["tags"] + fan["tags"]
    # partes da referência do nome fantasia: o campo Nome Fantasia, se preenchido; senão a Razão Social
    fonte = fan if fan["partes"] else raz

    inativo = any(re.match(r"INATIV[OA]\b", t) for t in tags)
    sem_contrato = any("SEM CONTRATO" in t for t in tags)
    lead = any(t == "LEAD" for t in tags) or _comeca(fonte["partes"], "LEAD") or _comeca(raz["partes"], "LEAD")
    fornecedor = _comeca(fan["partes"], "FORNECEDOR") or _comeca(raz["partes"], "FORNECEDOR")
    revenda = _comeca(fonte["partes"], "REVENDA") or _comeca(raz["partes"], "REVENDA") or (
        len(raz["partes"]) > 1 and raz["partes"][1]["tipo"] is None and _comeca(raz["partes"][1:], "REVENDA"))
    # a própria empresa é um escritório contábil: todas as partes do nome fantasia são de contabilidade
    contabilidade = bool(fonte["partes"]) and all(p["tipo"] == "escritorio" for p in fonte["partes"])

    # --- tipo de cadastro
    ramo_contab = norm(ramo).startswith("CONTAB")
    if inativo:
        tipo = "Ex-cliente"
    elif fornecedor:
        tipo = "Fornecedor"
    elif revenda:
        tipo = "Revenda"
    elif contabilidade:
        tipo = "Contabilidade"
    elif lead:
        tipo = "Lead"
    elif ramo_contab:
        tipo = None
        duvidas.append(("tipo_cadastro", "o ramo de atividade é Contabilidade, mas o nome não segue o padrão de "
                                         "escritório contábil."))
    elif sem_contrato:
        tipo = None if tem_contrato else "Cliente sem contrato"
        if tem_contrato:
            duvidas.append(("tipo_cadastro", "o nome indica (SEM CONTRATO), mas há contrato relacionado."))
    else:
        tipo = "Cliente" if tem_contrato else None

    # --- escritório e situação do atendimento contábil (não para o próprio escritório contábil)
    escritorio = situacao = None
    if tipo != "Contabilidade" and not contabilidade:
        def escritorios(analise):
            nomes, sem_nome = {}, False
            for p in analise["partes"]:
                if p["tipo"] == "escritorio":
                    nome = _nome_escritorio(p["texto"])
                    if nome:
                        nomes.setdefault(_chave(nome), nome)
                    else:
                        sem_nome = True
            return nomes, sem_nome

        de_fan, vazio_fan = escritorios(fan)
        de_raz, vazio_raz = escritorios(raz)
        candidatos = de_fan or de_raz
        if len(candidatos) > 1:
            duvidas.append(("escritorio_contabil", "mais de um escritório no nome (" + ", ".join(candidatos.values()) + ")."))
        elif de_fan and de_raz and set(de_fan) != set(de_raz):
            duvidas.append(("escritorio_contabil", f"a razão social indica “{next(iter(de_raz.values()))}” e o nome "
                                                   f"fantasia indica “{next(iter(de_fan.values()))}”."))
        elif candidatos:
            escritorio = next(iter(candidatos.values()))
        elif vazio_fan or vazio_raz:
            duvidas.append(("escritorio_contabil", "contabilidade citada sem o nome do escritório."))
        tipos_status = {p["tipo"] for p in fan["partes"] + raz["partes"]} & {"nao_utiliza", "solicitar"}
        if len(tipos_status) > 1:
            duvidas.append(("situacao_contabil", "o nome diz ao mesmo tempo NÃO UTILIZA e SOLICITAR CONTATO."))
        elif tipos_status:
            situacao = "Não utiliza" if "nao_utiliza" in tipos_status else "Solicitar contato"
        if escritorio is None and not candidatos and not (vazio_fan or vazio_raz):
            soltas = [p["texto"] for p in fan["partes"] + raz["partes"]
                      if p["tipo"] is None and _CONTAB_PALAVRA.search(p["texto"])]
            if soltas:
                duvidas.append(("escritorio_contabil", f"menção a contabilidade fora do padrão (“{soltas[0]}”)."))

    # --- segmento: duas primeiras palavras significativas do nome fantasia (só para clientes e ex-clientes)
    segmento = None
    if tipo not in ("Revenda", "Fornecedor", "Contabilidade") and not contabilidade:
        nome_fantasia = None
        if fan["partes"]:
            nome_fantasia = next((p["texto"] for p in fan["partes"] if p["tipo"] is None), None)
        elif len(raz["partes"]) > 1:
            depois = [p["texto"] for p in raz["partes"][1:] if p["tipo"] is None]
            nome_fantasia = depois[0] if depois else (raz["partes"][0]["texto"] if raz["partes"][0]["tipo"] is None else None)
        segmento = _segmento(nome_fantasia)

    regiao1, regiao2, duvida_regiao = identificar_regiao(bairro, cidade, mapa_compilado or _compilar_mapa(MAPA_PADRAO))
    if duvida_regiao:
        duvidas.append(("regiao1", duvida_regiao))

    valores = {"tipo_cadastro": tipo, "escritorio_contabil": escritorio, "situacao_contabil": situacao,
               "segmento_nome": segmento, "regiao1": regiao1, "regiao2": regiao2}
    return valores, duvidas


# --------------------------------------------------------------------------- banco

def _json(value, padrao):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return padrao
    return value if isinstance(value, type(padrao)) else padrao


def carregar_mapa(db) -> list[dict]:
    row = db.execute("SELECT valor FROM configuracoes_sistema WHERE chave = 'mapa_regioes'").fetchone()
    salvo = _json(row["valor"], []) if row else None
    return salvo if salvo else [dict(m) for m in MAPA_PADRAO]


def salvar_mapa(db, mapa: list[dict]) -> None:
    from .repository import now
    db.execute("INSERT INTO configuracoes_sistema (chave, valor, atualizado_em) VALUES ('mapa_regioes', %s::jsonb, %s) "
               "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor, atualizado_em = excluded.atualizado_em",
               (json.dumps(mapa, ensure_ascii=False), now()))


def mesclar(atual: dict, auto_antigo: dict, manuais: set, novo_auto: dict) -> tuple[dict, set]:
    """Valor final de cada campo, respeitando as edições manuais. Retorna (valores, manuais)."""
    valores, manuais = {}, set(manuais)
    for campo in CAMPOS:
        novo = novo_auto.get(campo)
        if campo in manuais:
            mudou = novo is not None and novo != auto_antigo.get(campo)
            if mudou and not (campo == "tipo_cadastro" and novo in _TIPOS_FRACOS):
                valores[campo] = novo
                manuais.discard(campo)
            else:
                valores[campo] = atual.get(campo)
        else:
            valores[campo] = novo
    return valores, manuais


def reclassificar(db, ids: list[int] | None = None) -> dict:
    """Recalcula o perfil dos clientes (todos, ou só `ids`) e grava o que mudou. Não faz commit.

    Retorna {"alterados", "por_tipo", "duvidas": [{codigo, documento, nome, campo, motivo}]}.
    """
    from .repository import now
    from .validation import display_name, format_cnpj

    mapa = _compilar_mapa(carregar_mapa(db))
    filtro, params = ("WHERE c.id = ANY(%s::bigint[])", ([int(i) for i in ids],)) if ids is not None else ("", ())
    rows = db.execute(
        "SELECT c.id, c.codigo, c.cnpj, c.razao_social, c.nome_fantasia, c.bairro, c.cidade, c.segmento, "
        f"{', '.join('c.' + k for k in CAMPOS)}, c.perfil_auto, c.perfil_manual, "
        "EXISTS (SELECT 1 FROM contratos ct WHERE ct.cliente_id = c.id) AS tem_contrato "
        f"FROM clientes c {filtro} ORDER BY c.id", params).fetchall()
    mudancas, duvidas, por_tipo = [], [], {}
    for r in rows:
        novo_auto, dv = identificar(r["razao_social"], r["nome_fantasia"], bairro=r["bairro"], cidade=r["cidade"],
                                    ramo=r["segmento"], tem_contrato=bool(r["tem_contrato"]), mapa_compilado=mapa)
        auto_antigo = _json(r["perfil_auto"], {})
        manuais = set(_json(r["perfil_manual"], []))
        atual = {k: r[k] for k in CAMPOS}
        valores, manuais_novos = mesclar(atual, auto_antigo, manuais, novo_auto)
        if valores != atual or novo_auto != auto_antigo or manuais_novos != manuais:
            mudancas.append({"id": r["id"], **valores, "perfil_auto": novo_auto,
                             "perfil_manual": sorted(manuais_novos)})
        tipo = valores["tipo_cadastro"] or "Sem classificação"
        por_tipo[tipo] = por_tipo.get(tipo, 0) + 1
        nome = display_name(dict(r))
        for campo, motivo in dv:
            if campo in manuais_novos:
                continue   # já definido à mão: não é pendência
            duvidas.append({"codigo": r["codigo"], "documento": format_cnpj(r["cnpj"]) or "", "nome": nome,
                            "campo": ROTULOS[campo], "motivo": motivo})
    if mudancas:
        cols = ", ".join(f"{k} text" for k in CAMPOS)
        sets = ", ".join(f"{k} = v.{k}" for k in CAMPOS)
        db.execute(f"UPDATE clientes c SET {sets}, perfil_auto = v.perfil_auto, perfil_manual = v.perfil_manual "
                   f"FROM jsonb_to_recordset(%s::jsonb) AS v(id bigint, {cols}, perfil_auto jsonb, perfil_manual jsonb) "
                   "WHERE c.id = v.id", (json.dumps(mudancas, ensure_ascii=False),))
    return {"alterados": len(mudancas), "por_tipo": por_tipo, "duvidas": duvidas, "processados": len(rows),
            "em": now()}


def limpar_valor(campo: str, valor) -> tuple[str | None, str | None]:
    """Valida um valor digitado (edição manual). Retorna (valor, erro)."""
    texto = re.sub(r"\s+", " ", str(valor or "")).strip() or None
    if texto is None:
        return None, None
    if campo == "tipo_cadastro" and texto not in TIPOS:
        return None, "Tipo de cadastro inválido."
    if campo == "situacao_contabil" and texto not in SITUACOES_CONTABEIS:
        return None, "Situação do atendimento contábil inválida."
    if len(texto) > MAX_LEN:
        return None, f"Use até {MAX_LEN} caracteres."
    return texto, None


def salvar_manual(db, client_id: int, edicoes: dict) -> dict:
    """Grava os campos do perfil editados à mão (só os enviados). Não faz commit. Retorna erros por campo."""
    erros, limpos = {}, {}
    for campo, valor in edicoes.items():
        if campo not in CAMPOS:
            continue
        limpos[campo], erro = limpar_valor(campo, valor)
        if erro:
            erros[campo] = erro
    if erros or not limpos:
        return erros
    row = db.execute("SELECT perfil_auto, perfil_manual FROM clientes WHERE id = %s", (client_id,)).fetchone()
    auto = _json(row["perfil_auto"], {})
    manuais = set(_json(row["perfil_manual"], []))
    for campo, valor in limpos.items():
        if valor == auto.get(campo):
            manuais.discard(campo)      # igual ao automático: volta a ser automático
        else:
            manuais.add(campo)
    sets = ", ".join(f"{k} = %({k})s" for k in limpos)
    db.execute(f"UPDATE clientes SET {sets}, perfil_manual = %(manual)s::jsonb WHERE id = %(id)s",
               {**limpos, "manual": json.dumps(sorted(manuais)), "id": client_id})
    return {}
