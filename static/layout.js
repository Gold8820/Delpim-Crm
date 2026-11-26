"use strict";
/* Estrutura visual: menu lateral (recolhível em telas pequenas), indicadores da carteira, painel de filtros,
   seletor de colunas e ajuste automático da tabela à largura da tela (sem rolagem horizontal).
   Usa as funções globais de app.js ($, $$, esc, api, state, loadClients). Não altera regras nem dados. */
(() => {
  const COLUNAS = [   // ordem da tabela; "fixa" = sempre visível
    { col: "codigo", rotulo: "Código" }, { col: "cliente", rotulo: "Cliente", fixa: true },
    { col: "perfil", rotulo: "Tipo / Região" }, { col: "cnpj", rotulo: "CNPJ" }, { col: "cidade", rotulo: "Cidade" },
    { col: "estado", rotulo: "UF" }, { col: "ramo", rotulo: "Ramo de atividade" }, { col: "vendedor", rotulo: "Vendedor" },
    { col: "valor", rotulo: "Valor no mês" }, { col: "classificacao", rotulo: "Classe (Curva ABC)" },
    { col: "acoes", rotulo: "Ações", fixa: true },
  ];
  // Quando não cabe, estas saem primeiro (da menos para a mais importante).
  const PRIORIDADE = ["estado", "ramo", "codigo", "cnpj", "vendedor", "cidade", "perfil", "classificacao", "valor"];
  const CHAVE = "sc.colunas.v1";          // preferência de exibição deste navegador (não é dado comercial)
  const CARTOES = 760;                    // abaixo disso a tabela vira cartões

  let escolhidas = carregarEscolha();
  let autoOcultas = [];

  function padrao() {
    const largura = window.innerWidth;
    // celular (cartões): só o essencial; telas largas: tudo
    const fora = largura <= CARTOES ? ["codigo", "cnpj", "estado", "ramo", "vendedor"]
      : largura >= 1700 ? [] : ["estado", "ramo"];
    return COLUNAS.filter((c) => !fora.includes(c.col)).map((c) => c.col);
  }

  function carregarEscolha() {
    try {
      const v = JSON.parse(localStorage.getItem(CHAVE) || "null");
      if (Array.isArray(v) && v.length) return v;
    } catch { /* navegador sem armazenamento: usa o padrão */ }
    return null;
  }

  function salvarEscolha(lista) {
    escolhidas = lista;
    try { if (lista) localStorage.setItem(CHAVE, JSON.stringify(lista)); else localStorage.removeItem(CHAVE); } catch { /* ignora */ }
  }

  const visiveis = () => new Set([...(escolhidas || padrao()), ...COLUNAS.filter((c) => c.fixa).map((c) => c.col)]);

  /* ------------------------------------------------------------ colunas: escolha + ajuste automático */
  function aplicarColunas() {
    const tabela = $("#table");
    if (!tabela) return;
    const vis = visiveis();
    for (const { col } of COLUNAS) tabela.classList.toggle(`oculta-${col}`, !vis.has(col));
    autoOcultas = [];
    const wrap = $(".table-wrap");
    if (window.innerWidth > CARTOES && !tabela.hidden) {
      for (const col of PRIORIDADE) {
        if (wrap.scrollWidth <= wrap.clientWidth + 1) break;
        if (!vis.has(col)) continue;
        tabela.classList.add(`oculta-${col}`);
        autoOcultas.push(col);
      }
    }
    const n = autoOcultas.length;
    const badge = $("#colunas-ocultas");
    badge.hidden = !n;
    badge.textContent = n ? `${n} oculta${n > 1 ? "s" : ""}` : "";
    $("#btn-colunas").title = n ? `Ocultas para caber na tela: ${autoOcultas.map(rotulo).join(", ")}` : "Escolher colunas";
    if (!$("#col-pop").hidden) desenharMenuColunas();
  }

  const rotulo = (col) => COLUNAS.find((c) => c.col === col)?.rotulo || col;

  function desenharMenuColunas() {
    const vis = visiveis();
    $("#col-pop").innerHTML = `<p class="col-pop-title">Colunas exibidas</p>
      ${COLUNAS.filter((c) => !c.fixa).map((c) => `<label class="check-line"><input type="checkbox" data-coluna="${c.col}" ${vis.has(c.col) ? "checked" : ""}>
        <span>${esc(c.rotulo)}${autoOcultas.includes(c.col) ? ` <small class="muted">(oculta: não cabe na tela)</small>` : ""}</span></label>`).join("")}
      <div class="col-pop-foot"><button type="button" class="link" id="col-padrao">Restaurar padrão</button></div>`;
  }

  function alternarMenuColunas(abrir) {
    const pop = $("#col-pop"), btn = $("#btn-colunas");
    const aberto = abrir ?? pop.hidden;
    pop.hidden = !aberto;
    btn.setAttribute("aria-expanded", String(aberto));
    if (aberto) desenharMenuColunas();
  }

  /* ------------------------------------------------------------ filtros */
  function filtrosAtivos() {
    const pf = Object.values(state.pf || {}).filter(Boolean).length;
    return pf + (state.seg ? 1 : 0);
  }

  function atualizarContagem() {
    const n = filtrosAtivos(), el = $("#filtros-count");
    el.hidden = !n;
    el.textContent = n;
  }

  function alternarPainel(abrir) {
    const painel = $("#painel-filtros"), btn = $("#btn-filtros");
    const aberto = abrir ?? painel.hidden;
    painel.hidden = !aberto;
    btn.setAttribute("aria-expanded", String(aberto));
    btn.classList.toggle("on", aberto);
    requestAnimationFrame(aplicarColunas);
  }

  function aplicarFiltro(pf) {
    Object.assign(state, { q: "", cls: "", seg: "", page: 1, pf: { ...pf } });
    $("#search").value = ""; $("#f-class").value = ""; $("#f-segment").value = "";
    $$("[data-pf]").forEach((s) => { s.value = state.pf[s.dataset.pf] || ""; });
    loadClients();
  }

  /* ------------------------------------------------------------ indicadores da carteira */
  async function carregarKpis() {
    const box = $("#kpis");
    if (!box) return;
    let d;
    try { d = await api("/api/perfil/diagnostico"); } catch { box.hidden = true; return; }
    const n = (v) => (v || 0).toLocaleString("pt-BR");
    const tipos = d.tipos || {};
    const kpi = (rotulo, valor, filtro, cls = "", dica = "") => `<button type="button" class="kpi ${cls}" data-kpi='${esc(JSON.stringify(filtro))}' title="${esc(dica || `Mostrar: ${rotulo}`)}">
      <span class="kpi-label">${esc(rotulo)}</span><b>${n(valor)}</b></button>`;
    box.hidden = !d.total;
    box.innerHTML = kpi("Clientes na carteira", d.total, {}, "", "Mostrar todos os clientes")
      + kpi("Contratados", d.situacao_contratual?.Contratado, { contrato: "sim" }, "ok")
      + kpi("Sem contrato", d.situacao_contratual?.["Cliente sem contrato"], { contrato: "nao" }, "warn")
      + kpi("Ex-clientes", tipos["Ex-cliente"], { tipo_cadastro: "Ex-cliente" }, "muted-kpi")
      + kpi("Pendentes de identificação", d.com_pendencia, { pendente: "algum" }, "alert",
        "Clientes com algum campo do perfil comercial sem identificação automática");
  }

  /* ------------------------------------------------------------ menu lateral (telas pequenas) */
  function alternarMenu(abrir) {
    const aberto = abrir ?? !document.body.classList.contains("menu-aberto");
    document.body.classList.toggle("menu-aberto", aberto);
    $("#sidebar-overlay").hidden = !aberto;
    $("#menu-toggle").setAttribute("aria-expanded", String(aberto));
  }

  function avatar() {
    const email = $("#usuario-email").textContent.trim();
    $("#usuario-avatar").textContent = email ? email[0].toUpperCase() : "";
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("#menu-toggle").addEventListener("click", () => alternarMenu());
    $("#sidebar-overlay").addEventListener("click", () => alternarMenu(false));
    $$(".nav-item").forEach((a) => a.addEventListener("click", () => alternarMenu(false)));
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      alternarMenu(false);
      alternarMenuColunas(false);
    });
    new MutationObserver(avatar).observe($("#usuario-email"), { childList: true, characterData: true, subtree: true });

    $("#btn-filtros").addEventListener("click", () => alternarPainel());
    $("#btn-colunas").addEventListener("click", (e) => { e.stopPropagation(); alternarMenuColunas(); });
    $("#col-pop").addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("click", () => alternarMenuColunas(false));
    $("#col-pop").addEventListener("change", (e) => {
      const c = e.target.closest("[data-coluna]");
      if (!c) return;
      const atual = visiveis();
      if (c.checked) atual.add(c.dataset.coluna); else atual.delete(c.dataset.coluna);
      salvarEscolha(COLUNAS.map((x) => x.col).filter((x) => atual.has(x)));
      aplicarColunas();
    });
    $("#col-pop").addEventListener("click", (e) => {
      if (e.target.closest("#col-padrao")) { salvarEscolha(null); aplicarColunas(); }
    });
    $("#kpis").addEventListener("click", (e) => {
      const k = e.target.closest("[data-kpi]");
      if (k) aplicarFiltro(JSON.parse(k.dataset.kpi));
    });
    document.addEventListener("change", (e) => { if (e.target.closest("#painel-filtros, .list-toolbar")) atualizarContagem(); });
    let t;
    window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(aplicarColunas, 120); });
  });

  Object.assign(window, {
    aposTabela: () => { aplicarColunas(); atualizarContagem(); },
    carregarKpis,
  });
})();
