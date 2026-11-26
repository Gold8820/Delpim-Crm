"""Perfil comercial: identificação automática pelos padrões de nome (exemplos reais fornecidos)."""
import pytest

from clientes import perfil


def ident(razao, fantasia=None, **kw):
    return perfil.identificar(razao, fantasia, **kw)


def campo(razao, fantasia=None, nome="tipo_cadastro", **kw):
    return ident(razao, fantasia, **kw)[0][nome]


# --------------------------------------------------------------------------- tipo de cadastro

@pytest.mark.parametrize("razao,fantasia", [
    ("(INATIVO)ADEGA PRONTO SOCORRO LOJA 2 * CONTABILIDADE DETECTA", None),
    ("(INATIVO) HORTIFRUTI ALEXANDRINO LUCIANA MARCOS FREIRE * CONTABILIDADE BATISTA", None),
    ("CASAS DE CARNES VOVO DURVAL EIRELI * ACOUGUE DURVAL", "(INATIVO) ACOUGUE CENTER CARNES DURVAL * CONTABILIDADE X"),
])
def test_inativo_vira_ex_cliente_mesmo_com_contrato(razao, fantasia):
    assert campo(razao, fantasia, tem_contrato=True) == "Ex-cliente"


@pytest.mark.parametrize("razao", ["REVENDA ALESSANDRO * REVENDA CONTABILIDADE NAO UTILIZA", "REVENDA JORGE NAKANISHI -",
                                   "REVENDA JOSIVALDO DOS SANTOS * CONTABILIDADE NAO UTILIZA", "REVENDA LUCAS PIRES SANTOS"])
def test_revendas_explicitas(razao):
    valores, duvidas = ident(razao)
    assert valores["tipo_cadastro"] == "Revenda" and not duvidas


def test_revenda_entre_parenteses_nao_e_revenda():
    """“(REVENDA JORGE JAPONES)” indica por quem o cliente foi vendido, não que ele seja uma revenda."""
    v, _ = ident("TEREZINHA GONCALVES DE ASSIS * ACOUGUE DIEGO (REVENDA JORGE JAPONES)",
                 "ACOUGUE CENTER CARNES DIEGO * CONTABILIDADE REMANSO*", tem_contrato=True)
    assert v["tipo_cadastro"] == "Cliente" and v["escritorio_contabil"] == "CONTABILIDADE REMANSO"
    assert v["segmento_nome"] == "ACOUGUE CENTER"


def test_contabilidade_vinculada_nao_torna_o_cliente_contabilidade():
    v, _ = ident("ROUPA PHORTION MATRIZ * CONTABILIDADE AAS.ASSESSORIA CONTABIL", tem_contrato=True)
    assert v["tipo_cadastro"] == "Cliente" and v["escritorio_contabil"] == "CONTABILIDADE AAS.ASSESSORIA CONTABIL"


@pytest.mark.parametrize("razao,fantasia", [
    ("(CONTABILIDADE CCLS", "CONTABILIDADE CCLS"),
    ("(CONTABILIDADE ATIVA * CONTABILIDADE ATIVA DESDE 1976 LTDA", "CONTABILIDADE ATIVA"),
    ("ESCRITORIO OLIVEIRA SERVICOS EMPRESARIAIS LTDA * (CONTABILIDADE ESCRITORIO OLIVEIRA", "(CONTABILIDADE ESCRITORIO OLIVEIRA"),
    ("(NIERI SERVICOS ADMINISTRATIVOS LTDA", "NIERI CONTABILIDADE"),
])
def test_escritorio_contabil_como_categoria(razao, fantasia):
    v, d = ident(razao, fantasia, ramo="CONTABILIDADE")
    assert v["tipo_cadastro"] == "Contabilidade" and v["escritorio_contabil"] is None and not d


def test_contabilidade_no_inicio_mas_com_outro_negocio_nao_e_contabilidade():
    v, _ = ident("CONTABILIDADE ORGPENHA * MERCADO SILVA - ANT. MERCADO MARLI",
                 "CONTABILIDADE ORGPENHA * MERCADO SILVA - ANT. MERCADO MARLI")
    assert v["tipo_cadastro"] == "Cliente" and v["escritorio_contabil"] == "CONTABILIDADE ORGPENHA"
    assert v["segmento_nome"] == "MERCADO SILVA"


