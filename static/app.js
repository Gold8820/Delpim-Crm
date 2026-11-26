"use strict";

/* ========================================================================
   Utilidades
   ======================================================================== */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const brlFmt = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const brl = (cents) => (cents == null ? "—" : brlFmt.format(cents / 100));
const MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"];
const monthLabel = (ym) => (ym ? `${MONTHS[+ym.slice(5, 7) - 1]}/${ym.slice(0, 4)}` : "");

function toast(message, type = "success") {
  const box = document.createElement("div");
  box.className = `toast ${type}`;
  box.textContent = message;
  $("#toasts").appendChild(box);
  setTimeout(() => box.remove(), type === "error" ? 7000 : 4500);
}

async function api(url, options = {}) {
  const init = { ...options };
  if (init.json !== undefined) {
    init.body = JSON.stringify(init.json);
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    delete init.json;
  }
  let res;
  try {
    res = await fetch(url, init);
  } catch {
    throw Object.assign(new Error("Não foi possível conectar ao servidor. Verifique se o sistema está em execução."), { fields: {} });
  }
  if (res.status === 401) {   // sessão expirada ou encerrada: volta para a tela de login
    const destino = location.pathname + location.search + location.hash;
    location.replace(`/login?proximo=${encodeURIComponent(destino)}`);
    throw Object.assign(new Error("Sessão expirada. Entre novamente."), { fields: {}, status: 401 });
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw Object.assign(new Error(body.erro || "Ocorreu um erro inesperado."), { fields: body.campos || {}, status: res.status });
  }
  return init.raw ? res : res.json();
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function parseMoney(text) {
  let s = String(text ?? "").replace(/R\$|\s/gi, "");
  if (!s || !/^[0-9.,]+$/.test(s)) return null;
  if (s.includes(".") && s.includes(",")) {
    const dec = s.lastIndexOf(",") > s.lastIndexOf(".") ? "," : ".";
    const th = dec === "," ? "." : ",";
    s = s.split(th).join("").replace(dec, ".");
  } else if (s.includes(",")) {
    if ((s.match(/,/g) || []).length > 1) return null;
    s = s.replace(",", ".");
  } else if (s.includes(".")) {
    if (/^\d{1,3}(\.\d{3})+$/.test(s)) s = s.replace(/\./g, "");
    else if ((s.match(/\./g) || []).length > 1) return null;
  }
  const n = Number(s);
  return Number.isFinite(n) ? Math.round(n * 100) : null;
}

function formatCnpj(raw) {
  const v = raw.replace(/[^0-9a-z]/gi, "").toUpperCase().slice(0, 14);
  let out = v.slice(0, 2);
  if (v.length > 2) out += "." + v.slice(2, 5);
  if (v.length > 5) out += "." + v.slice(5, 8);
  if (v.length > 8) out += "/" + v.slice(8, 12);
  if (v.length > 12) out += "-" + v.slice(12);
  return out;
}
const formatCep = (raw) => { const v = raw.replace(/\D/g, "").slice(0, 8); return v.length > 5 ? `${v.slice(0, 5)}-${v.slice(5)}` : v; };

function classOf(cents, faixas) {
  if (cents == null) return null;
  for (const f of faixas) if (cents >= f.min_centavos) return f.classificacao;
  return null;
}
const badge = (cls) => (cls ? `<span class="badge cls-${esc(cls)}">${esc(cls)}</span>` : `<span class="badge cls-none">Sem valor</span>`);

/* ========================================================================
   Estado e tela principal
   ======================================================================== */
const state = { q: "", cls: "", seg: "", mes: "", sort: "codigo", dir: "asc", page: 1, perPage: 25, meta: null, total: 0 };

async function loadMeta() {
  state.meta = await api("/api/meta");
  const { meses, segmentos, faixas } = state.meta;

  const monthSel = $("#f-month");
  if (!meses.includes(state.mes)) state.mes = meses[0] || "";
  monthSel.innerHTML = meses.length
    ? meses.map((m) => `<option value="${m}">${esc(monthLabel(m))}</option>`).join("")
    : `<option value="">Sem valores</option>`;
  monthSel.value = state.mes;
  monthSel.disabled = !meses.length;

  const segSel = $("#f-segment");
  if (state.seg && !segmentos.includes(state.seg)) state.seg = "";
  segSel.innerHTML = `<option value="">Todos</option>` + segmentos.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  segSel.value = state.seg;

  $("#dl-segments").innerHTML = segmentos.map((s) => `<option value="${esc(s)}">`).join("");
  $("#c-estado").innerHTML = `<option value="">—</option>` +
    Object.keys(state.meta.ufs).map((uf) => `<option value="${uf}">${uf}</option>`).join("");

  const range = (f) => {
    if (f.max_centavos == null) return `${brl(f.min_centavos)} ou mais`;
    if (f.min_centavos === 0) return `até ${brl(f.max_centavos)}`;
    return `${brl(f.min_centavos)} a ${brl(f.max_centavos)}`;
  };
  if (window.perfilMeta) window.perfilMeta();
  if (window.carregarKpis) window.carregarKpis();
  $("#legend").innerHTML = faixas.map((f) => `<span><i class="dot cls-${esc(f.classificacao)}"></i><b>${esc(f.classificacao)}</b> ${esc(range(f))}</span>`).join("");
}

async function loadClients() {
  const table = $("#table");
  table.classList.add("loading");
  const params = new URLSearchParams({
    q: state.q, classificacao: state.cls, segmento: state.seg, mes: state.mes,
    ordem: state.sort, direcao: state.dir, pagina: state.page, por_pagina: state.perPage, ...(state.pf || {}),
  });
  const seq = (state.reqSeq = (state.reqSeq || 0) + 1);   // filtros trocados rápido: só a última resposta vale
  try {
    const data = await api(`/api/clientes?${params}`);
    if (seq !== state.reqSeq) return;
    const lastPage = Math.max(1, Math.ceil(data.total / data.por_pagina));
    if (state.page > lastPage) { state.page = lastPage; return loadClients(); }
    state.total = data.total;
    renderTable(data);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    table.classList.remove("loading");
  }
}

function renderTable(data) {
  const filtered = state.q || state.cls || state.seg || Object.values(state.pf || {}).some(Boolean);
  const n = data.total_geral;
  $("#subtitle").textContent = n === 0
    ? "Nenhum cliente cadastrado"
    : `${n} ${n === 1 ? "cliente cadastrado" : "clientes cadastrados"}` +
      (filtered ? ` · ${data.total} ${data.total === 1 ? "resultado" : "resultados"}` : "") +
      (data.mes ? ` · valores de ${monthLabel(data.mes)}` : "");

  $("#tbody").innerHTML = data.clientes.map((c) => {
    const sub = c.razao_social && c.nome_fantasia && c.razao_social !== c.nome_fantasia
      ? `<div class="client-sub" title="${esc(c.razao_social)}">${esc(c.razao_social)}</div>` : "";
    return `<tr data-id="${c.id}">
      <td class="code" data-col="codigo" data-label="Código">${esc(c.codigo)}</td>
      <td data-col="cliente" data-label="Cliente"><div class="client-name" title="${esc(c.nome)}">${esc(c.nome)}</div>${sub}</td>
      <td class="perfil-cell" data-col="perfil" data-label="Tipo / Região">${esc(c.tipo_cadastro || "—")}<div class="client-sub">${esc([c.regiao1, c.regiao2].filter(Boolean).join(" · ") || "")}${c.situacao_contratual === "Cliente sem contrato" ? `<span class="tag-sem">sem contrato</span>` : ""}</div></td>
      <td data-col="cnpj" data-label="CNPJ">${esc(c.cnpj || "—")}</td>
      <td class="truncate" data-col="cidade" data-label="Cidade" title="${esc(c.cidade || "")}">${esc(c.cidade || "—")}</td>
      <td data-col="estado" data-label="UF">${esc(c.estado || "—")}</td>
      <td class="truncate" data-col="ramo" data-label="Ramo" title="${esc(c.segmento || "")}">${esc(c.segmento || "—")}</td>
      <td class="truncate" data-col="vendedor" data-label="Vendedor" title="${esc(c.vendedor || "")}">${esc(c.vendedor || "—")}</td>
      <td class="num" data-col="valor" data-label="Valor no mês">${brl(c.valor_centavos)}</td>
      <td data-col="classificacao" data-label="Classe">${badge(c.classificacao)}</td>
      <td class="col-actions" data-col="acoes"><div class="row-actions">
        <button type="button" data-act="edit" aria-label="Editar ${esc(c.nome)}" title="Editar"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg></button>
        <button type="button" class="del" data-act="delete" aria-label="Excluir ${esc(c.nome)}" title="Excluir"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6"/></svg></button>
      </div></td></tr>`;
  }).join("");

  const empty = $("#empty");
  const none = data.clientes.length === 0;
  $("#table").hidden = none;
  empty.hidden = !none;
  if (none) {
    empty.innerHTML = data.total_geral === 0
      ? `<strong>Nenhum cliente cadastrado ainda</strong>Cadastre um cliente manualmente ou importe uma planilha Excel/CSV.<br>
         <button class="btn btn-primary" data-empty="new">Novo cliente</button><button class="btn" data-empty="import">Importar planilha</button>`
      : `<strong>Nenhum cliente encontrado</strong>Nenhum cadastro corresponde à pesquisa ou aos filtros atuais.<br>
         <button class="btn" data-empty="clear">Limpar filtros</button>`;
  }

  const from = data.total === 0 ? 0 : (data.pagina - 1) * data.por_pagina + 1;
  const to = Math.min(data.total, data.pagina * data.por_pagina);
  const pages = Math.max(1, Math.ceil(data.total / data.por_pagina));
  $("#range").textContent = data.total ? `Mostrando ${from}–${to} de ${data.total}` : "";
  $("#page-info").textContent = `Página ${data.pagina} de ${pages}`;
  $("#prev").disabled = data.pagina <= 1;
  $("#next").disabled = data.pagina >= pages;

  $$("#table thead th[data-sort]").forEach((th) => {
    const active = th.dataset.sort === state.sort;
    th.querySelector(".arrow")?.remove();
    if (active) th.insertAdjacentHTML("beforeend", `<span class="arrow">${state.dir === "asc" ? "▲" : "▼"}</span>`);
    th.setAttribute("aria-sort", active ? (state.dir === "asc" ? "ascending" : "descending") : "none");
  });
  if (window.aposTabela) window.aposTabela();   // ajuste das colunas à largura da tela (layout.js)
}

async function refreshAll() {
  await loadMeta();
  await loadClients();
}

function wireMain() {
  $("#search").addEventListener("input", debounce((e) => { state.q = e.target.value.trim(); state.page = 1; loadClients(); }, 250));
  $("#f-class").addEventListener("change", (e) => { state.cls = e.target.value; state.page = 1; loadClients(); });
  $("#f-segment").addEventListener("change", (e) => { state.seg = e.target.value; state.page = 1; loadClients(); });
  $("#f-month").addEventListener("change", (e) => { state.mes = e.target.value; state.page = 1; loadClients(); });
  $("#per-page").addEventListener("change", (e) => { state.perPage = +e.target.value; state.page = 1; loadClients(); });
  $("#prev").addEventListener("click", () => { state.page--; loadClients(); });
  $("#next").addEventListener("click", () => { state.page++; loadClients(); });

  $("#table thead").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]");
    if (!th) return;
    const key = th.dataset.sort;
    if (state.sort === key) state.dir = state.dir === "asc" ? "desc" : "asc";
    else { state.sort = key; state.dir = key === "valor" || key === "classificacao" ? "desc" : "asc"; }
    state.page = 1;
    loadClients();
  });

  $("#tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = +btn.closest("tr").dataset.id;
    if (btn.dataset.act === "edit") openClientForm(id);
    else askDelete(id, btn.closest("tr"));
  });
  $("#tbody").addEventListener("dblclick", (e) => {
    const row = e.target.closest("tr");
    if (row && !e.target.closest("button")) openClientForm(+row.dataset.id);
  });

  $("#empty").addEventListener("click", (e) => {
    const b = e.target.closest("[data-empty]");
    if (!b) return;
    if (b.dataset.empty === "new") openClientForm();
    if (b.dataset.empty === "import") (window.openAutoImport || openImport)();
    if (b.dataset.empty === "clear") {
      if (window.limparFiltros) { window.limparFiltros(); return; }
      Object.assign(state, { q: "", cls: "", seg: "", page: 1 });
      $("#search").value = ""; $("#f-class").value = ""; $("#f-segment").value = "";
      loadClients();
    }
  });

  $("#btn-new").addEventListener("click", () => openClientForm());
  $("#btn-import").addEventListener("click", () => (window.openAutoImport || openImport)());
  $$("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));
}

