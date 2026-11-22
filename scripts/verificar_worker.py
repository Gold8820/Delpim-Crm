"""Verificação ponta a ponta de um Worker em execução (wrangler dev local ou o endereço publicado).

Usa SOMENTE dados fictícios, com códigos prefixados por ZZTESTE-, e remove tudo o que criou ao final
(clientes e, por cascata, contratos, valores e pagamentos). Não altera a Curva ABC nem mapeamentos salvos,
a não ser com --completo (que restaura os valores originais da Curva ao final). Use --completo apenas em
banco de TESTE.

    python scripts/verificar_worker.py http://127.0.0.1:8787
    python scripts/verificar_worker.py https://sistema-clientes.<conta>.workers.dev --exige-bloqueio

Login: Supabase Auth. Informe um usuário AUTORIZADO nas variáveis VERIFICAR_EMAIL e VERIFICAR_SENHA
(sem elas, só são conferidos o bloqueio sem login e o /healthz). A importação usa o mesmo caminho do
navegador (linhas em lotes).

Saída: uma linha por verificação (OK/FALHOU) e o tempo de cada chamada.
"""
from __future__ import annotations

import argparse
import http.cookiejar
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


class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Cliente:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()     # sessão (cookies HttpOnly do login)
        self.abrir = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies),
                                                 _SemRedirecionar()).open
        self.sem_sessao = urllib.request.build_opener(_SemRedirecionar()).open
        self.tempos = []

    def chamar(self, metodo, caminho, corpo=None, auth=True, tipo=None, bruto=False):
        headers = {"User-Agent": "sistema-clientes-verificacao/1.0"}   # sem isso o Cloudflare responde 1010
        data = None
        if corpo is not None and tipo is None:
            data = json.dumps(corpo).encode()
            headers["Content-Type"] = "application/json"
        elif corpo is not None:
            data, headers["Content-Type"] = corpo, tipo
        req = urllib.request.Request(self.base + caminho, data=data, method=metodo, headers=headers)
        inicio = time.perf_counter()
        try:
            with (self.abrir if auth else self.sem_sessao)(req, timeout=120) as res:
                status, conteudo, ctype = res.status, res.read(), res.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            status, conteudo, ctype = exc.code, exc.read(), exc.headers.get("Content-Type", "")
        ms = (time.perf_counter() - inicio) * 1000
        self.tempos.append((f"{metodo} {caminho.split('?')[0]}", status, ms))
        if bruto or "json" not in ctype:
            return status, conteudo
        return status, json.loads(conteudo or b"null")

    def enviar(self, linhas: list, nome: str, papel: str, tamanho: int = 2):
        """Como o navegador: linhas da planilha em lotes para /api/importacao/lote."""
        numeradas = [[i + 1, linha] for i, linha in enumerate(linhas)]
        lotes = [numeradas[i:i + tamanho] for i in range(0, len(numeradas), tamanho)]
        token, status, resposta = None, None, None
        for n, lote in enumerate(lotes):
            corpo = {"aba": 0, "linhas": lote, "final": n == len(lotes) - 1}
            if token:
                corpo["token"] = token
            else:
                corpo.update(arquivo=nome, planilhas=["Planilha1"])
            if corpo["final"]:
                corpo["papel"] = papel
            status, resposta = self.chamar("POST", "/api/importacao/lote", corpo)
            if status != 200:
                return status, resposta
            token = resposta["token"]
        return status, resposta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--completo", action="store_true", help="também altera e restaura a Curva ABC (só em banco de teste)")
    ap.add_argument("--exige-bloqueio", action="store_true", help="confere que, sem login, páginas e APIs são bloqueadas")
    args = ap.parse_args()
    c = Cliente(args.url)
    falhas = []

    def ok(nome, condicao, detalhe=""):
        print(("OK      " if condicao else "FALHOU  ") + nome + (f"  [{detalhe}]" if detalhe and not condicao else ""))
        if not condicao:
            falhas.append(nome)

    sufixo = uuid.uuid4().hex[:6].upper()
    criados = []
    try:
        if args.exige_bloqueio:
            for rota, esperado in (("/", 302), ("/api/clientes", 401), ("/api/meta", 401)):
                s, _ = c.chamar("GET", rota, auth=False, bruto=True)
                ok(f"sem login, {rota} é bloqueado ({esperado})", s == esperado, s)
            s, pagina = c.chamar("GET", "/login", auth=False, bruto=True)
            ok("tela de login abre e não expõe chaves", s == 200 and b"login-form" in pagina
               and b"sb_publishable" not in pagina and b"service_role" not in pagina, s)
            s, r = c.chamar("POST", "/api/auth/login", {"email": "ninguem@exemplo.com", "senha": "errada-123"}, auth=False)
            ok("login com senha errada é recusado", s in (401, 429), f"{s} {r}")
        email, senha = os.environ.get("VERIFICAR_EMAIL"), os.environ.get("VERIFICAR_SENHA")
        if not (email and senha):
            print("AVISO   VERIFICAR_EMAIL/VERIFICAR_SENHA não informados: verificações com login puladas.")
            s, b = c.chamar("GET", "/healthz")
            ok("healthz responde ok (Hyperdrive + Supabase)", s == 200 and b == {"status": "ok"}, f"{s} {b}")
            return 1 if falhas else 0
        s, r = c.chamar("POST", "/api/auth/login", {"email": email, "senha": senha})
        ok("login pelo Supabase Auth (usuário autorizado)", s == 200, f"{s} {r}")
        s, pagina = c.chamar("GET", "/", bruto=True)
        ok("depois do login, a tela de clientes abre", s == 200 and b"planilhas.js" in pagina, s)
        s, r = c.chamar("GET", "/api/auth/sessao")
        ok("sessão mostra o usuário", s == 200 and (r or {}).get("email") == email.lower(), f"{s} {r}")
        s, b = c.chamar("GET", "/healthz")
        ok("healthz responde ok", s == 200 and b == {"status": "ok"}, f"{s} {b}")
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
            planilha_cli = ([["Código", "Razão Social", "CNPJ", "Cidade", "UF"],
                                 [cod_a, f"Alfa Teste {sufixo}", CNPJS[0], "Curitiba", "PR"],
                                 [cod_b, f"Beta Teste {sufixo}", CNPJS[1], "Recife", "PE"]])
            n1, n2, n3 = (f"{PREFIXO}{sufixo}-{i}" for i in (1, 2, 3))
            planilha_con = ([["Documento Cliente", "Nº Contrato", "Produto", "Valor Mensal", "Valor Parcela Futuro"],
                                 [CNPJS[0], n1, "Plano A", 600, 5000],
                                 ["11.222.333/0001-81", n2, "Plano B", 500, 3000],
                                 [CNPJS[1], n3, "Plano A", 150, 900]])
            s1, up_cli = c.enviar(planilha_cli, "clientes.xlsx", "clientes")
            s2, up_con = c.enviar(planilha_con, "contratos.xlsx", "contratos")
            ok("envio das duas planilhas em lotes (leitura no navegador)", s1 == 200 and s2 == 200, f"{s1} {s2}")
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
        if os.environ.get("VERIFICAR_EMAIL"):
            s, _ = c.chamar("POST", "/api/auth/logout")
            s2, _ = c.chamar("GET", "/api/clientes")
            ok("sair encerra a sessão (API volta a responder 401)", s == 200 and s2 == 401, f"{s} {s2}")

    print("\nTempos (ms, incluindo rede):")
    for nome, status, ms in c.tempos:
        print(f"  {ms:8.0f}  {status}  {nome}")
    print(f"\n{len(falhas)} falha(s)." if falhas else "\nTodas as verificações passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