def test_fornecedor():
    assert campo("BANCO ITAU", "FORNECEDOR BANCO ITAU", tem_contrato=True) == "Fornecedor"
    assert campo("TROCAFONE S.A.", "(EXCLUIR) FORNECEDOR TROCAFONE S.A.") == "Fornecedor"


@pytest.mark.parametrize("razao,fantasia,contrato", [
    ("DIOGO DA SILVA ALMEIDA COSTA", None, False),                 # sem contrato e sem marcação: Cliente
    ("(SEM CONTRATO) 4U WINE LTDA * ADEGA VINHOS 4U WINE", "(SEM CONTRATO) ADEGA VINHOS 4U WINE", False),
    ("(SEM CONTRATO) MERCADO X LTDA * MERCADO X", None, True),     # o contrato é outro campo (situação contratual)
    ("MCI CONSULTORIA LTDA * DISTRIBUIDORA MCI TELECOM", "DISTRIBUIDORA MCI TELECOM * CONTABILIDADE ALVORADA", True),
    ("(LEAD) MERCADO NOVO * MERCADO NOVO", None, False),             # não existe mais a categoria Lead
])
def test_sem_marcacao_e_cliente(razao, fantasia, contrato):
    v, d = ident(razao, fantasia, tem_contrato=contrato, ramo="CONTABILIDADE")
    assert v["tipo_cadastro"] == "Cliente" and not [c for c, _ in d if c == "tipo_cadastro"]


def test_lead_nao_existe():
    assert "Lead" not in perfil.TIPOS and perfil.limpar_valor("tipo_cadastro", "Lead")[1]
    assert perfil.SITUACOES_CONTRATUAIS == ("Contratado", "Cliente sem contrato")


# --------------------------------------------------------------------------- contabilidade

@pytest.mark.parametrize("razao,fantasia,esperado", [
    ("A M DO NASCIMENTO ADEGA * ADEGA PRONTO SOCORRO LOJA 1", "ADEGA PRONTO SOCORRO LOJA 1 * CONTABILIDADE DETECTA",
     "CONTABILIDADE DETECTA"),
    ("(CPF )BICICLETARIA CICLO BIKE 2* CONTABILIDADE NOGUEIRA", None, "CONTABILIDADE NOGUEIRA"),
    ("(INATIVO) HORTIFRUTI GIANETTI (SERGIO) * CONTABILIDADE DIANA", None, "CONTABILIDADE DIANA"),
    ("CENTER CARNES GENESIS LTDA * ACOUGUE CENTER CARNES BOI NATIVO",
     "ACOUGUE CENTER CARNES BOI NATIVO * CONTABILIDADE ALVORADA", "CONTABILIDADE ALVORADA"),
    ("(INATIVO) HORTIFRUTI ALEXANDRINO LOJA 02 (THALES) * CONTABILIDADE BATISTA", None, "CONTABILIDADE BATISTA"),
    ("X * ACOUGUE DIEGO", "ACOUGUE CENTER CARNES DIEGO * CONTABILIDADE REMANSO*", "CONTABILIDADE REMANSO"),
    ("SUELI SANCHES LOPES *DEPOSITO CONSTRUCAO DHALAS", "DEPOSITO CONSTRUCAO DHALAS * ERIVALDO CONTADOR*", "ERIVALDO CONTADOR"),
    ("X * MERCADO DO BEM", "MERCADO DO BEM *CONTABILDIADE MEDEIROS", "CONTABILDIADE MEDEIROS"),
    ("X * PADARIA JARINAS", "PADARIA JARINAS * CONTADOR GENIVAL 99623-2471-", "CONTADOR GENIVAL"),
    ("X * MERCADO PRIMAVERA", "MERCADO PRIMAVERA - CONTABILIDADE OBJETIVO", "CONTABILIDADE OBJETIVO"),
    ("X * PIZZARIA BELLA ROMA", "PIZZARIA BELA ROMA *`CONTABILIDADE JMS", "CONTABILIDADE JMS"),
])
def test_extrai_escritorio(razao, fantasia, esperado):
    v, d = ident(razao, fantasia)
    assert v["escritorio_contabil"] == esperado and not d