/* ========================================================================
   Cadastro manual (novo / editar)
   ======================================================================== */
const clientForm = { id: null, payments: [], contracts: [], editingNote: null, extras: {} };

function monthOptions(selected) {
  const set = new Set(state.meta.meses);
  const [y, m] = state.meta.mes_atual.split("-").map(Number);
  for (let i = -1; i < 36; i++) {
    const d = new Date(y, m - 1 - i, 1);
    set.add(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
  }
  if (selected) set.add(selected);
  return [...set].sort().reverse().map((v) => `<option value="${v}">${esc(monthLabel(v))}</option>`).join("");
}

function clearFormErrors() {
  $$("#form-client small.err").forEach((s) => (s.textContent = ""));
  $$("#form-client .invalid").forEach((i) => i.classList.remove("invalid"));
  $("#client-error").hidden = true;
}

function showFormErrors(err) {
  const fields = err.fields || {};
  const first = Object.keys(fields)[0];
  for (const [name, msg] of Object.entries(fields)) {
    const input = $(`#form-client [name="${name === "valor_centavos" ? "valor" : name}"]`);
    if (!input) continue;
    input.classList.add("invalid");
    const small = input.closest(".field").querySelector("small.err");
    if (small) small.textContent = msg;
  }
  if (!first) { const box = $("#client-error"); box.textContent = err.message; box.hidden = false; }
  else { $(`#form-client [name="${first}"]`)?.focus(); }
}

function syncValueForMonth() {
  const month = $("#c-mes").value;
  const found = clientForm.payments.find((p) => p.mes_referencia === month);
  $("#c-valor").value = found ? (found.valor_centavos / 100).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "";
  updateClassPreview();
}

function updateClassPreview() {
  const text = $("#c-valor").value.trim();
  const box = $("#class-preview");
  if (!text) { box.innerHTML = `<span class="muted">Informe o valor</span>`; return; }
  const cents = parseMoney(text);
  box.innerHTML = cents == null ? `<span class="muted">Valor inválido</span>` : badge(classOf(cents, state.meta.faixas));
}

function renderHistory() {
  const box = $("#history");
  if (!clientForm.payments.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<h3>Histórico de pagamentos</h3><table><tbody>${clientForm.payments.map((p) =>
    `<tr><td>${esc(monthLabel(p.mes_referencia))}</td><td class="num">${brl(p.valor_centavos)}</td><td>${badge(p.classificacao)}</td></tr>`).join("")}</tbody></table>`;
}

/* Dados adicionais vindos das planilhas (Pessoa, Inscrição estadual, Contato...): só consulta. */
function extrasList(extras, labels) {
  const items = Object.entries(extras || {}).filter(([, v]) => v != null && v !== "");
  if (!items.length) return "";
  return `<dl class="extras">${items.map(([k, v]) => `<dt>${esc(labels[k] || k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
}

const fieldLabels = (key) => Object.fromEntries((state.meta[key] || []).map((c) => [c.chave, c.rotulo]));

function renderExtras() {
  const box = $("#extras-box");
  const html = extrasList(clientForm.extras, fieldLabels("campos"));
  box.hidden = !html;
  box.innerHTML = html ? `<h3>Dados adicionais da planilha de clientes</h3>${html}` : "";
}

function renderContracts() {
  const box = $("#contracts-box");
  if (!clientForm.contracts.length) { box.hidden = true; return; }
  box.hidden = false;
  const noteRow = (c) => {
    if (clientForm.editingNote !== c.id) {
      return `<div class="note-text">${c.observacoes ? esc(c.observacoes) : `<span class="muted">Sem observações</span>`}</div>
        <button type="button" class="link" data-edit-note="${c.id}">Editar observação</button>`;
    }
    return `<textarea class="note-input" rows="3" maxlength="4000" aria-label="Observações do contrato ${esc(c.numero)}" data-note-input="${c.id}">${esc(c.observacoes || "")}</textarea>
      <div class="note-actions"><button type="button" class="btn btn-sm" data-cancel-note>Cancelar</button>
      <button type="button" class="btn btn-sm btn-primary" data-save-note="${c.id}">Salvar observação</button></div>`;
  };
  box.innerHTML = `<h3>Contratos (${clientForm.contracts.length})</h3><table><thead><tr><th>Contrato</th><th>Produto</th><th>Situação</th><th class="num">Último valor mensal</th><th>Observações do contrato</th></tr></thead><tbody>${clientForm.contracts.map((c) =>
    `<tr><td><b>${esc(c.numero)}</b></td><td>${esc(c.produto || "—")}</td><td>${esc(c.situacao || "—")}</td>
     <td class="num">${c.ultimo_valor_centavos == null ? "—" : `${brl(c.ultimo_valor_centavos)} <span class="muted">(${esc(monthLabel(c.ultimo_mes))})</span>`}</td>
     <td class="note-cell">${noteRow(c)}${(() => {
       const extra = extrasList(c.dados_extras, fieldLabels("campos_contratos"));
       return extra ? `<details class="contract-extras"><summary>Dados do contrato</summary>${extra}</details>` : "";
     })()}</td></tr>`).join("")}</tbody></table>`;
}

async function onContractsClick(e) {
  const edit = e.target.closest("[data-edit-note]");
  const cancel = e.target.closest("[data-cancel-note]");
  const save = e.target.closest("[data-save-note]");
  if (edit) { clientForm.editingNote = +edit.dataset.editNote; renderContracts(); return; }
  if (cancel) { clientForm.editingNote = null; renderContracts(); return; }
  if (save) {
    const id = +save.dataset.saveNote;
    const text = $(`[data-note-input="${id}"]`).value;
    save.disabled = true;
    try {
      const d = await api(`/api/clientes/${clientForm.id}/contratos/${id}/observacoes`, { method: "PUT", json: { observacoes: text } });
      clientForm.contracts = d.contratos;
      clientForm.editingNote = null;
      toast("Observação do contrato salva.");
      renderContracts();
    } catch (err) {
      toast(err.message, "error");
      save.disabled = false;
    }
  }
}

async function openClientForm(id = null) {
  const form = $("#form-client");
  form.reset();
  clearFormErrors();
  clientForm.id = id;
  clientForm.payments = [];
  clientForm.contracts = [];
  clientForm.extras = {};
  clientForm.editingNote = null;
  $("#client-title").textContent = id ? "Editar cliente" : "Novo cliente";
  let defaultMonth = state.mes || state.meta.mes_atual;

  if (id) {
    try {
      const c = await api(`/api/clientes/${id}`);
      for (const el of form.elements) {
        if (el.name && el.name in c && el.name !== "mes" && el.name !== "valor") el.value = c[el.name] ?? "";
      }
      clientForm.payments = c.pagamentos;
      clientForm.contracts = c.contratos || [];
      clientForm.extras = c.dados_extras || {};
      if (window.perfilPreencher) window.perfilPreencher(c);
    } catch (err) { toast(err.message, "error"); return; }
  } else {
    defaultMonth = state.meta.mes_atual;
    if (window.perfilPreencher) window.perfilPreencher(null);
  }
  $("#c-mes").innerHTML = monthOptions(defaultMonth);
  $("#c-mes").value = defaultMonth;
  if (id) syncValueForMonth(); else updateClassPreview();
  renderHistory();
  renderContracts();
  renderExtras();
  $("#dlg-client").showModal();
  $("#c-codigo").focus();
}

function wireClientForm() {
  const form = $("#form-client");
  $("#c-cnpj").addEventListener("input", (e) => { e.target.value = formatCnpj(e.target.value); });
  $("#c-cep").addEventListener("input", (e) => { e.target.value = formatCep(e.target.value); });
  $("#c-valor").addEventListener("input", updateClassPreview);
  $("#c-valor").addEventListener("blur", (e) => {
    const cents = parseMoney(e.target.value);
    if (cents != null) e.target.value = (cents / 100).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  });
  $("#c-mes").addEventListener("change", () => { if (clientForm.id) syncValueForMonth(); });
  $("#contracts-box").addEventListener("click", onContractsClick);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearFormErrors();
    const payload = Object.fromEntries(new FormData(form).entries());
    if (window.perfilEdicoes) payload.perfil = window.perfilEdicoes();
    const btn = $("#client-save");
    btn.disabled = true;
    try {
      const saved = await api(clientForm.id ? `/api/clientes/${clientForm.id}` : "/api/clientes",
        { method: clientForm.id ? "PUT" : "POST", json: payload });
      $("#dlg-client").close();
      toast(clientForm.id ? `Cliente “${saved.nome}” atualizado.` : `Cliente “${saved.nome}” cadastrado.`);
      if (payload.valor && payload.mes) state.mes = payload.mes;
      if (!clientForm.id) state.page = 1;
      await refreshAll();
    } catch (err) {
      showFormErrors(err);
      if (!Object.keys(err.fields || {}).length) toast(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  });
}

/* ========================================================================
   Confirmações (exclusões)
   ======================================================================== */
let confirmAction = null;
function confirmDialog({ title, html, label = "Excluir", action }) {
  $("#delete-title").textContent = title;
  $("#delete-text").innerHTML = html;
  $("#delete-confirm").textContent = label;
  confirmAction = action;
  $("#dlg-delete").showModal();
}

function askDelete(id, row) {
  const name = row.querySelector(".client-name").textContent;
  const code = row.querySelector(".code").textContent;
  confirmDialog({
    title: "Excluir cliente",
    html: `Deseja excluir <b>${esc(name)}</b> (código ${esc(code)})? O cadastro, o histórico de pagamentos mensais e os contratos deste cliente serão removidos. <b>Esta ação não pode ser desfeita.</b>`,
    action: async () => {
      await api(`/api/clientes/${id}`, { method: "DELETE" });
      toast("Cliente excluído.");
      await refreshAll();
    },
  });
}

function wireDelete() {
  $("#delete-confirm").addEventListener("click", async () => {
    const btn = $("#delete-confirm");
    btn.disabled = true;
    try {
      await confirmAction();
      $("#dlg-delete").close();
    } catch (err) {
      toast(err.message, "error");
    } finally { btn.disabled = false; }
  });
}

/* ========================================================================
   Navegação entre telas
   ======================================================================== */
function showView(name) {
  const settings = name === "configuracoes";
  $("#view-clients").hidden = settings;
  $("#view-config").hidden = !settings;
  $$(".nav-item").forEach((a) => {
    const on = a.dataset.view === (settings ? "configuracoes" : "clientes");
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  document.title = settings ? "Configurações" : "Clientes";
  if ($("#topbar-page")) $("#topbar-page").textContent = settings ? "Configurações" : "Clientes";
  if (settings) {
    loadConfigs(); loadCurva();
    if (window.loadFonteValor) window.loadFonteValor();
    if (window.loadRegioes) window.loadRegioes();
  }
}
const routeFromHash = () => showView(location.hash === "#configuracoes" ? "configuracoes" : "clientes");

/* ========================================================================
   Importação de planilhas (clientes + contratos)
   ======================================================================== */
const ROLE = {
  clientes: { titulo: "Planilha de clientes", desc: "Cadastro: identificação, CNPJ, endereço, cidade, telefone…" },
  contratos: { titulo: "Planilha de contratos", desc: "Contratos: valores e informações comerciais dos clientes." },
};
const imp = {};
const sides = () => ["clientes", "contratos"].filter((p) => imp[p]);
const pendCount = (s) => Object.keys(s.pendencias).length;

function resetImport() {
  Object.assign(imp, {
    step: 1, clientes: null, contratos: null, unica: false, chave: "cnpj", mesPadrao: state.meta.mes_atual, substituir: false,
    analysis: null, tab: "clientes", filter: { clientes: "todos", contratos: "todos" }, result: null,
    busy: { clientes: false, contratos: false }, message: "",
  });
}

function openImport() {
  resetImport();
  renderImport();
  $("#dlg-import").showModal();
}

function setSteps() {
  $$("#steps li").forEach((li) => {
    const n = +li.dataset.step;
    li.classList.toggle("current", n === imp.step);
    li.classList.toggle("done", n < imp.step);
    li.textContent = (n < imp.step ? "✓ " : `${n}. `) + li.textContent.replace(/^(✓ |\d\. )/, "");
  });
}

function renderImport() {
  setSteps();
  ({ 1: renderStepFile, 2: renderStepMap, 3: renderStepReview, 4: renderStepDone })[imp.step]();
}

const foot = (html) => { $("#import-foot").innerHTML = html; };
const messageHtml = () => (imp.message ? `<div class="notice">${esc(imp.message)}</div>` : "");

function sideFrom(papel, d) {
  return {
    papel, token: d.token, filename: d.arquivo, sheets: d.planilhas, sheet: d.planilha, columns: d.colunas,
    total: d.total_linhas, mapping: d.mapeamento, pendencias: d.pendencias || {}, configuracao: d.configuracao,
    configId: d.configuracao?.id ?? null, configs: d.configuracoes, definirPadrao: false, manual: false,
    linhaCabecalho: d.linha_cabecalho ?? null,
  };
}

/* Com planilha única, as colunas de valor/mês do cliente só saem da associação se houver contratos de fato. */
const contratosAtivos = () => !!imp.contratos && !(imp.unica && imp.contratos.mapping.numero_contrato == null);

/* Opções comuns (mês de referência e células vazias) */
function optionsHtml() {
  return `<div class="options">
    <div class="row"><label for="mes-padrao">Mês de referência dos valores</label>
      <select id="mes-padrao">${monthOptions(imp.mesPadrao)}</select>
      <span class="muted">Usado quando a planilha não tem coluna de mês. Se o contrato ou cliente já tiver valor neste mês, ele será <b>substituído</b>, nunca somado.</span></div>
    <label class="check"><input type="checkbox" id="substituir" ${imp.substituir ? "checked" : ""}>
      <span>Substituir dados existentes por células vazias da planilha<small>Desmarcado (recomendado): células vazias são ignoradas e os dados já cadastrados são preservados.</small></span></label>
  </div>`;
}

/* Um único ouvinte de mudanças para todo o corpo do assistente */
function onBodyChange(e) {
  const t = e.target;
  if (t.id === "unica") toggleUnica(t.checked);
  else if (t.matches("input[data-header]")) rereadSide(t.dataset.header, { header: t.value });
  else if (t.id === "mes-padrao") imp.mesPadrao = t.value;
  else if (t.id === "substituir") imp.substituir = t.checked;
  else if (t.name === "chave") { imp.chave = t.value; refreshRelation(); }
  else if (t.matches("select[data-field]")) onFieldChange(t);
  else if (t.matches("select[data-sheet]")) rereadSide(t.dataset.sheet, { sheet: t.value });
  else if (t.matches("select[data-config]")) rereadSide(t.dataset.config, { config: t.value });
  else if (t.matches("input[data-default]")) imp[t.dataset.default].definirPadrao = t.checked;
  else if (t.matches("input[data-file]")) { if (t.files[0]) uploadFile(t.dataset.file, t.files[0]); }
}

/* --- Passo 1: arquivos --- */
function dropHtml(papel) {
  const r = ROLE[papel], s = imp[papel];
  const input = `<input type="file" accept=".xlsx,.csv,.xls" data-file="${papel}">`;
  if (papel === "contratos" && imp.unica && s) {
    return `<div class="drop-card"><h3>${r.titulo}</h3><div class="file-chip">📄 ${esc(s.filename)} · mesma planilha dos clientes</div>
      <div class="notice info">Planilha única: a mesma planilha é lida como clientes e como contratos.</div></div>`;
  }
  if (imp.busy[papel]) {
    return `<div class="drop-card"><h3>${r.titulo}</h3><div class="dropzone"><strong><span class="spinner"></span><span data-busy="${papel}">Lendo a planilha…</span></strong></div></div>`;
  }
  if (!s) {
    return `<div class="drop-card"><h3>${r.titulo}</h3>
      <div class="dropzone" data-drop="${papel}"><strong>Arraste o arquivo para cá</strong>
        <span class="muted">Excel (.xlsx), CSV ou .xls (XML do Excel 2003)</span><br><button type="button" class="btn btn-primary" data-pick="${papel}">Selecionar arquivo</button></div>
      <p class="help" style="margin-top:8px">${esc(r.desc)}</p>${input}</div>`;
  }
  const n = pendCount(s);
  const status = n
    ? `<div class="notice warn-box">⚠ ${n} ${n === 1 ? "campo precisa" : "campos precisam"} de correção. No próximo passo aparecem ${n === 1 ? "somente ele" : "somente eles"}; as demais associações foram mantidas.</div>`
    : s.configuracao
      ? `<div class="notice ok-box">✓ Todas as colunas foram reconhecidas pelo mapeamento padrão “${esc(s.configuracao.nome)}”.</div>`
      : `<div class="notice info">Colunas associadas automaticamente pelos títulos. Confira no próximo passo e, se quiser, salve como padrão.</div>`;
  return `<div class="drop-card" data-drop="${papel}"><h3>${r.titulo}</h3>
    <div class="file-chip">📄 ${esc(s.filename)} · ${s.total.toLocaleString("pt-BR")} ${s.total === 1 ? "linha" : "linhas"}</div>
    ${status}
    <div class="drop-actions"><button type="button" class="btn btn-sm" data-pick="${papel}">Trocar arquivo</button>
      <button type="button" class="btn btn-sm" data-remove="${papel}">Remover</button></div>${input}</div>`;
}

function renderStepFile() {
  const any = sides().length > 0;
  $("#import-body").innerHTML = `${messageHtml()}
    <p style="margin:0 0 14px">Selecione as duas planilhas, <b>sem precisar juntá-las</b>. Cada uma pode ser Excel (.xlsx) ou CSV. Se houver um mapeamento padrão salvo, as colunas são reconhecidas automaticamente.</p>
    <label class="check"><input type="checkbox" id="unica" ${imp.unica ? "checked" : ""}>
      <span>Esta planilha tem clientes e contratos juntos (planilha única)<small>Usa o mesmo arquivo como clientes e como contratos. Sem coluna de número do contrato, só os clientes são importados.</small></span></label>
    <div class="drops">${dropHtml("clientes")}${dropHtml("contratos")}</div>
    ${any ? optionsHtml() : ""}
    <p class="help">Modelos de planilha: <a href="/api/importacao/modelo.csv">clientes</a> · <a href="/api/importacao/modelo.csv?tipo=contratos">contratos</a>.
      <button type="button" class="link" id="goto-config">Gerenciar mapeamentos salvos</button></p>`;
  foot(`<button type="button" class="btn" id="cancel-import">Cancelar</button><span class="spacer"></span>
    <button type="button" class="btn btn-primary" id="to-next" ${any ? "" : "disabled"}>Continuar</button>`);

  const body = $("#import-body");
  body.onchange = onBodyChange;
  body.onclick = (e) => {
    const pick = e.target.closest("[data-pick]"), rm = e.target.closest("[data-remove]");
    if (pick) $(`input[data-file="${pick.dataset.pick}"]`).click();
    if (rm) {
      if (rm.dataset.remove === "clientes") { imp.unica = false; imp.contratos = null; }
      imp[rm.dataset.remove] = null; imp.message = ""; renderImport();
    }
    if (e.target.id === "goto-config") { $("#dlg-import").close(); location.hash = "#configuracoes"; }
  };
  $$("[data-drop]", body).forEach((z) => {
    ["dragenter", "dragover"].forEach((ev) => z.addEventListener(ev, (e) => { e.preventDefault(); z.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => z.addEventListener(ev, (e) => { e.preventDefault(); z.classList.remove("over"); }));
    z.addEventListener("drop", (e) => e.dataTransfer.files[0] && uploadFile(z.dataset.drop, e.dataTransfer.files[0]));
  });
  $("#cancel-import").onclick = () => $("#dlg-import").close();
  $("#to-next").onclick = goNext;
}

/* A planilha é lida no navegador (static/planilhas.js) e as linhas vão ao servidor em lotes:
   o servidor não precisa abrir o Excel (limite de CPU do Cloudflare Workers).
   Se o navegador não conseguir ler por um motivo técnico, usa o envio do arquivo inteiro, como antes. */
async function sendFile(papel, file) {
  if (window.Planilhas) {
    let lido = null;
    try {
      lido = await window.Planilhas.ler(file);
    } catch (err) {
      if (err instanceof window.Planilhas.ErroPlanilha) throw err;   // problema na planilha: mostra a mensagem
      console.warn("Leitura no navegador indisponível; enviando o arquivo ao servidor.", err);
    }
    if (lido) {
      return window.Planilhas.enviar((url, json) => api(url, { method: "POST", json }), lido, { papel },
        (feitos, total) => { if (total > 1) setBusyText(papel, `Enviando… ${Math.round((100 * feitos) / total)}%`); });
    }
  }
  const fd = new FormData();
  fd.append("arquivo", file);
  fd.append("papel", papel);
  return api("/api/importacao/arquivo", { method: "POST", body: fd });
}

function setBusyText(papel, text) {
  const box = $(`[data-busy="${papel}"]`);
  if (box) box.textContent = text;
}

async function uploadFile(papel, file) {
  imp.busy[papel] = true; imp.message = "";
  renderImport();
  try {
    const d = await sendFile(papel, file);
    imp[papel] = sideFrom(papel, d);
    if (papel === "contratos") { imp.chave = d.chave || "cnpj"; imp.unica = false; }
    if (papel === "clientes" && imp.unica) await syncUnica();
  } catch (err) {
    imp.message = err.message;
  } finally {
    imp.busy[papel] = false;
    renderImport();
  }
}

/* Planilha única: os contratos usam a mesma planilha, com a associação própria da seção de contratos. */
async function syncUnica() {
  const c = imp.clientes;
  if (!imp.unica) return;
  if (!c) { imp.contratos = null; return; }
  try {
    const d = await api("/api/importacao/planilha", { method: "POST",
      json: { token: c.token, papel: "contratos", planilha: c.sheet, manual: false } });
    imp.contratos = sideFrom("contratos", { ...d, token: c.token, arquivo: c.filename });
    imp.chave = d.chave || imp.chave;
  } catch (err) {
    imp.unica = false; imp.contratos = null; imp.message = err.message;
  }
}

async function toggleUnica(checked) {
  imp.message = "";
  if (checked) {
    imp.unica = true;
    await syncUnica();
  } else {
    imp.unica = false;
    if (imp.contratos && imp.clientes && imp.contratos.token === imp.clientes.token) imp.contratos = null;
  }
  renderImport();
}

function mappingProblem() {
  const c = imp.clientes, k = imp.contratos;
  if (c && c.mapping.codigo == null && c.mapping.cnpj == null) return "Planilha de clientes: associe a coluna de Código ou de CNPJ.";
  if (k && contratosAtivos()) {
    if (k.mapping.numero_contrato == null) return "Planilha de contratos: associe a coluna do número do contrato.";
    if (k.mapping.chave_cliente == null) return "Planilha de contratos: associe a coluna que identifica o cliente.";
    if (c && c.mapping[imp.chave] == null) return `Para relacionar por ${imp.chave === "cnpj" ? "CNPJ" : "código"}, associe essa coluna também na planilha de clientes.`;
  }
  return null;
}

/* Com mapeamento padrão aplicado e nada a corrigir, vai direto para a revisão. */
function goNext() {
  const ready = sides().every((p) => imp[p].configuracao && pendCount(imp[p]) === 0) && !mappingProblem();
  if (ready) return runPreview();
  imp.step = 2; imp.message = "";
  renderImport();
}

/* --- Passo 2: associação de colunas --- */
const exemplos = (s, idx) => (idx == null ? "" : s.columns[idx].exemplos.length ? `Ex.: ${esc(s.columns[idx].exemplos.join(" · "))}` : "");

function fieldRowsHtml(papel) {
  const s = imp[papel];
  let fields = papel === "clientes" ? state.meta.campos : state.meta.campos_contratos.filter((f) => f.chave !== "chave_cliente");
  if (papel === "clientes" && contratosAtivos()) fields = fields.filter((f) => f.chave !== "valor" && f.chave !== "mes");
  const hints = { valor_mensal: "Escolha a coluna do valor pago no mês. Ela define a classificação.", numero_contrato: "Obrigatório: evita duplicar contratos." };
  if (imp.unica) hints.numero_contrato = "Opcional na planilha única: sem ele, só os clientes são importados.";
  return fields.map((f) => {
    const p = s.pendencias[f.chave];
    const note = p
      ? `<span class="fix">⚠ ${esc(p.motivo)} ${p.sugestao != null ? `Sugestão: “${esc(s.columns[p.sugestao].nome)}”. Confirme ou escolha outra.` : "Escolha a coluna correta ou deixe sem associar."}</span>`
      : exemplos(s, s.mapping[f.chave]);
    return `<tr class="${p ? "needs-fix" : ""}" data-row="${f.chave}"><td>${esc(f.rotulo)}${papel === "contratos" && hints[f.chave] ? `<small class="hint">${hints[f.chave]}</small>` : ""}</td>
      <td>${selectHtml(papel, f.chave, f.rotulo)}</td><td class="sample">${note}</td></tr>`;
  }).join("");
}

const selectHtml = (papel, field, label) => `<select data-papel="${papel}" data-field="${field}" aria-label="Coluna para ${esc(label)}">
  <option value="">— Não importar —</option>
  ${imp[papel].columns.map((c) => `<option value="${c.indice}" ${imp[papel].mapping[field] === c.indice ? "selected" : ""}>${esc(c.nome)}</option>`).join("")}</select>`;

function relationHtml() {
  const c = imp.clientes;
  const label = imp.chave === "cnpj" ? "CNPJ" : "código do cliente";
  const cidx = c ? c.mapping[imp.chave] : null;
  const left = !c ? `<span class="muted">Sem planilha de clientes: os contratos serão relacionados aos clientes já cadastrados.</span>`
    : cidx == null ? `<span class="fix">⚠ Associe a coluna de ${label} na planilha de clientes.</span>`
    : `<b>${esc(c.columns[cidx].nome)}</b>`;
  return `<div class="relation" id="relation-box">
    <div class="rel-title">Como relacionar os contratos aos clientes</div>
    <div class="rel-row"><span>Relacionar por</span>
      <label class="radio"><input type="radio" name="chave" value="cnpj" ${imp.chave === "cnpj" ? "checked" : ""}> CNPJ <em>(recomendado)</em></label>
      <label class="radio"><input type="radio" name="chave" value="codigo" ${imp.chave === "codigo" ? "checked" : ""}> Código do cliente</label></div>
    <div class="rel-grid"><div><small>Planilha de clientes</small>${left}</div><div class="rel-eq">=</div>
      <div><small>Planilha de contratos</small>${selectHtml("contratos", "chave_cliente", "relacionamento")}</div></div>
    <small class="muted">Pontos, barras e traços são ignorados: 11.222.333/0001-81 e 11222333000181 são o mesmo CNPJ. Um cliente pode ter vários contratos.</small>
  </div>`;
}

function refreshRelation() {
  const box = $("#relation-box");
  if (box) box.outerHTML = relationHtml();
}

function fixNoticeHtml(papel) {
  const n = pendCount(imp[papel]);
  return n ? `<div class="notice warn-box" data-fixnote="${papel}">⚠ ${n} ${n === 1 ? "campo precisa" : "campos precisam"} de correção (destacados abaixo). As demais associações do mapeamento padrão foram mantidas.</div>` : "";
}

function panelHtml(papel) {
  const s = imp[papel], r = ROLE[papel];
  return `<section class="panel" data-panel="${papel}">
    <header class="panel-head"><h3>${r.titulo}</h3><span class="file-chip">📄 ${esc(s.filename)} · ${s.total.toLocaleString("pt-BR")} ${s.total === 1 ? "linha" : "linhas"}</span></header>
    <div class="panel-tools">
      <label>Linha do cabeçalho <input type="number" min="1" max="1000" data-header="${papel}" value="${s.linhaCabecalho ?? ""}" placeholder="automática" style="width:110px"></label>
      ${s.sheets.length > 1 ? `<label>Aba <select data-sheet="${papel}">${s.sheets.map((n) => `<option ${n === s.sheet ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>` : ""}
      <label>Mapeamento <select data-config="${papel}"><option value="">Automático (sem configuração salva)</option>
        ${s.configs.map((c) => `<option value="${c.id}" ${c.id === s.configId ? "selected" : ""}>${esc(c.nome)}${c.padrao ? " (padrão)" : ""}</option>`).join("")}</select></label>
    </div>
    <div data-fixwrap="${papel}">${fixNoticeHtml(papel)}</div>
    ${papel === "contratos" ? relationHtml() : ""}
    <div class="preview-table" style="max-height:none"><table class="map-table">
      <thead><tr><th>Campo do sistema</th><th>Coluna da planilha</th><th>Exemplo / situação</th></tr></thead><tbody>${fieldRowsHtml(papel)}</tbody></table></div>
    <label class="check set-default"><input type="checkbox" data-default="${papel}" ${s.definirPadrao ? "checked" : ""}>
      <span><b>Definir como padrão</b><small>Salva esta associação (pelo nome das colunas) e a aplica automaticamente nas próximas importações.</small></span></label>
  </section>`;
}

function renderStepMap() {
  $("#import-body").innerHTML = `${messageHtml()}
    <p style="margin:0 0 12px">Associe os campos do sistema às colunas de cada planilha. Já fizemos isso automaticamente; confira e ajuste o que for preciso.</p>
    ${sides().map(panelHtml).join("")}${optionsHtml()}`;
  foot(`<button type="button" class="btn" id="back">Voltar</button><span class="spacer"></span><button type="button" class="btn btn-primary" id="to-review">Continuar</button>`);
  const body = $("#import-body");
  body.onchange = onBodyChange;
  body.onclick = null;
  $("#back").onclick = () => { imp.step = 1; imp.message = ""; renderImport(); };
  $("#to-review").onclick = () => {
    const problem = mappingProblem();
    if (problem) { imp.message = problem; renderImport(); $("#import-body").scrollTop = 0; return; }
    runPreview();
  };
}

function onFieldChange(select) {
  const s = imp[select.dataset.papel], field = select.dataset.field;
  s.mapping[field] = select.value === "" ? null : +select.value;
  delete s.pendencias[field];
  if (imp.unica && select.dataset.papel === "contratos" && field === "numero_contrato") { renderStepMap(); return; }
  const row = select.closest("tr");
  if (row) { row.classList.remove("needs-fix"); row.querySelector(".sample").innerHTML = exemplos(s, s.mapping[field]); }
  const wrap = $(`[data-fixwrap="${select.dataset.papel}"]`);
  if (wrap) wrap.innerHTML = fixNoticeHtml(select.dataset.papel);
  refreshRelation();
}

async function rereadSide(papel, opts) {
  const s = imp[papel];
  let configId = s.configId, manual = s.manual;
  if ("config" in opts) { manual = opts.config === ""; configId = manual ? null : +opts.config; }
  const header = "header" in opts ? (opts.header === "" || opts.header == null ? null : +opts.header) : s.linhaCabecalho;
  try {
    const d = await api("/api/importacao/planilha", { method: "POST", json: { token: s.token, papel, planilha: opts.sheet ?? s.sheet, config_id: configId, manual, linha_cabecalho: header } });
    Object.assign(s, { sheet: d.planilha, sheets: d.planilhas, columns: d.colunas, total: d.total_linhas, mapping: d.mapeamento,
      pendencias: d.pendencias || {}, configuracao: d.configuracao, configId: d.configuracao?.id ?? null, configs: d.configuracoes, manual,
      linhaCabecalho: d.linha_cabecalho ?? null });
    if (papel === "contratos") imp.chave = d.chave || imp.chave;
    imp.message = "";
  } catch (err) { imp.message = err.message; }
  renderImport();
}

/* --- Pré-visualização --- */
function sidePayload(papel) {
  const s = imp[papel];
  if (!s) return undefined;
  const mapping = { ...s.mapping };
  if (papel === "clientes" && contratosAtivos()) { mapping.valor = null; mapping.mes = null; }
  return { token: s.token, planilha: s.sheet, mapeamento: mapping, linha_cabecalho: s.linhaCabecalho ?? null };
}

const importParams = () => ({
  clientes: sidePayload("clientes"), contratos: sidePayload("contratos"), chave: imp.chave,
  mes_padrao: imp.mesPadrao, substituir_vazios: imp.substituir,
  arquivo_clientes: imp.clientes?.filename, arquivo_contratos: imp.contratos?.filename,
});

async function saveDefaults() {
  for (const papel of sides()) {
    const s = imp[papel];
    if (!s.definirPadrao) continue;
    if (papel === "contratos" && !contratosAtivos()) continue;
    try {
      const { mapeamento } = sidePayload(papel);
      const c = await api("/api/importacao/configuracoes/do-arquivo", { method: "POST",
        json: { papel, token: s.token, planilha: s.sheet, mapeamento, linha_cabecalho: s.linhaCabecalho, chave: papel === "contratos" ? imp.chave : null, config_id: s.configId } });
      Object.assign(s, { configId: c.id, configuracao: { id: c.id, nome: c.nome, padrao: true }, pendencias: {}, manual: false, definirPadrao: false });
      s.configs = s.configs.filter((x) => x.id !== c.id).map((x) => ({ ...x, padrao: false })).concat([{ id: c.id, nome: c.nome, padrao: true }]);
      toast(`Mapeamento da ${ROLE[papel].titulo.toLowerCase()} salvo como padrão (“${c.nome}”).`);
    } catch (err) { toast(err.message, "error"); }
  }
}

async function runPreview() {
  imp.message = ""; imp.step = 3; imp.analysis = null;
  imp.tab = imp.clientes ? "clientes" : "contratos";
  imp.filter = { clientes: "todos", contratos: "todos" };
  renderImport();
  try {
    const analysis = await api("/api/importacao/previa", { method: "POST", json: importParams() });
    await saveDefaults();
    imp.analysis = analysis;
    renderImport();
  } catch (err) {
    imp.step = 2; imp.message = err.message;
    renderImport();
  }
}

/* --- Passo 3: revisão --- */
const STATUS_LABEL = { novo: "Novo", atualizar: "Atualizar", sem_alteracao: "Sem alteração", rejeitado: "Rejeitado" };
const PAY_LABEL = { novo: "Novo", substituido: "Substituído", igual: "Sem alteração" };
const FORMAT_BY_FIELD = { CNPJ: (v) => formatCnpj(v), CEP: (v) => formatCep(v) };
const ROW_LIMIT = 200;

function changeText(c) {
  const fmt = FORMAT_BY_FIELD[c.campo] || ((v) => v);
  const show = (v) => (v == null || v === "" ? "(vazio)" : fmt(String(v)));
  return `${esc(c.campo)}: ${esc(show(c.de))} → ${esc(show(c.para))}`;
}

function detailsHtml(l) {
  return (l.erros || []).map((e) => `<div class="reasons">${esc(e)}</div>`).join("")
    + (l.avisos || []).map((w) => `<div class="warn">${esc(w)}</div>`).join("")
    + (l.mudancas || []).slice(0, 6).map((c) => `<div class="changes">${changeText(c)}</div>`).join("")
    + ((l.mudancas || []).length > 6 ? `<div class="changes">+ ${l.mudancas.length - 6} alteração(ões)</div>` : "");
}

const stat = (cls, n, label) => `<div class="stat ${cls}"><b>${n.toLocaleString("pt-BR")}</b><span>${label}</span></div>`;
const emptyRow = (cols) => `<tr><td colspan="${cols}" class="muted" style="text-align:center;padding:24px">Nada a mostrar nesta lista.</td></tr>`;
const more = (n) => (n > ROW_LIMIT ? `<p class="help">Exibindo as primeiras ${ROW_LIMIT} de ${n} linhas desta lista. Todas as linhas válidas serão importadas.</p>` : "");

function pills(tab, counts) {
  const items = [["todos", "Todas"], ["novo", "Novos"], ["atualizar", "Atualizar"], ["sem_alteracao", "Sem alteração"], ["rejeitado", "Rejeitadas"]];
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  return `<div class="tabs sub">${items.map(([k, label]) => `<button type="button" class="tab ${imp.filter[tab] === k ? "active" : ""}" data-filter="${tab}:${k}">${label} (${k === "todos" ? total : counts[k]})</button>`).join("")}</div>`;
}

function clientsTabHtml(a, r) {
  const rows = a.linhas.filter((l) => imp.filter.clientes === "todos" || l.status === imp.filter.clientes);
  const withValues = !r.tem_contratos;
  const cols = withValues ? 8 : 5;
  const body = rows.slice(0, ROW_LIMIT).map((l) => `<tr><td>${l.linha}</td><td><span class="st ${l.status}">${STATUS_LABEL[l.status]}</span></td>
    <td>${esc(l.codigo || "—")}</td><td>${esc(l.nome || "—")}<div class="muted">${esc(l.cnpj || "")}</div></td>
    ${withValues ? `<td>${l.mes ? esc(monthLabel(l.mes)) : "—"}</td><td class="num">${brl(l.valor_centavos)}</td><td>${l.classificacao ? badge(l.classificacao) : "—"}</td>` : ""}
    <td>${detailsHtml(l)}</td></tr>`).join("");
  return `${pills("clientes", r.linhas_por_status)}<div class="preview-table"><table><thead><tr><th>Linha</th><th>Situação</th><th>Código</th><th>Cliente</th>
    ${withValues ? `<th>Mês</th><th class="num">Valor</th><th>Classif.</th>` : ""}<th>Detalhes</th></tr></thead><tbody>${body || emptyRow(cols)}</tbody></table></div>${more(rows.length)}`;
}

function contractsTabHtml(a, r) {
  const rows = a.contratos_linhas.filter((l) => imp.filter.contratos === "todos" || l.status === imp.filter.contratos);
  const body = rows.slice(0, ROW_LIMIT).map((l) => `<tr><td>${l.linha}</td><td><span class="st ${l.status}">${STATUS_LABEL[l.status]}</span></td>
    <td><b>${esc(l.contrato || "—")}</b><div class="muted">${esc([l.produto, l.situacao].filter(Boolean).join(" · "))}</div></td>
    <td>${esc(l.cliente || "—")}<div class="muted">${esc([l.codigo, l.cnpj].filter(Boolean).join(" · "))}</div></td>
    <td>${l.mes ? esc(monthLabel(l.mes)) : "—"}</td><td class="num">${brl(l.valor_centavos)}</td><td class="num muted">${brl(l.parcela_centavos)}</td>
    <td>${detailsHtml(l)}</td></tr>`).join("");
  return `${pills("contratos", r.contratos_por_status)}<div class="preview-table"><table><thead><tr><th>Linha</th><th>Situação</th><th>Contrato</th><th>Cliente</th>
    <th>Mês</th><th class="num">Valor mensal</th><th class="num">Parcela futuro</th><th>Detalhes</th></tr></thead><tbody>${body || emptyRow(8)}</tbody></table></div>${more(rows.length)}
    <p class="help">“Parcela futuro” é apenas informativa: não entra no valor do mês nem na classificação.</p>`;
}

function totalsTabHtml(a) {
  const body = a.totais.slice(0, ROW_LIMIT).map((t) => `<tr><td>${esc(t.codigo || "—")}</td><td>${esc(t.cliente)}<div class="muted">${esc(t.cnpj || "")}</div></td>
    <td>${esc(monthLabel(t.mes))}</td><td class="num">${t.contratos}</td><td class="num"><b>${brl(t.valor_centavos)}</b></td><td>${badge(t.classificacao)}</td>
    <td><span class="st ${t.status === "novo" ? "novo" : t.status === "substituido" ? "atualizar" : "sem_alteracao"}">${PAY_LABEL[t.status]}</span>
      ${t.status === "substituido" ? `<div class="muted">antes: ${brl(t.anterior_centavos)}</div>` : ""}</td></tr>`).join("");
  return `<p class="help" style="margin-top:0">O valor do mês de cada cliente é a <b>soma dos valores mensais dos seus contratos</b> naquele mês. É esse total que define a classificação.</p>
    <div class="preview-table"><table><thead><tr><th>Código</th><th>Cliente</th><th>Mês</th><th class="num">Contratos</th><th class="num">Total do mês</th><th>Classif.</th><th>Situação</th></tr></thead>
    <tbody>${body || emptyRow(7)}</tbody></table></div>${more(a.totais.length)}`;
}

function inconsTabHtml(a) {
  const body = a.inconsistencias.slice(0, ROW_LIMIT).map((i) => `<tr><td>${i.planilha === "clientes" ? "Clientes" : "Contratos"}</td><td>${i.linha ?? "—"}</td>
    <td><span class="st ${i.tipo === "cliente_sem_contrato" ? "atualizar" : "rejeitado"}">${esc(i.tipo_rotulo)}</span></td>
    <td>${esc(i.identificacao || "—")}</td><td>${esc(i.nome || "—")}</td><td>${esc(i.motivo)}</td></tr>`).join("");
  return `<div class="tabs sub"><span class="muted">Corrija a planilha e importe novamente, ou prossiga apenas com os registros válidos.</span><span class="spacer"></span>
    <button type="button" class="btn btn-sm" id="dl-incons">Baixar relatório (CSV)</button></div>
    <div class="preview-table"><table><thead><tr><th>Planilha</th><th>Linha</th><th>Tipo</th><th>Identificação</th><th>Cliente</th><th>Motivo</th></tr></thead>
    <tbody>${body || emptyRow(6)}</tbody></table></div>${more(a.inconsistencias.length)}`;
}

function renderStepReview() {
  const a = imp.analysis;
  if (!a) {
    $("#import-body").innerHTML = `<p style="padding:40px;text-align:center"><span class="spinner"></span>Relacionando e analisando as planilhas…</p>`;
    return foot("");
  }
  const r = a.resumo, hasC = r.tem_contratos, hasCli = !!imp.clientes;
  const cards = [];
  if (hasCli) cards.push(stat("", r.clientes_lidos, "Clientes encontrados"));
  if (hasC) {
    cards.push(stat("", r.contratos_lidos, "Contratos encontrados"),
      stat("new", r.contratos_relacionados, "Contratos relacionados a clientes"),
      stat(r.contratos_sem_cliente ? "rej" : "", r.contratos_sem_cliente, "Contratos sem cliente correspondente"));
    if (hasCli) cards.push(stat(r.clientes_sem_contrato ? "warn" : "", r.clientes_sem_contrato, "Clientes sem contrato"));
  }
  cards.push(stat(r.duplicados ? "warn" : "", r.duplicados, "Registros duplicados"), stat(r.invalidos ? "rej" : "", r.invalidos, "Informações inválidas"));

  const write = [];
  if (hasCli || r.clientes_novos || r.clientes_atualizados) write.push(`<b>Clientes:</b> ${r.clientes_novos} a cadastrar · ${r.clientes_atualizados} a atualizar · ${r.clientes_sem_alteracao} sem alteração`);
  if (hasC) write.push(`<b>Contratos:</b> ${r.contratos_novos} novos · ${r.contratos_atualizados} a atualizar · ${r.contratos_sem_alteracao} sem alteração`);
  const pay = [];
  if (r.pagamentos_novos) pay.push(`${r.pagamentos_novos} novo(s)`);
  if (r.pagamentos_substituidos) pay.push(`${r.pagamentos_substituidos} substituído(s), não somado(s)`);
  if (r.pagamentos_iguais) pay.push(`${r.pagamentos_iguais} já igual(is)`);
  if (pay.length) write.push(`<b>Valores do mês (${esc(r.meses.map(monthLabel).join(", "))}):</b> ${esc(pay.join(" · "))}`);

  const tabs = [];
  if (hasCli) tabs.push(["clientes", `Clientes (${r.clientes_lidos})`]);
  if (hasC) tabs.push(["contratos", `Contratos (${r.contratos_lidos})`], ["valores", `Valores do mês (${a.totais.length})`]);
  tabs.push(["inconsistencias", `Inconsistências (${a.inconsistencias.length})`]);
  const content = { clientes: () => clientsTabHtml(a, r), contratos: () => contractsTabHtml(a, r), valores: () => totalsTabHtml(a), inconsistencias: () => inconsTabHtml(a) };

  $("#import-body").innerHTML = `${messageHtml()}
    <div class="cards">${cards.join("")}</div>
    <p class="summary-line">${write.join("<br>")}</p>
    ${a.avisos.map((w) => `<div class="notice warn-box">${esc(w)}</div>`).join("")}
    <div class="tabs main-tabs" role="tablist">${tabs.map(([k, label]) => `<button type="button" role="tab" class="tab ${imp.tab === k ? "active" : ""}" data-tab="${k}">${label}</button>`).join("")}</div>
    <div id="tab-content">${content[imp.tab]()}</div>`;

  const body = $("#import-body");
  body.onchange = null;
  body.onclick = (e) => {
    const tab = e.target.closest("[data-tab]"), flt = e.target.closest("[data-filter]");
    if (tab) { imp.tab = tab.dataset.tab; renderStepReview(); }
    if (flt) { const [t, k] = flt.dataset.filter.split(":"); imp.filter[t] = k; renderStepReview(); }
    if (e.target.id === "dl-incons") downloadInconsistencies();
  };

  const canConfirm = r.clientes_novos + r.clientes_atualizados + r.contratos_novos + r.contratos_atualizados > 0;
  foot(`<button type="button" class="btn" id="back">Voltar</button><span class="spacer"></span>
    <span class="muted">${canConfirm ? "Nada é gravado até você confirmar" : "Nada a importar"}</span>
    <button type="button" class="btn btn-primary" id="confirm" ${canConfirm ? "" : "disabled"}>Confirmar importação</button>`);
  $("#back").onclick = () => { imp.step = 2; imp.message = ""; renderImport(); };
  $("#confirm").onclick = confirmImport;
}

async function downloadInconsistencies() {
  try {
    const res = await api("/api/importacao/inconsistencias.csv", { method: "POST", json: importParams(), raw: true });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await res.blob());
    a.download = "inconsistencias-importacao.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (err) { toast(err.message, "error"); }
}

async function confirmImport() {
  const btn = $("#confirm");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner" style="border-top-color:#fff;border-color:rgba(255,255,255,.4)"></span>Importando…`;
  try {
    imp.result = await api("/api/importacao/confirmar", { method: "POST", json: importParams() });
    imp.step = 4;
    renderImport();
  } catch (err) {
    imp.message = err.message;
    renderStepReview();
  }
}

/* --- Passo 4: concluído --- */
function renderStepDone() {
  const r = imp.result;
  const items = [["Clientes cadastrados", r.clientes_novos], ["Clientes atualizados", r.clientes_atualizados], ["Clientes sem alteração", r.clientes_sem_alteracao]];
  if (r.tem_contratos) items.push(["Contratos gravados", r.contratos_gravados]);
  items.push(["Valores mensais gravados", r.pagamentos_gravados], ["Linhas com inconsistência", r.linhas_rejeitadas]);
  $("#import-body").innerHTML = `<div class="done-box"><div class="check-mark">✓</div><h2>Importação concluída</h2>
    <p class="muted">Os dados já estão disponíveis na tabela de clientes.</p>
    <ul>${items.map(([k, v]) => `<li><span>${k}</span><b>${v}</b></li>`).join("")}</ul></div>`;
  $("#import-body").onchange = null; $("#import-body").onclick = null;
  foot(`<span class="spacer"></span><button type="button" class="btn btn-primary" id="finish">Concluir</button>`);
  $("#finish").onclick = async () => {
    $("#dlg-import").close();
    if (r.meses?.length) state.mes = r.meses[r.meses.length - 1];
    state.page = 1;
    await refreshAll();
    toast(`Importação concluída: ${r.clientes_novos} cliente(s) cadastrado(s), ${r.clientes_atualizados} atualizado(s)${r.tem_contratos ? `, ${r.contratos_gravados} contrato(s) gravado(s)` : ""}.`);
  };
}

/* ========================================================================
   Configurações de importação (mapeamentos salvos)
   ======================================================================== */
const cfgState = { clientes: [], contratos: [] };
const CFG_TEXT = {
  clientes: { titulo: "Planilha de clientes", desc: "Mapeamento do arquivo de cadastro de clientes." },
  contratos: { titulo: "Planilha de contratos", desc: "Mapeamento do arquivo de contratos e do campo que o relaciona aos clientes." },
};
let editingConfig = null;
const fmtDateTime = (iso) => (iso ? new Date(iso).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" }) : "");

async function loadConfigs() {
  try {
    Object.assign(cfgState, await api("/api/importacao/configuracoes"));
    renderConfigs();
  } catch (err) { toast(err.message, "error"); }
}

function renderConfigs() {
  $("#config-lists").innerHTML = ["clientes", "contratos"].map((tipo) => {
    const items = cfgState[tipo], t = CFG_TEXT[tipo];
    const labels = Object.fromEntries((tipo === "contratos" ? state.meta.campos_contratos : state.meta.campos).map((f) => [f.chave, f.rotulo]));
    const current = items.find((c) => c.padrao);
    const rows = items.map((c) => `<tr data-id="${c.id}">
      <td><b>${esc(c.nome)}</b> ${c.padrao ? `<span class="badge-default">Padrão</span>` : ""}
        <details><summary>Ver associações</summary><ul class="assoc">${Object.entries(c.mapeamento).filter(([, v]) => v)
          .map(([k, v]) => `<li><span>${esc(labels[k] || k)}</span><b>${esc(v)}</b></li>`).join("")}</ul></details></td>
      <td>${c.campos_associados}</td>${tipo === "contratos" ? `<td>${c.chave === "codigo" ? "Código do cliente" : "CNPJ"}</td>` : ""}
      <td class="muted">${esc(fmtDateTime(c.atualizado_em))}</td>
      <td class="col-actions"><div class="cfg-actions">
        ${c.padrao ? "" : `<button type="button" class="btn btn-sm" data-cfg="default">Definir como padrão</button>`}
        <button type="button" class="btn btn-sm" data-cfg="edit">Editar</button>
        <button type="button" class="btn btn-sm btn-quiet-danger" data-cfg="delete">Excluir</button></div></td></tr>`).join("");
    return `<section class="card cfg-card"><div class="cfg-head"><div><h2>${t.titulo}</h2><p class="muted">${t.desc}</p></div>
      <div class="cfg-current">${current ? `Padrão atual: <b>${esc(current.nome)}</b>` : "Nenhum padrão definido"}</div></div>
      ${items.length ? `<div class="table-wrap"><table><thead><tr><th>Configuração</th><th>Campos associados</th>${tipo === "contratos" ? "<th>Relaciona por</th>" : ""}<th>Atualizada em</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<div class="empty"><strong>Nenhum mapeamento salvo</strong>Ao importar, marque “Definir como padrão” na tela de associação de colunas para criar um.</div>`}</section>`;
  }).join("");
}

function openConfigEdit(c) {
  editingConfig = c;
  $("#config-error").hidden = true;
  $("#config-title").textContent = `Editar mapeamento — ${c.tipo === "contratos" ? "planilha de contratos" : "planilha de clientes"}`;
  $("#cfg-nome").value = c.nome;
  $("#cfg-padrao").checked = c.padrao;
  $("#cfg-padrao").disabled = c.padrao;
  $("#cfg-chave-box").hidden = c.tipo !== "contratos";
  $("#cfg-chave").value = c.chave || "cnpj";
  $("#cfg-heads").innerHTML = c.cabecalhos.map((h) => `<option value="${esc(h)}">`).join("");
  const fields = c.tipo === "contratos" ? state.meta.campos_contratos : state.meta.campos;
  $("#cfg-fields").innerHTML = fields.map((f) => `<tr><td>${esc(f.rotulo)}</td>
    <td><input data-cfg-field="${f.chave}" list="cfg-heads" value="${esc(c.mapeamento[f.chave] || "")}" maxlength="120" aria-label="Coluna para ${esc(f.rotulo)}"></td></tr>`).join("");
  $("#dlg-config").showModal();
  $("#cfg-nome").focus();
}

function wireConfigs() {
  $("#config-lists").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-cfg]");
    if (!btn) return;
    const id = +btn.closest("tr").dataset.id;
    const config = [...cfgState.clientes, ...cfgState.contratos].find((c) => c.id === id);
    if (btn.dataset.cfg === "edit") return openConfigEdit(config);
    if (btn.dataset.cfg === "default") {
      try {
        await api(`/api/importacao/configuracoes/${id}/padrao`, { method: "POST" });
        toast(`“${config.nome}” agora é o padrão da ${CFG_TEXT[config.tipo].titulo.toLowerCase()}.`);
        await loadConfigs();
      } catch (err) { toast(err.message, "error"); }
      return;
    }
    confirmDialog({
      title: "Excluir mapeamento",
      html: `Deseja excluir o mapeamento <b>${esc(config.nome)}</b>?${config.padrao ? " Ele é o padrão atual: as próximas importações voltarão a associar as colunas automaticamente até que você defina outro padrão." : ""} Os clientes e contratos já importados não são afetados.`,
      action: async () => {
        await api(`/api/importacao/configuracoes/${id}`, { method: "DELETE" });
        toast("Mapeamento excluído.");
        await loadConfigs();
      },
    });
  });

  $("#form-config").addEventListener("submit", async (e) => {
    e.preventDefault();
    const mapeamento = {};
    $$("#cfg-fields input[data-cfg-field]").forEach((i) => { mapeamento[i.dataset.cfgField] = i.value.trim() || null; });
    const btn = $("#cfg-save");
    btn.disabled = true;
    try {
      await api(`/api/importacao/configuracoes/${editingConfig.id}`, { method: "PUT",
        json: { nome: $("#cfg-nome").value, mapeamento, chave: editingConfig.tipo === "contratos" ? $("#cfg-chave").value : null, padrao: $("#cfg-padrao").checked } });
      $("#dlg-config").close();
      toast("Mapeamento atualizado.");
      await loadConfigs();
    } catch (err) {
      const box = $("#config-error");
      box.textContent = err.message; box.hidden = false;
    } finally { btn.disabled = false; }
  });
}

/* ========================================================================
   Curva ABC (Configurações)
   Limites editáveis e salvos no banco. Todos os campos são texto em R$ (ex.: 1.000,00).
   ======================================================================== */
const CURVA_ORDEM = ["A+", "A", "B", "C"];
const curvaState = { faixas: [], rascunho: null };

async function loadCurva() {
  try {
    const d = await api("/api/configuracoes/curva-abc");
    curvaState.faixas = d.faixas;
    curvaState.rascunho = null;
    renderCurva();
  } catch (err) { toast(err.message, "error"); }
}

function renderCurva(errors = {}, message = "") {
  const body = $("#curva-body");
  if (!body) return;
  const valores = curvaState.rascunho || curvaState.faixas;
  const get = (label, campo) => (valores.find((f) => f.classificacao === label) || {})[campo] ?? "";
  const field = (label, campo, text) => {
    const err = errors[`${label}.${campo}`];
    return `<div class="money"><span>R$</span><input inputmode="decimal" autocomplete="off" data-faixa="${label}" data-campo="${campo}"
      aria-label="${campo === "minimo" ? "Mínimo" : "Máximo"} da classificação ${label}" value="${esc(text)}" ${err ? 'class="invalid"' : ""}></div>
      <small class="err">${esc(err || "")}</small>`;
  };
  const rows = CURVA_ORDEM.map((label) => `<tr>
      <td><span class="dot cls-${esc(label)}"></span> <b>${esc(label)}</b></td>
      <td class="curva-campo">${field(label, "minimo", get(label, "minimo"))}</td>
      <td class="curva-campo">${label === "A+" ? `<span class="muted">sem limite</span>` : field(label, "maximo", get(label, "maximo"))}</td>
    </tr>`).join("");
  body.innerHTML = `${errors.geral || errors.erro ? `<div class="notice">${esc(errors.geral || errors.erro)}</div>` : ""}
    ${message ? `<div class="notice">${esc(message)}</div>` : ""}
    <p class="help">Cada faixa começa no mínimo indicado e termina 1 centavo antes do mínimo da faixa acima.
      A faixa C deve começar em R$ 0,00 e a A+ não tem máximo. Não pode haver sobreposição nem valores sem classificação.</p>
    <div class="preview-table" style="max-height:none"><table class="map-table curva-table">
      <thead><tr><th>Classificação</th><th>Mínimo (R$)</th><th>Máximo (R$)</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <div class="curva-actions"><button type="button" class="btn btn-primary" id="curva-save">Salvar Curva ABC</button></div>`;
  $("#curva-save").onclick = salvarCurva;
}

async function salvarCurva() {
  const body = $("#curva-body");
  const valorDe = (label, campo) => body.querySelector(`input[data-faixa="${label}"][data-campo="${campo}"]`)?.value ?? "";
  const faixas = CURVA_ORDEM.map((label) => ({ classificacao: label, minimo: valorDe(label, "minimo"), maximo: label === "A+" ? "" : valorDe(label, "maximo") }));
  curvaState.rascunho = faixas.map((f) => ({ classificacao: f.classificacao, minimo: f.minimo, maximo: f.maximo }));
  const btn = $("#curva-save");
  btn.disabled = true;
  try {
    const d = await api("/api/configuracoes/curva-abc", { method: "PUT", json: { faixas } });
    curvaState.faixas = d.faixas;
    curvaState.rascunho = null;
    toast(d.mensagem);
    await refreshAll();            // legenda, filtros e classificações exibidas na lista
    renderCurva();
  } catch (err) {
    renderCurva(err.fields || {}, err.message);
  } finally {
    const again = $("#curva-save");
    if (again) again.disabled = false;
  }
}

/* ========================================================================
   Início
   ======================================================================== */
/* Usuário logado (Supabase Auth) e botão Sair. Sem login configurado (uso local), a área fica oculta. */
async function wireSession() {
  try {
    const s = await api("/api/auth/sessao");
    if (!s.login || !s.email) return;
    $("#usuario-email").textContent = s.email;
    $("#sidebar-user").hidden = false;
    $("#btn-sair").onclick = async () => {
      $("#btn-sair").disabled = true;
      try { await api("/api/auth/logout", { method: "POST" }); } catch { /* sai mesmo assim */ }
      location.replace("/login");
    };
  } catch { /* 401 já redireciona para o login */ }
}

document.addEventListener("DOMContentLoaded", async () => {
  wireSession();
  wireMain();
  wireClientForm();
  wireDelete();
  wireConfigs();
  try {
    await refreshAll();
  } catch (err) {
    toast(err.message, "error");
  }
  routeFromHash();
  window.addEventListener("hashchange", routeFromHash);
});
