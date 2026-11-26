"""Perfil comercial de ponta a ponta: importação → classificação → filtros → edição manual → reimportação."""
from test_importacao_automatica import CAB_CLIENTES, CAB_CONTRATOS, cnpj, etapa, linha_cliente, linha_contrato

CLIENTES = [  # (n, razão social, nome fantasia, bairro, cidade, ramo)
    (1, "A M DO NASCIMENTO ADEGA * ADEGA PRONTO SOCORRO LOJA 1", "ADEGA PRONTO SOCORRO LOJA 1 * CONTABILIDADE DETECTA",
     "Penha", "São Paulo", "ADEGA"),
    (2, "(INATIVO) HORTIFRUTI ALEXANDRINO LUCIANA MARCOS FREIRE * CONTABILIDADE BATISTA", "", "Parque da Mooca",
     "São Paulo", ""),
    (3, "REVENDA ALESSANDRO * REVENDA CONTABILIDADE NAO UTILIZA", "", "Tatuapé", "São Paulo", ""),
    (4, "MERCADO MINI PRECO (NORDESTE LJ2) * CONTABILIDADE SOLICITAR CONTATO", "", "Cidade Jardim Cumbica",
     "Guarulhos", "MERCADO"),
    (5, "(CONTABILIDADE BATISTA UNIPESSOAL LTDA", "CONTABILIDADE BATISTA", "Penha", "São Paulo", "CONTABILIDADE"),
    (6, "DIOGO DA SILVA ALMEIDA COSTA", "", "Vila Nhocune", "São Paulo", ""),
    (7, "(SEM CONTRATO) 4U WINE LTDA * ADEGA VINHOS 4U WINE", "(SEM CONTRATO) ADEGA VINHOS 4U WINE", "Itaquera",
     "São Paulo", ""),
    (8, "(INATIVO) HORTIFRUTI GIANETTI (SERGIO) * CONTABILIDADE BATISTA", "", "Penha", "São Paulo", ""),
    (9, "(INATIVO) ROUPA DB KIDS MODA INFANTIL CONTABILIDADE 11 97326-5156* ROUPA DB KIDS MODA INFANTIL", "",
     "Jardim da Conquista (Zona Leste)", "São Paulo", ""),
]


def linhas_clientes(mudar=None):
    mudar = mudar or {}
    out = []
    for n, razao, fantasia, bairro, cidade, ramo in CLIENTES:
        out.append(linha_cliente(n, cnpj(n), **{"Razão Social": razao, "Nome Fantasia": fantasia, "Bairro": bairro,
                                               "Cidade": cidade, "Ramo Atividade": ramo, **mudar.get(n, {})}))
    return out


def importar(client):
    _, _, feito_c = etapa(client, "clientes", CAB_CLIENTES, linhas_clientes())
    _, _, feito_k = etapa(client, "contratos", CAB_CONTRATOS,
                          [linha_contrato("100", cnpj(1)), linha_contrato("200", cnpj(2)), linha_contrato("300", cnpj(5))])
    return feito_c, feito_k


def lista(client, **filtros):
    res = client.get("/api/clientes", query_string={"por_pagina": 200, **filtros})
    assert res.status_code == 200, res.json
    return res.json


def por_doc(client):
    return {c["cnpj"].replace(".", "").replace("/", "").replace("-", ""): c for c in lista(client)["clientes"]}