@pytest.mark.parametrize("razao,fantasia,situacao", [
    ("(INATIVO) HORTI FRUTI DA FAMILIA * CONTABILIDADE SOLICITAR CONTATO", None, "Solicitar contato"),
    ("MERCADO MINI PRECO (NORDESTE LJ2) * CONTABILIDADE SOLICITAR CONTATO", None, "Solicitar contato"),
    ("REVENDA JOSIVALDO DOS SANTOS * CONTABILIDADE NAO UTILIZA", None, "Não utiliza"),
    ("CONSUMIDOR CONTABILIDADE NAO UTILIZA", None, "Não utiliza"),
    ("X * ADEGA 29", "(INATIVO) ADEGA 29 *SOLICITAR CONTADOR*", "Solicitar contato"),
    ("X * HORTIFRUTI BOM SABOR", "(INATIVO) HORTIFRUTI BOM SABOR - NÃO TEM CONTABILIDADE", "Não utiliza"),
])
def test_situacao_contabil_nao_vira_nome_de_escritorio(razao, fantasia, situacao):
    v, d = ident(razao, fantasia)
    assert v["situacao_contabil"] == situacao and v["escritorio_contabil"] is None and not d


def test_contabilidade_fora_do_padrao_vai_para_inconsistencias():
    v, d = ident("(INATIVO) ROUPA DB KIDS MODA INFANTIL CONTABILIDADE 11 97326-5156* ROUPA DB KIDS MODA INFANTIL")
    assert v["escritorio_contabil"] is None and d[0][0] == "escritorio_contabil"
    assert v["segmento_nome"] == "ROUPA DB"
    v, d = ident("X * MERCADO SMART SILVA", "MERCADO SMART SILVA * CONTABILIDADE")
    assert v["escritorio_contabil"] is None and "sem o nome" in d[0][1]


# --------------------------------------------------------------------------- segmento

@pytest.mark.parametrize("razao,fantasia,esperado", [
    ("A M DO NASCIMENTO ADEGA * ADEGA PRONTO SOCORRO LOJA 1", "ADEGA PRONTO SOCORRO LOJA 1 * CONTABILIDADE DETECTA",
     "ADEGA PRONTO"),
    ("CENTER CARNES GENESIS LTDA * ACOUGUE CENTER CARNES BOI NATIVO",
     "ACOUGUE CENTER CARNES BOI NATIVO * CONTABILIDADE ALVORADA", "ACOUGUE CENTER"),
    ("(INATIVO) HORTIFRUTI ALEXANDRINO LUCIANA MARCOS FREIRE * CONTABILIDADE BATISTA", None, "HORTIFRUTI ALEXANDRINO"),
    ("ROUPA PHORTION MATRIZ * CONTABILIDADE AAS.ASSESSORIA CONTABIL", None, "ROUPA PHORTION"),
    ("MERCADO MINI PRECO (NORDESTE LJ2) * CONTABILIDADE SOLICITAR CONTATO", None, "MERCADO MINI"),
    ("(INATIVO) HENRIQUE SILVA CANTUARIA EIRELI * ACOUGUE FAGUNDES", None, "ACOUGUE FAGUNDES"),   # após o 1º asterisco
    ("(CPF )BICICLETARIA CICLO BIKE 2* CONTABILIDADE NOGUEIRA", None, "BICICLETARIA CICLO"),    # sem o prefixo (CPF)
    ("REVENDA ALESSANDRO * REVENDA CONTABILIDADE NAO UTILIZA", None, "REVENDA ALESSANDRO"),
    ("CENTRAL DE CARNES LESTEGOL EIRELI - ME *ACOUGUE  LESTE GOL", "ACOUGUE  LESTE GOL", "ACOUGUE LESTE"),
    ("X * ADEGA E TABACARIA RV", "ADEGA E TABACARIA RV * CONTABILIDADE OPENPRIMO", "ADEGA TABACARIA"),
    ("X * MERCADO VILA NOVA", "(REVENDA DIOGO) MERCADO DO BAIRRO LOJA 1(REVENDA DIOGO) *SOLICITAR CONTATO", "MERCADO BAIRRO"),
])
def test_segmento_duas_palavras_do_nome_fantasia(razao, fantasia, esperado):
    assert campo(razao, fantasia, "segmento_nome") == esperado


def test_sem_nome_fantasia_nao_inventa_segmento():
    assert campo("DIOGO DA SILVA ALMEIDA COSTA", None, "segmento_nome") is None


# --------------------------------------------------------------------------- regiões

