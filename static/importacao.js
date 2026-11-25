"use strict";
/* ==========================================================================
   Importação automática: planilha de CLIENTES e depois planilha de CONTRATOS.

   1. O botão "Importar planilha" abre direto a janela de arquivos do sistema (seleção múltipla).
   2. O navegador lê cada arquivo e o servidor identifica, pelos cabeçalhos, qual é a planilha de clientes
      e qual é a de contratos (/api/importacao/identificar). Se algum arquivo não puder ser identificado com
      segurança, só uma mensagem objetiva é mostrada e nada é importado.
   3. Etapa 1 (clientes) e etapa 2 (contratos): envio em lotes, reconhecimento automático das colunas,
      análise e gravação, cada uma em transação própria. Um erro em uma não impede a outra.
   4. Resultado de cada etapa, resumo geral e CSV de inconsistências.

   Sem tela intermediária e sem associação manual de colunas: vale o mapeamento padrão salvo (ou o
   reconhecimento automático, salvo como padrão na primeira importação). Nenhum dado comercial é
   guardado no navegador (nada de localStorage).
   ========================================================================== */
(function () {
  const ETAPAS = [
    { papel: "clientes", titulo: "Planilha de clientes" },
    { papel: "contratos", titulo: "Planilha de contratos" },
  ];
  const ACEITOS = [".xlsx", ".xls", ".csv"];
  const LINHAS_PARA_IDENTIFICAR = 200;
  const auto = { mes: null, fase: "inicio", problemas: [], etapas: {} };

  const post = (url, json) => api(url, { method: "POST", json });
  const extensao = (nome) => (nome.match(/\.[^.]+$/) || [""])[0].toLowerCase();

  /* ---------------------------------------------------------------- 1. janela de arquivos */
  function seletor() {
    let inp = $("#auto-arquivos");
    if (!inp) {
      inp = Object.assign(document.createElement("input"), { type: "file", id: "auto-arquivos", multiple: true,
        accept: ACEITOS.join(","), hidden: true });
      inp.addEventListener("change", () => {
        const arquivos = [...inp.files];
        inp.value = "";   // permite escolher os mesmos arquivos de novo
        if (arquivos.length) iniciar(arquivos);
      });
      document.body.appendChild(inp);
    }
    return inp;
  }

  // Chamado no clique do botão (o navegador só abre a janela de arquivos dentro de um clique).
  function abrir() {
    seletor().click();
  }

  /* ---------------------------------------------------------------- 2. identificação */
  async function iniciar(arquivos) {
    Object.assign(auto, { mes: state.meta.mes_atual, fase: "identificando", problemas: [], etapas: {} });
    desenhar();
    if (!$("#dlg-auto").open) $("#dlg-auto").showModal();

    const lidos = [];
    for (const arquivo of arquivos) {
      if (!ACEITOS.includes(extensao(arquivo.name))) {
        auto.problemas.push(`“${arquivo.name}”: formato não aceito (use .xlsx, .xls ou .csv).`);
        continue;
      }
      try {
        lidos.push({ arquivo, lido: await Planilhas.ler(arquivo) });
      } catch (err) {
        auto.problemas.push(`“${arquivo.name}”: ${err.message || "não foi possível ler o arquivo."}`);
      }
    }
    let identificados = [];
    if (lidos.length) {
      try {
        const r = await post("/api/importacao/identificar", { arquivos: lidos.map(({ arquivo, lido }) =>
          ({ nome: arquivo.name, linhas: (lido.abas[0] || []).slice(0, LINHAS_PARA_IDENTIFICAR) })) });
        identificados = r.arquivos.map((a, i) => ({ ...lidos[i], papel: a.papel, motivo: a.motivo }));
      } catch (err) {
        auto.problemas.push(err.message);
      }
    }
    for (const a of identificados.filter((x) => !x.papel)) {
      auto.problemas.push(`Não foi possível identificar “${a.arquivo.name}”: ${a.motivo}`);
    }
    for (const e of ETAPAS) {
      const mesmos = identificados.filter((x) => x.papel === e.papel);
      if (mesmos.length > 1) {
        auto.problemas.push(`Foram selecionadas ${mesmos.length} planilhas de ${e.papel} (${mesmos.map((x) => `“${x.arquivo.name}”`).join(", ")}). Selecione só uma de cada.`);
      }
    }
    if (auto.problemas.length) { auto.fase = "problema"; desenhar(); return; }

    // 3. etapas, uma depois da outra: clientes e depois contratos
    auto.fase = "importando";
    for (const e of ETAPAS) {
      const achado = identificados.find((x) => x.papel === e.papel);
      auto.etapas[e.papel] = achado ? { estado: "aguardando", texto: "", arquivo: achado.arquivo.name } : { estado: "pulada" };
    }
    desenhar();
    for (const e of ETAPAS) {
      const achado = identificados.find((x) => x.papel === e.papel);
      if (achado) await etapa(e.papel, achado.arquivo, achado.lido);
    }
    auto.fase = "fim";
    desenhar();
    try { await refreshAll(); } catch { /* a lista atualiza na próxima abertura */ }
  }

  /* ---------------------------------------------------------------- 3. execução de uma etapa */
  function andamento(papel, texto) {
    Object.assign(auto.etapas[papel], { estado: "rodando", texto });
    desenhar();
  }

  const OBRIGATORIOS = {
    clientes: [[["cnpj", "codigo"], "Documento (CPF/CNPJ) ou código do cliente"], [["razao_social", "nome_fantasia"], "Razão social ou nome fantasia"]],
    contratos: [[["numero_contrato"], "Nº (número do contrato)"], [["chave_cliente"], "Documento Cliente"]],
  };

  async function etapa(papel, arquivo, lido) {
    const info = auto.etapas[papel];
    info.inconsistencias = [];
    try {
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
      if (feito.perfil) auto.perfil = feito.perfil;   // classificação da carteira após esta etapa (a última vale)
    } catch (err) {
      Object.assign(info, { estado: "erro", texto: err.message || "Erro inesperado." });
    }
    desenhar();
  }

  /* ---------------------------------------------------------------- 4. tela de andamento e resultados */
  const n = (v) => (v || 0).toLocaleString("pt-BR");
  const cartao = (rotulo, valor, cls = "") => `<div class="stat ${cls}"><b>${n(valor)}</b><span>${rotulo}</span></div>`;
  const contar = (lista, tipo) => lista.filter((i) => i.tipo === tipo).length;

  function blocoEtapa(e) {
    const info = auto.etapas[e.papel];
    if (!info || info.estado === "pulada") return `<section class="auto-etapa"><h3>${e.titulo}</h3><p class="muted">Nenhum arquivo de ${e.papel} foi selecionado.</p></section>`;
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
      ${r.pagamentos_gravados != null ? `<p class="muted">Valores de ${esc(monthLabel(auto.mes))} gravados para ${n(r.pagamentos_gravados)} cliente(s), reclassificados pela Curva ABC.</p>` : ""}</section>`;
  }

  function todasInconsistencias() {
    const lista = ETAPAS.flatMap((e) => (auto.etapas[e.papel]?.inconsistencias || []).map((i) => ({ ...i, arquivo: auto.etapas[e.papel].arquivo })));
    // perfil comercial: identificação duvidosa (o campo ficou sem classificação)
    for (const d of auto.perfil?.duvidas || []) {
      lista.push({ arquivo: auto.etapas.clientes?.arquivo || "", planilha: "clientes", linha: "", tipo_rotulo: "Classificação a revisar",
        identificacao: d.documento || d.codigo, nome: d.nome, motivo: `${d.campo}: ${d.motivo}`, conteudo: "" });
    }
    return lista;
  }

  function resumoPerfil() {
    const p = auto.perfil;
    if (!p) return "";
    const tipos = Object.entries(p.por_tipo || {}).sort((a, b) => b[1] - a[1])
      .map(([t, q]) => `<span class="pill">${esc(t)}: <b>${n(q)}</b></span>`).join(" ");
    return `<p class="muted">Perfil comercial da carteira (tipo, contabilidade, segmento e regiões) atualizado.
      ${p.duvidas?.length ? `${n(p.duvidas.length)} identificação(ões) duvidosa(s) no relatório.` : ""}</p><div class="pills">${tipos}</div>`;
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
      ${resumoPerfil()}
      ${inc ? `<button type="button" class="btn btn-sm" id="auto-csv">Baixar inconsistências (CSV para Excel)</button>` : `<p class="muted">Nenhuma inconsistência encontrada.</p>`}
      ${ok.some((i) => i.truncado) ? `<p class="muted">A lista de inconsistências mostra até 1.000 itens por planilha.</p>` : ""}</section>`;
  }

  function desenhar() {
    const corpo = $("#auto-body"), pe = $("#auto-foot");
    if (auto.fase === "identificando") {
      corpo.innerHTML = `<p><span class="spinner"></span> Lendo e identificando os arquivos…</p>`;
      pe.innerHTML = `<span class="muted">Aguarde.</span>`;
      return;
    }
    if (auto.fase === "problema") {
      corpo.innerHTML = `<div class="notice">${auto.problemas.map((p) => `<div>${esc(p)}</div>`).join("")}</div>
        <p class="muted">Nada foi importado. Selecione uma planilha de clientes e/ou uma de contratos.</p>`;
      pe.innerHTML = `<button type="button" class="btn" id="auto-fechar">Fechar</button><span class="spacer"></span>
        <button type="button" class="btn btn-primary" id="auto-nova">Selecionar arquivos</button>`;
      $("#auto-fechar").onclick = () => $("#dlg-auto").close();
      $("#auto-nova").onclick = abrir;
      return;
    }
    corpo.innerHTML = `<p class="muted auto-mes">Valores dos contratos no mês de referência: <b>${esc(monthLabel(auto.mes))}</b>.</p>`
      + ETAPAS.map(blocoEtapa).join("") + (auto.fase === "fim" ? resumoGeral() : "");
    if (auto.fase !== "fim") {
      pe.innerHTML = `<span class="muted">Importando… não feche esta janela.</span>`;
      return;
    }
    pe.innerHTML = `<button type="button" class="link" id="auto-avancado">Importação avançada</button><span class="spacer"></span>
      <button type="button" class="btn" id="auto-nova">Importar outros arquivos</button>
      <button type="button" class="btn btn-primary" id="auto-fechar">Concluir</button>`;
    $("#auto-fechar").onclick = () => $("#dlg-auto").close();
    $("#auto-nova").onclick = abrir;
    $("#auto-avancado").onclick = () => { $("#dlg-auto").close(); openImport(); };
    const csv = $("#auto-csv");
    if (csv) csv.onclick = baixarCsv;
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