def test_importacao_classifica_e_relata_duvidas(client, consulta):
    feito_c, feito_k = importar(client)
    assert feito_k["perfil"]["processados"] == 9
    c = por_doc(client)
    a = c[cnpj(1)]
    assert (a["tipo_cadastro"], a["escritorio_contabil"], a["regiao1"], a["regiao2"],
            a["situacao_contratual"]) == ("Cliente", "CONTABILIDADE DETECTA", "Zona Leste", "Penha", "Contratado")
    assert "situacao_contabil" not in a and "segmento_nome" not in a       # campos removidos não aparecem na API
    # salvo nos campos do banco (não só calculado na tela)
    assert tuple(consulta("SELECT escritorio_contabil, regiao1, perfil_auto->>'escritorio_contabil' FROM clientes "
                          "WHERE cnpj = %s", (cnpj(1),))[0].values()) == ("CONTABILIDADE DETECTA", "Zona Leste", "CONTABILIDADE DETECTA")
    assert a["razao_social"].startswith("A M DO NASCIMENTO")        # o nome original não é alterado
    assert c[cnpj(2)]["tipo_cadastro"] == "Ex-cliente" and c[cnpj(2)]["razao_social"].startswith("(INATIVO)")
    assert c[cnpj(2)]["regiao2"] == "Parque da Mooca" and c[cnpj(2)]["situacao_contratual"] == "Contratado"
    assert c[cnpj(3)]["tipo_cadastro"] == "Revenda" and c[cnpj(3)]["escritorio_contabil"] is None
    m = c[cnpj(4)]
    assert m["escritorio_contabil"] is None and m["segmento"] == "MERCADO"   # ramo de atividade da planilha
    assert m["regiao1"] is None and m["regiao2"] == "Cidade Jardim Cumbica"     # Guarulhos não mapeado: não inventa
    assert m["tipo_cadastro"] == "Cliente" and m["situacao_contratual"] == "Cliente sem contrato"
    assert c[cnpj(5)]["tipo_cadastro"] == "Contabilidade" and c[cnpj(5)]["escritorio_contabil"] is None
    assert c[cnpj(6)]["tipo_cadastro"] == "Cliente"
    assert c[cnpj(7)]["tipo_cadastro"] == "Cliente" and c[cnpj(7)]["situacao_contratual"] == "Cliente sem contrato"
    assert c[cnpj(9)]["regiao1"] == "Zona Leste" and c[cnpj(9)]["escritorio_contabil"] is None
    duvidas = feito_k["perfil"]["duvidas"]
    assert [(d["documento"].replace(".", "").replace("/", "").replace("-", ""), d["campo"]) for d in duvidas] == [
        (cnpj(9), "Escritório de contabilidade")]


def test_filtros_combinados(client):
    importar(client)
    docs = lambda r: sorted(x["cnpj"].replace(".", "").replace("/", "").replace("-", "") for x in r["clientes"])
    assert docs(lista(client, tipo_cadastro="Revenda", regiao1="Zona Leste")) == [cnpj(3)]
    assert docs(lista(client, contrato="nao")) == sorted(cnpj(n) for n in (3, 4, 6, 7, 8, 9))
    assert docs(lista(client, tipo_cadastro="Ex-cliente", escritorio_contabil="contabilidade batista")) == \
        sorted([cnpj(2), cnpj(8)])
    assert docs(lista(client, segmento="ADEGA")) == [cnpj(1)]                 # Ramo de atividade
    assert docs(lista(client, segmento="mercado", regiao1="Zona Leste")) == []      # combinado com outro filtro
    assert docs(lista(client, regiao2="Penha", contrato="sim")) == sorted([cnpj(1), cnpj(5)])
    assert docs(lista(client, pendente="regiao1")) == sorted([cnpj(4), cnpj(6)])   # Guarulhos e Vila Nhocune sem regra
    assert cnpj(9) in docs(lista(client, pendente="contabilidade"))
    meta = client.get("/api/meta").json["perfil"]
    assert "CONTABILIDADE BATISTA" in meta["opcoes"]["escritorio_contabil"]
    assert meta["opcoes"]["regiao1"] == ["Zona Leste"] and "Revenda" in meta["tipos"]


def test_edicao_manual_preservada_na_reimportacao(client):
    importar(client)
    c = por_doc(client)
    cid = c[cnpj(6)]["id"]
    atual = client.get(f"/api/clientes/{cid}").json
    corpo = {**{k: atual[k] for k in ("codigo", "razao_social", "nome_fantasia", "cnpj", "cidade", "bairro")},
             "vendedor": "Carla", "perfil": {"tipo_cadastro": "Revenda", "regiao1": "Zona Leste", "regiao2": "Penha"}}
    r = client.put(f"/api/clientes/{cid}", json=corpo)
    assert r.status_code == 200, r.json
    assert (r.json["tipo_cadastro"], r.json["regiao1"], r.json["regiao2"], r.json["vendedor"]) == \
        ("Revenda", "Zona Leste", "Penha", "Carla")
    assert set(r.json["perfil_manual"]) == {"tipo_cadastro", "regiao1", "regiao2"}
    assert client.put(f"/api/clientes/{cid}", json={**corpo, "perfil": {"tipo_cadastro": "Qualquer"}}).status_code == 400

    # reimportação: a planilha não tem informação nova para esses campos nem coluna de vendedor → nada muda
    importar(client)
    d = client.get(f"/api/clientes/{cid}").json
    assert (d["tipo_cadastro"], d["regiao1"], d["regiao2"], d["vendedor"]) == ("Revenda", "Zona Leste", "Penha", "Carla")
    # o nome passou a ter (INATIVO): informação nova substitui o tipo manual; a região manual continua
    etapa(client, "clientes", CAB_CLIENTES, linhas_clientes({6: {"Razão Social": "(INATIVO) DIOGO DA SILVA ALMEIDA COSTA"}}))
    d = client.get(f"/api/clientes/{cid}").json
    assert (d["tipo_cadastro"], d["regiao1"]) == ("Ex-cliente", "Zona Leste")