@pytest.mark.parametrize("bairro,cidade,r1,r2", [   # Região 2 = o campo Bairro, como está
    ("Penha", "São Paulo", "Zona Leste", "Penha"),
    ("Parque da Mooca", "São Paulo", "Zona Leste", "Parque da Mooca"),
    ("Tatuapé", "SAO PAULO", "Zona Leste", "Tatuapé"),
    ("Jardim Vila Carrão", "São Paulo", "Zona Leste", "Jardim Vila Carrão"),
    ("Parque Artur Alvim", "São Paulo", "Zona Leste", "Parque Artur Alvim"),
    ("Vila Mariana", "São Paulo", "Zona Sul", "Vila Mariana"),
    ("Vila Maria Alta", "São Paulo", "Zona Norte", "Vila Maria Alta"),
    ("Jardim da Conquista (Zona Leste)", "São Paulo", "Zona Leste", "Jardim da Conquista (Zona Leste)"),
    ("Jardim Angela (Zona Leste)", "São Paulo", "Zona Leste", "Jardim Angela (Zona Leste)"),   # o escrito vence
    ("Vila Nhocune", "São Paulo", None, "Vila Nhocune"),                          # não mapeado: sem Região 1
    ("Cidade Jardim Cumbica", "Guarulhos", None, "Cidade Jardim Cumbica"),       # outra cidade: não inventa
    (None, "São Paulo", None, None),
])
def test_regioes(bairro, cidade, r1, r2):
    v, d = ident("X * MERCADO X", bairro=bairro, cidade=cidade)
    assert (v["regiao1"], v["regiao2"]) == (r1, r2) and not d


def test_mapeamento_configuravel():
    mapa, erros = perfil.mapa_de_texto("Guarulhos; ; Grande SP Norte\nSão Paulo; Vila Nhocune; Zona Leste\n"
                                       "# comentário\nsem região")
    assert erros == ["Linha 4: informe ao menos a cidade e a Região 1."]
    comp = perfil._compilar_mapa(mapa)
    assert perfil.identificar_regiao("Vila Nhocune", "São Paulo", comp)[:2] == ("Zona Leste", "Vila Nhocune")
    assert perfil.identificar_regiao("Cumbica", "Guarulhos", comp)[:2] == ("Grande SP Norte", "Cumbica")
    texto = perfil.mapa_para_texto(perfil.MAPA_PADRAO)
    assert perfil.mapa_de_texto(texto) == (perfil.MAPA_PADRAO, [])


def test_bairro_ambiguo_vai_para_inconsistencias():
    comp = perfil._compilar_mapa([{"cidade": "X", "bairro": "Alfa", "regiao1": "Zona Leste"},
                                  {"cidade": "X", "bairro": "Beta", "regiao1": "Zona Sul"}])
    r1, r2, duvida = perfil.identificar_regiao("Alfa Beta", "X", comp)
    assert r1 is None and duvida


# --------------------------------------------------------------------------- edição manual

def test_manual_preservado_ate_a_planilha_mudar():
    auto = {"tipo_cadastro": "Cliente", "regiao1": None}
    atual = {"tipo_cadastro": "Revenda", "regiao1": "Zona Leste"}
    manuais = {"tipo_cadastro", "regiao1"}
    # mesma informação na planilha: o manual fica
    v, m = perfil.mesclar(atual, auto, manuais, {"tipo_cadastro": "Cliente", "regiao1": None})
    assert v["tipo_cadastro"] == "Revenda" and v["regiao1"] == "Zona Leste" and m == manuais
    # planilha sem informação (região vazia) não apaga o manual
    v, m = perfil.mesclar(atual, {**auto, "regiao1": "Zona Sul"}, manuais, {"tipo_cadastro": "Cliente", "regiao1": None})
    assert v["regiao1"] == "Zona Leste"
    # o nome passou a ter (INATIVO): informação nova e suficiente substitui
    v, m = perfil.mesclar(atual, auto, manuais, {"tipo_cadastro": "Ex-cliente", "regiao1": None})
    assert v["tipo_cadastro"] == "Ex-cliente" and m == {"regiao1"}
    # contrato novo (tipo "Cliente", deduzido) nunca substitui um tipo manual
    v, _ = perfil.mesclar(atual, {"tipo_cadastro": None}, {"tipo_cadastro"}, {"tipo_cadastro": "Cliente"})
    assert v["tipo_cadastro"] == "Revenda"


def test_limpar_valor():
    assert perfil.limpar_valor("tipo_cadastro", "Revenda") == ("Revenda", None)
    assert perfil.limpar_valor("tipo_cadastro", "Qualquer")[1]
    assert perfil.limpar_valor("regiao1", "  Zona   Leste ") == ("Zona Leste", None)
    assert perfil.limpar_valor("regiao1", "") == (None, None)
