"use strict";
/* ==========================================================================
   Importação automática: planilha de CLIENTES e depois planilha de CONTRATOS.

   1. O usuário escolhe os dois arquivos (ou só um) e clica em IMPORTAR.
   2. Etapa 1 (clientes): lê no navegador, envia em lotes, reconhece as colunas, analisa e grava.
   3. Etapa 2 (contratos): o mesmo, relacionando cada contrato ao cliente pelo documento (CPF/CNPJ).
   4. Mostra o resultado de cada etapa e o resumo geral; as inconsistências podem ser baixadas em CSV.

   As etapas são independentes: cada uma é gravada em uma transação própria no servidor, e um erro em uma
   não impede a outra. Não há associação manual de colunas: vale o mapeamento padrão salvo (ou o
   reconhecimento automático, que é salvo como padrão na primeira importação). Nenhum dado comercial é
   guardado no navegador (nada de localStorage).
   ========================================================================== */
(function () {
  const ETAPAS = [
    { papel: "clientes", titulo: "Planilha de clientes", desc: "Cadastro (Pessoa, Documento, Razão Social, endereço, contatos…)." },
    { papel: "contratos", titulo: "Planilha de contratos", desc: "Contratos (Nº, Documento Cliente, Valor Atual, Observação…)." },
  ];
  const auto = { arquivos: { clientes: null, contratos: null }, mes: null, rodando: false, etapas: {}, fim: false };

  const post = (url, json) => api(url, { method: "POST", json });

  function abrir() {
    Object.assign(auto, { arquivos: { clientes: null, contratos: null }, rodando: false, etapas: {}, fim: false,
                          mes: state.meta.mes_atual });
    desenhar();
    $("#dlg-auto").showModal();
  }

  /* ---------------------------------------------------------------- tela de seleção */
  function cartaoArquivo(e) {
    const f = auto.arquivos[e.papel];
    return `<div class="drop-card" data-auto-drop="${e.papel}">
      <h3>${e.titulo}</h3>
      ${f ? `<div class="file-chip">📄 ${esc(f.name)}</div>
             <div class="drop-actions"><button type="button" class="btn btn-sm" data-auto-pick="${e.papel}">Trocar</button>
             <button type="button" class="btn btn-sm" data-auto-remove="${e.papel}">Remover</button></div>`
          : `<div class="dropzone"><strong>Arraste o arquivo para cá</strong>
             <span class="muted">Excel (.xlsx), Excel 2003 XML (.xls) ou CSV</span><br>
             <button type="button" class="btn btn-primary" data-auto-pick="${e.papel}">Selecionar planilha de ${e.papel.toUpperCase()}</button></div>
             <p class="help">${esc(e.desc)}</p>`}
      <input type="file" accept=".xlsx,.xls,.csv" data-auto-file="${e.papel}" hidden>
    </div>`;
  }

  function desenhar() {
    const corpo = $("#auto-body"), pe = $("#auto-foot");
    if (auto.rodando || auto.fim) { desenharProgresso(); return; }
    const algum = auto.arquivos.clientes || auto.arquivos.contratos;
    corpo.innerHTML = `
      <p style="margin:0 0 14px">Selecione as duas planilhas e clique em <b>Importar</b>. Primeiro os clientes são gravados;
        depois, os contratos são relacionados a eles pelo documento (CPF/CNPJ). As colunas são reconhecidas automaticamente.</p>
      <div class="drops">${ETAPAS.map(cartaoArquivo).join("")}</div>
      <div class="options"><div class="row"><label for="auto-mes">Mês de referência dos valores dos contratos</label>
        <select id="auto-mes">${monthOptions(auto.mes)}</select>
        <span class="muted">O valor do mês substitui o anterior do mesmo mês (nunca soma).</span></div></div>
      <p class="help"><button type="button" class="link" id="auto-avancado">Importação avançada (revisar a associação de colunas)</button></p>`;
    pe.innerHTML = `<button type="button" class="btn" data-close>Cancelar</button><span class="spacer"></span>
      <button type="button" class="btn btn-primary" id="auto-importar" ${algum ? "" : "disabled"}>Importar</button>`;
    pe.querySelector("[data-close]").onclick = () => $("#dlg-auto").close();
    $("#auto-importar").onclick = importar;
    $("#auto-mes").value = auto.mes;   // monthOptions não marca a opção escolhida
    $("#auto-mes").onchange = (ev) => { auto.mes = ev.target.value; };
    $("#auto-avancado").onclick = () => { $("#dlg-auto").close(); openImport(); };
    corpo.onclick = (ev) => {
      const pick = ev.target.closest("[data-auto-pick]"), rm = ev.target.closest("[data-auto-remove]");
      if (pick) $(`input[data-auto-file="${pick.dataset.autoPick}"]`).click();
      if (rm) { auto.arquivos[rm.dataset.autoRemove] = null; desenhar(); }
    };
    corpo.onchange = (ev) => {
      const inp = ev.target.closest("[data-auto-file]");
      if (inp && inp.files[0]) { auto.arquivos[inp.dataset.autoFile] = inp.files[0]; desenhar(); }
    };
    $$("[data-auto-drop]", corpo).forEach((z) => {
      ["dragenter", "dragover"].forEach((t) => z.addEventListener(t, (e) => { e.preventDefault(); z.classList.add("over"); }));
      ["dragleave", "drop"].forEach((t) => z.addEventListener(t, (e) => { e.preventDefault(); z.classList.remove("over"); }));
      z.addEventListener("drop", (e) => {
        if (e.dataTransfer.files[0]) { auto.arquivos[z.dataset.autoDrop] = e.dataTransfer.files[0]; desenhar(); }
      });
    });
  }

  /* ---------------------------------------------------------------- execução das etapas */
  async function importar() {
    auto.rodando = true;
    for (const e of ETAPAS) auto.etapas[e.papel] = { estado: auto.arquivos[e.papel] ? "aguardando" : "pulada", texto: "" };
    desenhar();
    for (const e of ETAPAS) {
      if (auto.arquivos[e.papel]) await etapa(e.papel, auto.arquivos[e.papel]);   // uma depois da outra
    }
    auto.rodando = false;
    auto.fim = true;
    desenhar();
    try { await refreshAll(); } catch { /* a lista atualiza na próxima abertura */ }
  }

  function andamento(papel, texto) {
    Object.assign(auto.etapas[papel], { estado: "rodando", texto });
    desenhar();
  }

  const OBRIGATORIOS = {
    clientes: [[["cnpj", "codigo"], "Documento (CPF/CNPJ) ou código do cliente"], [["razao_social", "nome_fantasia"], "Razão social ou nome fantasia"]],
    contratos: [[["numero_contrato"], "Nº (número do contrato)"], [["chave_cliente"], "Documento Cliente"]],
  };

  async function etapa(papel, arquivo) {
    const info = auto.etapas[papel];
    info.inconsistencias = [];
    try {
      andamento(papel, "Lendo a planilha…");
      const lido = await Planilhas.ler(arquivo);
      andamento(papel, "Enviando os dados…");
      const up = await Planilhas.enviar(post, lido, { papel },
        (feitos, total) => total > 1 && andamento(papel, `Enviando os dados… ${Math.round((100 * feitos) / total)}%`));

      // Colunas: reconhecidas automaticamente (ou pelo padrão salvo). Só o que é desconhecido vai para o relatório.
      const cab = up.linha_cabecalho || "cabeçalho";
      for (const c of up.colunas_desconhecidas || []) {
        info.inconsistencias.push({ planilha: papel, linha: cab, tipo_rotulo: "Coluna não reconhecida", identificacao: c.nome,
          nome: "", motivo: `A coluna “${c.nome}” não corresponde a nenhum campo do sistema e foi ignorada.`, conteudo: "" });
      }
      for (const [campo, p] of Object.entries(up.pendencias || {})) {
        const usada = p.sugestao != null ? up.colunas[p.sugestao].nome : null;
        info.inconsistencias.push({ planilha: papel, linha: cab, tipo_rotulo: "Coluna do padrão não encontrada", identificacao: p.coluna,
          nome: campo, motivo: usada ? `${p.motivo} Foi usada a coluna “${usada}”.` : `${p.motivo} O campo ficou sem dados nesta importação.`,
          conteudo: "" });
      }
      const faltando = OBRIGATORIOS[papel].filter(([campos]) => campos.every((k) => up.mapeamento[k] == null)).map(([, rotulo]) => rotulo);
      if (faltando.length) {
        throw new Error(`Não foi possível reconhecer: ${faltando.join("; ")}. Confira a planilha ou ajuste o mapeamento em Configurações de importação.`);
      }

      const corpo = { [papel]: { token: up.token, planilha: up.planilha, mapeamento: up.mapeamento },
        mes_padrao: auto.mes, substituir_vazios: false, [`arquivo_${papel}`]: arquivo.name };
      if (papel === "contratos") corpo.chave = up.chave || "cnpj";

      andamento(papel, "Conferindo os registros…");
      const previa = await post("/api/importacao/previa", corpo);
      andamento(papel, "Gravando…");
      const feito = await post("/api/importacao/confirmar", corpo);

      if (!up.configuracao) {   // primeira importação: guarda o reconhecimento automático como padrão
        try {
          await post("/api/importacao/configuracoes/do-arquivo", { papel, token: up.token, planilha: up.planilha,
            mapeamento: up.mapeamento, chave: papel === "contratos" ? (up.chave || "cnpj") : null });
        } catch { /* o padrão pode ser salvo depois, em Configurações */ }
      }
      const linhas = papel === "contratos" ? previa.contratos_linhas || [] : previa.linhas || [];
      Object.assign(info, {
        estado: "ok", texto: "Concluído", resumo: { ...previa.resumo, ...feito },
        zerados: linhas.filter((l) => (l.avisos || []).some((a) => a.startsWith("Valor do mês considerado R$ 0,00"))).length,
        truncado: previa.truncado,
      });
      info.inconsistencias.push(...(previa.inconsistencias || []).filter((i) => i.planilha === papel));
    } catch (err) {
      Object.assign(info, { estado: "erro", texto: err.message || "Erro inesperado." });
    }
    info.arquivo = arquivo.name;
    desenhar();
  }

  /* ---------------------------------------------------------------- resultados */
  const n = (v) => (v || 0).toLocaleString("pt-BR");
  const cartao = (rotulo, valor, cls = "") => `<div class="stat ${cls}"><b>${n(valor)}</b><span>${rotulo}</span></div>`;
  const contar = (lista, tipo) => lista.filter((i) => i.tipo === tipo).length;

  function blocoEtapa(e) {
    const info = auto.etapas[e.papel];
    if (!info || info.estado === "pulada") return `<section class="auto-etapa"><h3>${e.titulo}</h3><p class="muted">Nenhum arquivo selecionado.</p></section>`;
    const icone = { aguardando: "⏳", rodando: "<span class=\"spinner\"></span>", ok: "✓", erro: "✗" }[info.estado];
    const cab = `<h3><span class="auto-icone ${info.estado}">${icone}</span> ${e.titulo}${info.arquivo ? ` <span class="muted">· ${esc(info.arquivo)}</span>` : ""}</h3>`;
    if (info.estado === "aguardando") return `<section class="auto-etapa">${cab}<p class="muted">Aguardando…</p></section>`;
    if (info.estado === "rodando") return `<section class="auto-etapa">${cab}<p>${esc(info.texto)}</p></section>`;
    if (info.estado === "erro") {
      return `<section class="auto-etapa">${cab}<div class="notice">${esc(info.texto)}</div>
        <p class="muted">Nada desta planilha foi gravado. ${e.papel === "clientes" ? "A planilha de contratos é processada mesmo assim." : ""}</p></section>`;
    }
    const r = info.resumo, inc = info.inconsistencias;
    if (e.papel === "clientes") {
      return `<section class="auto-etapa">${cab}<div class="cards">
        ${cartao("Total processado", r.clientes_lidos)}${cartao("Novos cadastros", r.clientes_novos, "new")}
        ${cartao("Atualizados", r.clientes_atualizados, "upd")}${cartao("Sem alterações", r.clientes_sem_alteracao)}
        ${cartao("Erros encontrados", r.linhas_rejeitadas, "rej")}</div></section>`;
    }
    const duplicados = contar(inc, "duplicado"), semCliente = r.contratos_sem_cliente || 0;
    const outros = Math.max(0, (r.contratos_rejeitados || 0) - duplicados - semCliente);
    return `<section class="auto-etapa">${cab}<div class="cards">
      ${cartao("Total processado", r.contratos_lidos)}${cartao("Novos contratos", r.contratos_novos, "new")}
      ${cartao("Atualizados", r.contratos_atualizados, "upd")}${cartao("Sem alterações", r.contratos_sem_alteracao)}
      ${cartao("Relacionados aos clientes", r.contratos_relacionados)}${cartao("Não relacionados", semCliente, "warn")}
      ${cartao("Duplicidades", duplicados, "rej")}${cartao("Erros", outros, "rej")}</div>
      ${info.zerados ? `<p class="muted">${n(info.zerados)} contrato(s) com valor do mês R$ 0,00 conforme a fonte do valor pago (encerrados/em atraso).</p>` : ""}
      ${r.pagamentos_gravados != null ? `<p class="muted">Valores do mês gravados: ${n(r.pagamentos_gravados)} cliente(s), reclassificados pela Curva ABC.</p>` : ""}</section>`;
  }

  function todasInconsistencias() {
    return ETAPAS.flatMap((e) => (auto.etapas[e.papel]?.inconsistencias || []).map((i) => ({ ...i, arquivo: auto.etapas[e.papel].arquivo })));
  }

  function resumoGeral() {
    const est = ETAPAS.map((e) => auto.etapas[e.papel]).filter((i) => i && i.estado !== "pulada");
    const ok = est.filter((i) => i.estado === "ok");
    // Cada etapa conta só os próprios registros (na de contratos, "clientes atualizados" são os valores do mês).
    const cli = auto.etapas.clientes?.estado === "ok" ? auto.etapas.clientes.resumo : {};
    const con = auto.etapas.contratos?.estado === "ok" ? auto.etapas.contratos.resumo : {};
    const processados = (cli.clientes_lidos || 0) + (con.contratos_lidos || 0);
    const gravados = (cli.clientes_novos || 0) + (cli.clientes_atualizados || 0) + (con.contratos_novos || 0) + (con.contratos_atualizados || 0);
    const erros = (cli.linhas_rejeitadas || 0) + (con.contratos_rejeitados || 0);
    const inc = todasInconsistencias().length;
    const falhas = est.filter((i) => i.estado === "erro").length;
    return `<section class="auto-etapa auto-geral"><h3>Resumo geral</h3>
      <p class="summary-line">${n(processados)} registro(s) processado(s) · ${n(gravados)} gravado(s) (novos ou atualizados) ·
        ${n(erros)} com erro · ${n(inc)} inconsistência(s)${falhas ? ` · <b>${falhas} planilha(s) não importada(s)</b>` : ""}.</p>
      ${inc ? `<button type="button" class="btn btn-sm" id="auto-csv">Baixar inconsistências (CSV para Excel)</button>` : `<p class="muted">Nenhuma inconsistência encontrada.</p>`}
      ${ok.some((i) => i.truncado) ? `<p class="muted">A lista de inconsistências mostra até 1.000 itens por planilha.</p>` : ""}</section>`;
  }

  function desenharProgresso() {
    $("#auto-body").innerHTML = ETAPAS.map(blocoEtapa).join("") + (auto.fim ? resumoGeral() : "");
    $("#auto-foot").innerHTML = auto.fim
      ? `<button type="button" class="btn" id="auto-nova">Nova importação</button><span class="spacer"></span>
         <button type="button" class="btn btn-primary" id="auto-fechar">Concluir</button>`
      : `<span class="muted">Importando… não feche esta janela.</span>`;
    if (auto.fim) {
      $("#auto-fechar").onclick = () => $("#dlg-auto").close();
      $("#auto-nova").onclick = abrir;
      const csv = $("#auto-csv");
      if (csv) csv.onclick = baixarCsv;
    }
  }

  /* CSV com ';' e BOM (abre direto no Excel em português): arquivo, planilha, linha e motivo. */
  function baixarCsv() {
    const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const linhas = [["Arquivo", "Planilha", "Linha", "Tipo", "Identificação", "Nome", "Motivo", "Conteúdo da linha"].map(q).join(";")];
    for (const i of todasInconsistencias()) {
      linhas.push([i.arquivo, i.planilha === "contratos" ? "Contratos" : "Clientes", i.linha, i.tipo_rotulo || i.tipo,
        i.identificacao, i.nome, i.motivo, i.conteudo].map(q).join(";"));
    }
    const blob = new Blob(["﻿" + linhas.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "inconsistencias-importacao.csv" });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  /* ---------------------------------------------------------------- configuração: fonte do valor pago */
  async function carregarFonte() {
    const box = $("#fonte-body");
    if (!box) return;
    try {
      desenharFonte(await api("/api/configuracoes/fonte-valor"));
    } catch (err) {
      box.innerHTML = `<div class="notice">${esc(err.message)}</div>`;
    }
  }

  function desenharFonte(f) {
    const box = $("#fonte-body");
    const op = (k, t, d) => `<label class="check"><input type="checkbox" data-fonte="${k}" ${f[k] ? "checked" : ""}>
      <span>${t}<small>${d}</small></span></label>`;
    box.innerHTML = `${op("ativo", "Atualizar os valores do mês pela planilha de contratos",
        "Desmarcado: a importação de contratos grava os contratos, mas não altera valores do mês nem classificações.")}
      ${op("excluir_encerrados", "Contrato encerrado conta R$ 0,00 no mês", "Usa a coluna “Encerrado” (Sim/Não).")}
      ${op("excluir_atrasados", "Contrato com parcelas em atraso conta R$ 0,00 no mês", "Usa a coluna “Atrasado” (quantidade de parcelas em atraso).")}
      <div class="curva-actions"><button type="button" class="btn btn-primary" id="fonte-salvar">Salvar fonte do valor pago</button></div>`;
    $("#fonte-salvar").onclick = async () => {
      const corpo = Object.fromEntries($$("[data-fonte]", box).map((c) => [c.dataset.fonte, c.checked]));
      $("#fonte-salvar").disabled = true;
      try {
        const r = await api("/api/configuracoes/fonte-valor", { method: "PUT", json: corpo });
        toast(r.mensagem);
        desenharFonte(r);
      } catch (err) {
        toast(err.message, "error");
        $("#fonte-salvar").disabled = false;
      }
    };
  }

  window.openAutoImport = abrir;
  window.loadFonteValor = carregarFonte;
})();
