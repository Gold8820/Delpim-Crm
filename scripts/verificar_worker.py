"""Verificação ponta a ponta de um Worker em execução (wrangler dev local ou o endereço publicado).

Usa SOMENTE dados fictícios, com códigos prefixados por ZZTESTE-, e remove tudo o que criou ao final
(clientes e, por cascata, contratos, valores e pagamentos). Não altera a Curva ABC nem mapeamentos salvos,
a não ser com --completo (que restaura os valores originais da Curva ao final). Use --completo apenas em
banco de TESTE.

    python scripts/verificar_worker.py http://127.0.0.1:8787 --usuario admin --senha ...
    (a senha também pode vir da variável ACCESS_PASSWORD)

Saída: uma linha por verificação (OK/FALHOU) e o tempo de cada chamada.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

from openpyxl import Workbook

PREFIXO = "ZZTESTE-"
# CNPJs válidos e fictícios (dígitos verificadores corretos), usados só nesta verificação.
CNPJS = ["11222333000181", "11444777000161"]


def xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class Cliente:
    def __init__(self, base, user, password):
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.tempos = []

    def chamar(self, metodo, caminho, corpo=None, auth=True, tipo=None, bruto=False):
        headers = {"User-Agent": "sistema-clientes-verificacao/1.0"}   # sem isso o Cloudflare responde 1010
        if auth:
            headers["Authorization"] = self.auth
        data = None
        if corpo is not None and tipo is None:
            data = json.dumps(corpo).encode()
            headers["Content-Type"] = "application/json"
        elif corpo is not None:
            data, headers["Content-Type"] = corpo, tipo
        req = urllib.request.Request(self.base + caminho, data=data, method=metodo, headers=headers)
        inicio = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                status, conteudo, ctype = res.status, res.read(), res.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            status, conteudo, ctype = exc.code, exc.read(), exc.headers.get("Content-Type", "")
        ms = (time.perf_counter() - inicio) * 1000
        self.tempos.append((f"{metodo} {caminho.split('?')[0]}", status, ms))
        if bruto or "json" not in ctype:
            return status, conteudo
        return status, json.loads(conteudo or b"null")

    def enviar(self, conteudo: bytes, nome: str, papel: str):
        fronteira = uuid.uuid4().hex
        partes = (f"--{fronteira}\r\nContent-Disposition: form-data; name=\"papel\"\r\n\r\n{papel}\r\n"
                  f"--{fronteira}\r\nContent-Disposition: form-data; name=\"arquivo\"; filename=\"{nome}\"\r\n"
                  f"Content-Type: application/octet-stream\r\n\r\n").encode() + conteudo + f"\r\n--{fronteira}--\r\n".encode()
        return self.chamar("POST", "/api/importacao/arquivo", partes, tipo=f"multipart/form-data; boundary={fronteira}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--usuario", default=os.environ.get("ACCESS_USER", "admin"))
    ap.add_argument("--senha", default=os.environ.get("ACCESS_PASSWORD"))
    ap.add_argument("--completo", action="store_true", help="também altera e restaura a Curva ABC (só em banco de teste)")
    args = ap.parse_args()
    if not args.senha:
        print("Informe a senha (--senha ou variável ACCESS_PASSWORD).")
        return 2

    c = Cliente(args.url, args.usuario, args.senha)
    falhas = []

    def ok(nome, condicao, detalhe=""):
        print(("OK      " if condicao else "FALHOU  ") + nome + (f"  [{detalhe}]" if detalhe and not condicao else ""))
        if not condicao:
            falhas.append(nome)

    sufixo = uuid.uuid4().hex[:6].upper()
    criados = []
    try:
        s, b = c.chamar("GET", "/healthz", auth=False)
        ok("healthz sem senha responde ok", s == 200 and b == {"status": "ok"}, f"{s} {b}")
        s, _ = c.chamar("GET", "/api/clientes", auth=False)
        ok("API sem senha é recusada (401)", s == 401, s)
        s, _ = c.chamar("GET", "/static/app.js", auth=False, bruto=True)
        ok("arquivo estático sem senha é recusado (401)", s == 401, s)
        s, pagina = c.chamar("GET", "/", bruto=True)
        ok("página inicial abre com senha", s == 200 and b"<html" in pagina.lower(), s)
        for arq, marca in (("style.css", b"{"), ("app.js", b"function")):
            s, conteudo = c.chamar("GET", f"/static/{arq}?v=cf", bruto=True)
            ok(f"/static/{arq} carrega pelo Static Assets", s == 200 and marca in conteudo, s)
        s, meta = c.chamar("GET", "/api/meta")
        ok("meta (faixas da Curva ABC) via Hyperdrive", s == 200 and len(meta.get("faixas", [])) == 4, s)

        # Cadastro, edição, consulta
        codigo = f"{PREFIXO}{sufixo}"
        s, cli = c.chamar("POST", "/api/clientes", {"codigo": codigo, "razao_social": f"Teste Worker {sufixo}",
                                                    "valor": "1.050,00", "mes": "11/2026",
                                                    "observacoes": "Observação geral\ncom acentuação"})
        ok("cadastro de cliente", s == 201, f"{s} {cli}")
        if s == 201:
            criados.append(cli["id"])
            ok("classificação pelo valor pago (A+)", cli["pagamentos"][0]["classificacao"] == "A+")
            s, cli2 = c.chamar("PUT", f"/api/clientes/{cli['id']}", {"codigo": codigo, "razao_social": f"Teste Worker {sufixo} Editado",
                                                                     "valor": "200,00", "mes": "11/2026"})
            ok("edição de cliente (reclassifica para B)", s == 200 and cli2["pagamentos"][0]["classificacao"] == "B", s)
            s, lista = c.chamar("GET", f"/api/clientes?q={codigo}")
            ok("consulta/busca de cliente", s == 200 and lista["total"] == 1, s)

        # Importação com duas planilhas (clientes + contratos)
        cod_a, cod_b = f"{PREFIXO}A{sufixo}", f"{PREFIXO}B{sufixo}"
        existentes = {r["cnpj"] for r in c.chamar("GET", "/api/clientes?q=11.222.333&por_pagina=100")[1].get("clientes", [])}
        if any(x in existentes for x in ("11.222.333/0001-81",)):
            print("AVISO   CNPJ fictício já existe no banco; importação pulada para não alterar dados reais.")
        else:
            planilha_cli = xlsx([["Código", "Razão Social", "CNPJ", "Cidade", "UF"],
                                 [cod_a, f"Alfa Teste {sufixo}", CNPJS[0], "Curitiba", "PR"],
                                 [cod_b, f"Beta Teste {sufixo}", CNPJS[1], "Recife", "PE"]])
            n1, n2, n3 = (f"{PREFIXO}{sufixo}-{i}" for i in (1, 2, 3))
            planilha_con = xlsx([["Documento Cliente", "Nº Contrato", "Produto", "Valor Mensal", "Valor Parcela Futuro"],
                                 [CNPJS[0], n1, "Plano A", 600, 5000],
                                 ["11.222.333/0001-81", n2, "Plano B", 500, 3000],
                                 [CNPJS[1], n3, "Plano A", 150, 900]])
            s1, up_cli = c.enviar(planilha_cli, "clientes.xlsx", "clientes")
            s2, up_con = c.enviar(planilha_con, "contratos.xlsx", "contratos")
            ok("upload das duas planilhas (.xlsx)", s1 == 200 and s2 == 200, f"{s1} {s2}")
            if s1 == 200 and s2 == 200:
                mapa_con = dict(up_con["mapeamento"])
                if mapa_con.get("valor_mensal") is None:   # "Valor Mensal" é sugerido; garante a escolha explícita
                    mapa_con["valor_mensal"] = 3
                corpo = {"chave": "cnpj", "mes_padrao": "2026-11", "arquivo_clientes": "clientes.xlsx",
                         "arquivo_contratos": "contratos.xlsx",
                         "clientes": {"token": up_cli["token"], "planilha": up_cli["planilha"], "mapeamento": up_cli["mapeamento"]},
                         "contratos": {"token": up_con["token"], "planilha": up_con["planilha"], "mapeamento": mapa_con}}
                s, prev = c.chamar("POST", "/api/importacao/previa", corpo)
                ok("prévia da importação", s == 200 and prev["resumo"]["contratos_relacionados"] == 3, f"{s} {prev if s != 200 else prev['resumo']}")
                s, fim = c.chamar("POST", "/api/importacao/confirmar", corpo)
                ok("confirmação da importação", s == 200, f"{s} {fim}")
                s, l2 = c.chamar("GET", f"/api/clientes?q={PREFIXO}A{sufixo}")
                alfa = (l2.get("clientes") or [{}])[0]
                if alfa.get("id"):
                    criados.append(alfa["id"])
                s, l3 = c.chamar("GET", f"/api/clientes?q={PREFIXO}B{sufixo}")
                if (l3.get("clientes") or [{}])[0].get("id"):
                    criados.append(l3["clientes"][0]["id"])
                ok("cliente relacionado a 2 contratos, soma 1.100,00 = A+",
                   alfa.get("classificacao") == "A+" and alfa.get("valor_centavos") == 110000, alfa)
                s, prev2 = c.chamar("POST", "/api/importacao/previa", corpo)
                r = prev2.get("resumo", {}) if s == 200 else {}
                ok("reimportação idêntica não cria nada novo",
                   s == 200 and r.get("clientes_novos") == 0 and r.get("contratos_novos", 0) == 0, r)
                if alfa.get("id"):
                    s, ficha = c.chamar("GET", f"/api/clientes/{alfa['id']}")
                    ok("ficha traz os contratos", s == 200 and len(ficha.get("contratos", [])) == 2, s)
                    contrato = ficha["contratos"][0]
                    s, _ = c.chamar("PUT", f"/api/clientes/{alfa['id']}/contratos/{contrato['id']}/observacoes",
                                    {"observacoes": "Observação do contrato"})
                    s2, ficha = c.chamar("GET", f"/api/clientes/{alfa['id']}")
                    ok("observação do contrato independente da do cliente",
                       s == 200 and not ficha.get("observacoes")
                       and any(x["observacoes"] == "Observação do contrato" for x in ficha["contratos"]), s)
                s, csv = c.chamar("POST", "/api/importacao/inconsistencias.csv", corpo, bruto=True)
                ok("CSV de inconsistências", s == 200, s)

        s, modelo = c.chamar("GET", "/api/importacao/modelo.csv?tipo=contratos", bruto=True)
        ok("modelo de planilha (CSV)", s == 200 and len(modelo) > 10, s)
        s, curva = c.chamar("GET", "/api/configuracoes/curva-abc")
        ok("leitura da Curva ABC", s == 200 and [f["classificacao"] for f in curva["faixas"]] == ["A+", "A", "B", "C"], s)
        if args.completo and s == 200:
            original = [{k: f[k] for k in ("classificacao", "minimo", "maximo")} for f in curva["faixas"]]
            nova = [dict(f) for f in original]
            nova[1]["minimo"], nova[2]["maximo"] = "350,00", "349,99"
            s, r = c.chamar("PUT", "/api/configuracoes/curva-abc", {"faixas": nova})
            ok("edição da Curva ABC", s == 200, f"{s} {r}")
            s, r = c.chamar("PUT", "/api/configuracoes/curva-abc", {"faixas": original})
            ok("Curva ABC restaurada", s == 200, s)
    finally:
        for cid in criados:
            s, _ = c.chamar("DELETE", f"/api/clientes/{cid}")
            ok(f"limpeza: cliente de teste {cid} excluído", s == 200, s)

    print("\nTempos (ms, incluindo rede):")
    for nome, status, ms in c.tempos:
        print(f"  {ms:8.0f}  {status}  {nome}")
    print(f"\n{len(falhas)} falha(s)." if falhas else "\nTodas as verificações passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
