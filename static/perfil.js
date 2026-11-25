"use strict";
/* Perfil comercial do cliente: filtros combinados da tela principal, campos na ficha (editáveis),
   vendedor em lote (gestão da carteira) e mapeamento geográfico das regiões (Configurações).
   Usa as funções globais de app.js ($, $$, esc, api, toast, state, loadClients, refreshAll). */
(() => {
  const VAZIO = "__vazio__";
  const FILTROS = [
    ["tipo_cadastro", "Tipo de cadastro"], ["contrato", "Situação contratual"],
    ["escritorio_contabil", "Escritório de contabilidade"], ["segmento_nome", "Segmento"],
    ["regiao1", "Região 1"], ["regiao2", "Região 2"], ["vendedor", "Vendedor responsável"],
  ];
  const CAMPOS = ["tipo_cadastro", "escritorio_contabil", "situacao_contabil", "segmento_nome", "regiao1", "regiao2"];
  const ficha = { original: {}, manual: [] };
  state.pf = {};

  const meta = () => state.meta.perfil || { opcoes: {}, tipos: [], situacoes_contabeis: [] };
  const opt = (v, t) => `<option value="${esc(v)}">${esc(t)}</option>`;

  function opcoes(campo) {
    if (campo === "contrato") return [["sim", "Contratado"], ["nao", "Não contratado"]];
    const lista = campo === "tipo_cadastro" ? meta().tipos : meta().opcoes[campo] || [];
    return [...lista.map((v) => [v, v]), [VAZIO, campo === "vendedor" ? "Sem vendedor" : "Sem classificação"]];
  }

  /* ------------------------------------------------------------ filtros */
  function desenharFiltros() {
    const box = $("#perfil-filtros");
    box.innerHTML = FILTROS.map(([k, rotulo]) => {
      const lista = opcoes(k);
      if (state.pf[k] && !lista.some(([v]) => v === state.pf[k])) delete state.pf[k];
      return `<label class="filter"><span>${esc(rotulo)}</span><select data-pf="${k}">${opt("", "Todos")}${lista.map(([v, t]) => opt(v, t)).join("")}</select></label>`;
    }).join("") + `<div class="perfil-acoes">
        <button type="button" class="btn btn-sm" id="pf-limpar">Limpar filtros</button>
        <button type="button" class="btn btn-sm" id="pf-vendedor" title="Define o vendedor de todos os clientes da lista filtrada">Atribuir vendedor…</button></div>`;
    $$("[data-pf]", box).forEach((s) => { s.value = state.pf[s.dataset.pf] || ""; });
    // listas de sugestões da ficha
    const dl = (id, campo) => { $(id).innerHTML = (meta().opcoes[campo] || []).map((v) => `<option value="${esc(v)}">`).join(""); };
    dl("#dl-pf-escritorio", "escritorio_contabil"); dl("#dl-pf-segmento", "segmento_nome");
    dl("#dl-pf-regiao1", "regiao1"); dl("#dl-pf-regiao2", "regiao2"); dl("#dl-vendedores", "vendedor");
    $("#pf-tipo_cadastro").innerHTML = opt("", "— Sem classificação") + meta().tipos.map((t) => opt(t, t)).join("");
    $("#pf-situacao_contabil").innerHTML = opt("", "—") + meta().situacoes_contabeis.map((t) => opt(t, t)).join("");
  }

  function filtrosAtuais() {
    return { q: state.q, classificacao: state.cls, segmento: state.seg, mes: state.mes, ...state.pf };
  }

  function limparFiltros() {
    Object.assign(state, { q: "", cls: "", seg: "", page: 1, pf: {} });
    $("#search").value = ""; $("#f-class").value = ""; $("#f-segment").value = "";
    $$("[data-pf]").forEach((s) => { s.value = ""; });
    loadClients();
  }

  /* ------------------------------------------------------------ vendedor em lote */
  function abrirVendedor() {
    const ativos = Object.entries(filtrosAtuais()).filter(([k, v]) => v && k !== "mes");
    const dlg = $("#dlg-vendedor");
    $("#vend-erro").hidden = true;
    $("#vend-nome").value = "";
    $("#vend-texto").innerHTML = ativos.length
      ? `O vendedor será definido para os <b>${state.total.toLocaleString("pt-BR")}</b> cliente(s) da lista filtrada.
         Deixe em branco para remover o vendedor desses clientes.`
      : `Aplique ao menos um filtro (por exemplo, Região ou Tipo de cadastro) para escolher os clientes.`;
    $("#vend-aplicar").disabled = !ativos.length;
    dlg.showModal();
    $("#vend-nome").focus();
  }

  async function aplicarVendedor() {
    const btn = $("#vend-aplicar");
    btn.disabled = true;
    try {
      const r = await api("/api/clientes/vendedor-em-lote", { method: "POST", json: { filtros: filtrosAtuais(), vendedor: $("#vend-nome").value } });
      $("#dlg-vendedor").close();
      toast(r.mensagem);
      await refreshAll();
    } catch (err) {
      $("#vend-erro").textContent = err.message;
      $("#vend-erro").hidden = false;
    } finally {
      btn.disabled = false;
    }
  }

  /* ------------------------------------------------------------ ficha do cliente */
  function preencher(c) {
    ficha.original = {};
    ficha.manual = (c && c.perfil_manual) || [];
    for (const k of CAMPOS) {
      const el = $(`#pf-${k}`);
      const v = (c && c[k]) || "";
      if (el.tagName === "SELECT" && v && ![...el.options].some((o) => o.value === v)) el.insertAdjacentHTML("beforeend", opt(v, v));
      el.value = v;
      ficha.original[k] = v;
    }
    $("#pf-contratual").textContent = c ? (c.situacao_contratual || "—") : "Definida pelos contratos";
    dicas();
  }

  function dicas() {
    for (const k of CAMPOS) {
      const el = $(`#pf-${k}`), dica = $(`[data-pf-dica="${k}"]`);
      const mudou = el.value.trim() !== ficha.original[k];
      dica.textContent = mudou ? "será salvo como edição manual" : ficha.manual.includes(k) ? "editado manualmente" : (ficha.original[k] ? "automático" : "");
      dica.className = `dica ${mudou || ficha.manual.includes(k) ? "manual" : ""}`;
    }
  }

  /* Só os campos alterados pelo usuário: o restante continua automático. */
  function edicoes() {
    const out = {};
    for (const k of CAMPOS) {
      const v = $(`#pf-${k}`).value.trim();
      if (v !== ficha.original[k]) out[k] = v;
    }
    return out;
  }

  /* ------------------------------------------------------------ configurações: regiões e reclassificação */
  function resultadoReclassificacao(r) {
    const tipos = Object.entries(r.por_tipo || {}).sort((a, b) => b[1] - a[1])
      .map(([t, q]) => `<span class="pill">${esc(t)}: <b>${q.toLocaleString("pt-BR")}</b></span>`).join(" ");
    const d = r.duvidas || [];
    $("#regioes-resultado").innerHTML = `<p class="muted">${(r.processados || 0).toLocaleString("pt-BR")} cliente(s) analisado(s),
      ${(r.alterados || 0).toLocaleString("pt-BR")} com classificação alterada.</p><div class="pills">${tipos}</div>
      ${d.length ? `<p>${d.length.toLocaleString("pt-BR")} cadastro(s) com identificação duvidosa ficaram sem classificação no campo indicado.
        <button type="button" class="btn btn-sm" id="regioes-csv">Baixar lista (CSV para Excel)</button></p>` : `<p class="muted">Nenhuma identificação duvidosa.</p>`}`;
    const b = $("#regioes-csv");
    if (b) b.onclick = () => baixarDuvidas(d);
  }

  function baixarDuvidas(lista) {
    const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const linhas = [["Código", "Documento", "Cliente", "Campo", "Motivo"].map(q).join(";"),
      ...lista.map((i) => [i.codigo, i.documento, i.nome, i.campo, i.motivo].map(q).join(";"))];
    const blob = new Blob(["﻿" + linhas.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "classificacao-a-revisar.csv" });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  async function carregarRegioes() {
    const box = $("#regioes-body");
    if (!box) return;
    try {
      const r = await api("/api/configuracoes/regioes");
      box.innerHTML = `<label class="field"><span class="muted">Uma linha por regra: <b>Cidade; Bairro; Região 1; Região 2</b>.
          Bairro vazio vale para a cidade toda. Um bairro do cadastro corresponde à regra quando contém o nome dela
          (Parque da Mooca → Mooca). Região 2 vazia = o bairro da regra. ${r.linhas} regra(s).</span>
        <textarea id="regioes-texto" rows="12" spellcheck="false" aria-label="Mapeamento de regiões"></textarea></label>
        <div class="curva-actions"><button type="button" class="btn" id="regioes-reclassificar">Reclassificar carteira agora</button>
          <button type="button" class="btn btn-primary" id="regioes-salvar">Salvar mapeamento</button></div>
        <div id="regioes-resultado"></div>`;
      $("#regioes-texto").value = r.texto;
      $("#regioes-salvar").onclick = () => executar("#regioes-salvar", () =>
        api("/api/configuracoes/regioes", { method: "PUT", json: { texto: $("#regioes-texto").value } }));
      $("#regioes-reclassificar").onclick = () => executar("#regioes-reclassificar", () =>
        api("/api/perfil/reclassificar", { method: "POST", json: {} }));
    } catch (err) {
      box.innerHTML = `<div class="notice">${esc(err.message)}</div>`;
    }
  }

  async function executar(botao, chamada) {
    const b = $(botao);
    b.disabled = true;
    try {
      const r = await chamada();
      if (r.texto != null) $("#regioes-texto").value = r.texto;
      toast(r.mensagem || "Carteira reclassificada.");
      resultadoReclassificacao(r);
      await loadMeta();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      b.disabled = false;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("#perfil-filtros").addEventListener("change", (e) => {
      const s = e.target.closest("[data-pf]");
      if (!s) return;
      if (s.value) state.pf[s.dataset.pf] = s.value; else delete state.pf[s.dataset.pf];
      state.page = 1;
      loadClients();
    });
    $("#perfil-filtros").addEventListener("click", (e) => {
      if (e.target.closest("#pf-limpar")) limparFiltros();
      if (e.target.closest("#pf-vendedor")) abrirVendedor();
    });
    $("#vend-aplicar").addEventListener("click", aplicarVendedor);
    $("#perfil-box").addEventListener("input", dicas);
    $("#perfil-box").addEventListener("change", dicas);
  });

  Object.assign(window, { perfilMeta: desenharFiltros, perfilPreencher: preencher, perfilEdicoes: edicoes,
    limparFiltros, loadRegioes: carregarRegioes });
})();
