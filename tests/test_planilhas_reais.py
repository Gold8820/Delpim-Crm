"""Validação com as planilhas REAIS (fora do repositório), no banco de TESTE.

Procura ../clientes.xls e ../contratos.xls ao lado da pasta do projeto (ou PLANILHAS_REAIS=<pasta>).
Sem os arquivos, os testes são pulados. Os dados reais nunca entram no repositório: são lidos pelo
mesmo código do navegador (static/planilhas.js, no Node), importados num schema temporário do banco de
teste (apagado ao final) e só contagens são exibidas.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PASTA = Path(os.environ.get("PLANILHAS_REAIS", ROOT.parent))
CLIENTES, CONTRATOS = PASTA / "clientes.xls", PASTA / "contratos.xls"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not (CLIENTES.is_file() and CONTRATOS.is_file() and NODE),
                                reason="planilhas reais (ou Node) não disponíveis nesta máquina")

RUNNER = r"""
const P = require(process.argv[1]); const fs = require("fs");
(async () => {
  const bytes = new Uint8Array(fs.readFileSync(process.argv[2]));
  const lido = await P.ler({ name: process.argv[3], size: bytes.length, bytes });
  process.stdout.write(JSON.stringify(lido));
})().catch((e) => { process.stderr.write(String(e)); process.exit(1); });
"""


def ler(caminho: Path) -> dict:
    res = subprocess.run([NODE, "-e", RUNNER, str(ROOT / "static" / "planilhas.js"), str(caminho), caminho.name],
                         capture_output=True, timeout=120)
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    return json.loads(res.stdout.decode("utf-8"))


def enviar(client, lido, papel, lote=1500):
    linhas = lido["abas"][0]
    token, res = None, None
    for i in range(0, len(linhas), lote):
        corpo = {"aba": 0, "linhas": linhas[i:i + lote], "final": i + lote >= len(linhas)}
        if token:
            corpo["token"] = token
        else:
            corpo.update(arquivo=lido["arquivo"], planilhas=lido["planilhas"])
        if corpo["final"]:
            corpo["papel"] = papel
        res = client.post("/api/importacao/lote", json=corpo)
        assert res.status_code == 200, res.json
        token = res.json["token"]
    return res.json


def etapa(client, lido, papel):
    up = enviar(client, lido, papel)
    corpo = {papel: {"token": up["token"], "planilha": up["planilha"], "mapeamento": up["mapeamento"]},
             "mes_padrao": "2026-11", f"arquivo_{papel}": lido["arquivo"]}
    if papel == "contratos":
        corpo["chave"] = up["chave"]
    previa = client.post("/api/importacao/previa", json=corpo).json
    feito = client.post("/api/importacao/confirmar", json=corpo)
    assert feito.status_code == 200, feito.json
    return up, previa, feito.json


def test_planilhas_reais_identificadas_automaticamente(client):
    """Os dois arquivos reais, selecionados juntos (em qualquer ordem), são identificados pelos cabeçalhos."""
    lidos = [ler(CONTRATOS), ler(CLIENTES)]
    corpo = {"arquivos": [{"nome": l["arquivo"], "linhas": l["abas"][0][:200]} for l in lidos]}
    res = client.post("/api/importacao/identificar", json=corpo).json["arquivos"]
    assert {a["nome"]: a["papel"] for a in res} == {CONTRATOS.name: "contratos", CLIENTES.name: "clientes"}


def test_planilhas_reais_importadas_em_duas_etapas(client, consulta):
    cli, con = ler(CLIENTES), ler(CONTRATOS)
    up_c, prev_c, feito_c = etapa(client, cli, "clientes")
    up_k, prev_k, feito_k = etapa(client, con, "contratos")
    assert up_c["colunas_desconhecidas"] == [] and up_k["colunas_desconhecidas"] == []

    rc, rk = prev_c["resumo"], prev_k["resumo"]
    tipos = {}
    for i in prev_c["inconsistencias"] + prev_k["inconsistencias"]:
        tipos[(i["planilha"], i["tipo"])] = tipos.get((i["planilha"], i["tipo"]), 0) + 1
    print("\nCLIENTES  processados=%d novos=%d atualizados=%d sem_alteracao=%d erros=%d" % (
        rc["clientes_lidos"], feito_c["clientes_novos"], feito_c["clientes_atualizados"],
        feito_c["clientes_sem_alteracao"], rc["linhas_rejeitadas"]))
    print("CONTRATOS processados=%d novos=%d atualizados=%d sem_alteracao=%d relacionados=%d nao_relacionados=%d "
          "rejeitados=%d" % (rk["contratos_lidos"], feito_k["contratos_novos"], feito_k["contratos_atualizados"],
                             feito_k["contratos_sem_alteracao"], rk["contratos_relacionados"],
                             rk["contratos_sem_cliente"], rk["contratos_rejeitados"]))
    print("inconsistências por tipo:", tipos)
    pessoas = consulta("SELECT dados_extras->>'pessoa', COUNT(*) FROM clientes GROUP BY 1 ORDER BY 1")
    print("clientes por tipo de pessoa:", [tuple(p) for p in pessoas])

    assert rc["clientes_lidos"] == len(cli["abas"][0]) - 1 or rc["clientes_lidos"] > 1000
    assert rk["contratos_lidos"] == 514
    assert feito_k["contratos_novos"] + rk["contratos_rejeitados"] == 514
    # relacionamento só por documento: nada relacionado por nome
    assert rk["contratos_relacionados"] == feito_k["contratos_novos"]

    # perfil comercial identificado a partir dos nomes reais e dos contratos relacionados
    perfil = feito_k["perfil"]
    print("perfil por tipo:", perfil["por_tipo"], "| dúvidas:", len(perfil["duvidas"]))
    linhas = [tuple(r) for r in consulta(
        "SELECT razao_social, tipo_cadastro, escritorio_contabil FROM clientes")]

    def ex(prefixo):   # (tipo, escritório) do cliente cujo nome começa assim
        achados = [r[1:] for r in linhas if (r[0] or "").startswith(prefixo)]
        assert len(achados) == 1, (prefixo, len(achados))
        return achados[0]

    assert ex("REVENDA ALESSANDRO")[0] == "Revenda"
    assert ex("ROUPA PHORTION MATRIZ")[1] == "CONTABILIDADE AAS.ASSESSORIA CONTABIL"
    assert ex("MERCADO MINI PRECO")[1] is None      # "SOLICITAR CONTATO" não vira nome de escritório
    assert ex("(INATIVO) HORTIFRUTI ALEXANDRINO LUCIANA")[:2] == ("Ex-cliente", "CONTABILIDADE BATISTA")
    assert perfil["por_tipo"].get("Revenda", 0) >= 4 and len(perfil["duvidas"]) < 30
    assert "Lead" not in perfil["por_tipo"]
    d = perfil["diagnostico"]
    print("DIAGNÓSTICO:", {c["rotulo"]: (c["identificados"], c["pendentes"]) for c in d["campos"]},
          d["situacao_contratual"], d["contabilidade"])
    assert d["nunca_classificados"] == 0 and d["situacao_contratual"]["Contratado"] > 400

    # reimportação idêntica: nada novo, nada duplicado
    antes = [consulta(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("clientes", "contratos", "contrato_valores")]
    _, _, again_c = etapa(client, cli, "clientes")
    _, _, again_k = etapa(client, con, "contratos")
    depois = [consulta(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("clientes", "contratos", "contrato_valores")]
    assert depois == antes
    assert again_c["clientes_novos"] == 0 and again_k["contratos_novos"] == 0
    assert again_c["clientes_atualizados"] == 0 and again_k["contratos_atualizados"] == 0