def test_vendedor_da_planilha_e_em_lote(client):
    cab = CAB_CLIENTES + ["Vendedor"]
    linhas = [l + (["Marcos"] if i == 0 else [""]) for i, l in enumerate(linhas_clientes())]
    etapa(client, "clientes", cab, linhas, nome="clientes-vendedor.xls", salvar_padrao=False)
    c = por_doc(client)
    assert c[cnpj(1)]["vendedor"] == "Marcos" and c[cnpj(2)]["vendedor"] is None

    sem_filtro = client.post("/api/clientes/vendedor-em-lote", json={"filtros": {}, "vendedor": "Ana"})
    assert sem_filtro.status_code == 400
    r = client.post("/api/clientes/vendedor-em-lote", json={"filtros": {"regiao2": "Penha"}, "vendedor": "Ana"})
    assert r.status_code == 200 and r.json["alterados"] == 3
    assert {x["vendedor"] for x in lista(client, vendedor="Ana")["clientes"]} == {"Ana"}
    assert len(lista(client, vendedor="ana")["clientes"]) == 3


def test_mapeamento_de_regioes_configuravel(client):
    importar(client)
    atual = client.get("/api/configuracoes/regioes").json
    assert "São Paulo; Penha; Zona Leste" in atual["texto"]
    r = client.put("/api/configuracoes/regioes", json={"texto": atual["texto"] + "\nGuarulhos; ; Grande SP"})
    assert r.status_code == 200, r.json
    assert por_doc(client)[cnpj(4)]["regiao1"] == "Grande SP"
    assert client.put("/api/configuracoes/regioes", json={"texto": "Guarulhos"}).status_code == 400
    assert client.post("/api/perfil/reclassificar").json["processados"] == 9


def test_diagnostico_e_pendentes(client):
    importar(client)
    d = client.get("/api/perfil/diagnostico").json
    campos = {c["campo"]: (c["identificados"], c["pendentes"]) for c in d["campos"]}
    assert d["total"] == 9 and d["nunca_classificados"] == 0
    assert campos["tipo_cadastro"] == (9, 0)
    assert "segmento_nome" not in campos
    assert campos["regiao1"] == (7, 2)                  # Guarulhos e Vila Nhocune sem regra
    assert campos["regiao2"] == (9, 0)
    assert campos["contabilidade"] == (4, 5)            # 3, 4, 6, 7 e 9 sem escritório; 5 é o próprio escritório
    assert d["situacao_contratual"] == {"Contratado": 3, "Cliente sem contrato": 6}
    assert d["tipos"]["Ex-cliente"] == 3 and "Lead" not in d["tipos"]
    csv = client.get("/api/perfil/pendentes.csv")
    texto = csv.data.decode("utf-8-sig")
    assert csv.status_code == 200 and "Pendente de identificação" in texto and "Região 1" in texto


def test_clientes_existentes_nunca_classificados_aparecem_no_meta(client, consulta):
    importar(client)
    consulta("UPDATE clientes SET perfil_auto = '{}'::jsonb, tipo_cadastro = NULL")
    assert client.get("/api/meta").json["perfil"]["nunca_classificados"] == 9
    client.post("/api/perfil/reclassificar")
    assert client.get("/api/meta").json["perfil"]["nunca_classificados"] == 0


def test_reimportacao_sem_alteracoes_preenche_clientes_antigos(client, consulta):
    """Situação real: clientes importados antes dos campos novos (colunas vazias) e reimportação idêntica."""
    importar(client)
    consulta("UPDATE clientes SET perfil_auto = '{}'::jsonb, tipo_cadastro = NULL, escritorio_contabil = NULL, "
             "regiao1 = NULL, regiao2 = NULL")
    _, _, feito = etapa(client, "clientes", CAB_CLIENTES, linhas_clientes())
    assert feito["clientes_novos"] == 0 and feito["clientes_atualizados"] == 0      # nada mudou no cadastro...
    assert feito["perfil"]["diagnostico"]["nunca_classificados"] == 0               # ...mas o perfil foi preenchido
    c = por_doc(client)[cnpj(1)]
    assert (c["escritorio_contabil"], c["regiao1"], c["regiao2"]) == ("CONTABILIDADE DETECTA", "Zona Leste", "Penha")
    assert len(lista(client, segmento="ADEGA", regiao1="Zona Leste")["clientes"]) == 1
