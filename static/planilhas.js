"use strict";
/* ==========================================================================
   Leitura de planilhas no navegador (.xlsx, .xls XML do Excel 2003 e .csv)
   e envio das linhas ao servidor em lotes.

   Por quê: no Cloudflare Workers (plano gratuito) a CPU por requisição é limitada. Abrir o Excel no
   servidor custava mais do que o limite. Aqui o navegador faz a leitura e o servidor recebe só as linhas.

   Os valores seguem exatamente a leitura que o servidor fazia (openpyxl / ElementTree / csv do Python),
   para que a importação dê o mesmo resultado: números inteiros e decimais, datas (como datetime),
   texto sem alteração, linhas numeradas como no Excel. O teste tests/test_planilhas_navegador.py roda
   este arquivo no Node e compara, célula a célula, com a leitura do servidor.

   Sem bibliotecas externas: ZIP com DecompressionStream e XML com um leitor simples (sem DTD nem
   entidades externas: um arquivo com DOCTYPE é recusado, como no servidor).
   ========================================================================== */
(function (raiz) {
  const SEGURO = Number.MAX_SAFE_INTEGER;

  class ErroPlanilha extends Error {}   // mensagem pronta para o usuário (não tenta a leitura no servidor)

  /* ---------------------------------------------------------------- XML mínimo */
  const ENT = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
  function decodificar(texto) {
    return texto.replace(/\r\n?/g, "\n").replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[a-zA-Z]+);/g, (m, e) => {
      if (e[0] === "#") {
        const cp = e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
        return String.fromCodePoint(cp);
      }
      if (e in ENT) return ENT[e];
      throw new ErroPlanilha("XML inválido");
    });
  }
  function atributos(texto) {
    const out = {};
    const re = /([^\s=\/]+)\s*=\s*("([^"]*)"|'([^']*)')/g;
    let m;
    while ((m = re.exec(texto))) out[m[1]] = decodificar(m[3] !== undefined ? m[3] : m[4]);
    return out;
  }
  /* Árvore de elementos: {nome, attrs, filhos, texto (antes do 1º filho, como o .text do ElementTree), conteudo} */
  function lerXml(texto) {
    if (/<!DOCTYPE|<!ENTITY/i.test(texto)) throw new ErroPlanilha("XML com DOCTYPE");
    const raizNo = { nome: "#raiz", attrs: {}, filhos: [], partes: [] };
    const pilha = [raizNo];
    const re = /<!--[\s\S]*?-->|<!\[CDATA\[([\s\S]*?)\]\]>|<\?[\s\S]*?\?>|<(\/?)([^\s>\/]+)((?:[^>"'\/]|"[^"]*"|'[^']*'|\/(?!>))*)(\/?)>|([^<]+)/g;
    let m;
    while ((m = re.exec(texto))) {
      const atual = pilha[pilha.length - 1];
      if (m[1] !== undefined) { atual.partes.push({ t: m[1] }); continue; }
      if (m[6] !== undefined) { atual.partes.push({ t: decodificar(m[6]) }); continue; }
      if (!m[3]) continue;   // comentário ou instrução de processamento
      if (m[2] === "/") {
        if (pilha.length < 2 || atual.nome !== m[3]) throw new ErroPlanilha("XML malformado");
        pilha.pop();
        continue;
      }
      const no = { nome: m[3], attrs: atributos(m[4]), filhos: [], partes: [] };
      atual.filhos.push(no);
      atual.partes.push({ no });
      if (!m[5]) pilha.push(no);
    }
    if (pilha.length !== 1) throw new ErroPlanilha("XML malformado");
    return raizNo.filhos[0];
  }
  const local = (nome) => nome.slice(nome.indexOf(":") + 1);
  const prefixo = (nome) => (nome.includes(":") ? nome.slice(0, nome.indexOf(":")) : "");
  /* texto antes do primeiro elemento filho (igual ao .text do ElementTree); null se vazio */
  function textoInicial(no) {
    let s = "";
    for (const p of no.partes) { if (p.no) break; s += p.t; }
    return s === "" ? null : s;
  }
  /* todo o texto dos descendentes */
  function textoTotal(no) {
    let s = "";
    for (const p of no.partes) s += p.no ? textoTotal(p.no) : p.t;
    return s;
  }
  const filhos = (no, nomeLocal) => no.filhos.filter((f) => local(f.nome) === nomeLocal);
  const filho = (no, nomeLocal) => no.filhos.find((f) => local(f.nome) === nomeLocal) || null;

  /* ---------------------------------------------------------------- números e datas (como o openpyxl) */
  function numero(texto) {
    // openpyxl: com "." ou expoente é float; senão int. Fora da faixa segura do JavaScript, vai como texto.
    const flutuante = /[.eE]/.test(texto);
    const v = Number(texto);
    if (!Number.isFinite(v) || Math.abs(v) > SEGURO) return flutuante ? { $f: texto } : { $i: texto };
    return v;
  }
  const pad = (n, t = 2) => String(n).padStart(t, "0");
  function iso(ms) {
    const d = new Date(ms);
    return `${pad(d.getUTCFullYear(), 4)}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  }
  const EPOCA_1900 = Date.UTC(1899, 11, 30), EPOCA_1904 = Date.UTC(1904, 0, 1), DIA = 86400000;
  function deExcel(v, data1904, comoDuracao) {
    if (comoDuracao) {   // formatos [h]:mm:ss -> como o str(timedelta) do Python
      const totalMs = Math.round(v * DIA);
      const dias = Math.floor(totalMs / DIA), resto = totalMs - dias * DIA;
      const s = Math.floor(resto / 1000);
      const hms = `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
      return dias ? `${dias} day${Math.abs(dias) === 1 ? "" : "s"}, ${hms}` : hms;
    }
    let dia = Math.floor(v);
    const fracMs = Math.round((v - dia) * DIA);
    if (v >= 0 && v < 1 && fracMs < DIA) {   // só horário: o openpyxl devolve um time (texto HH:MM:SS)
      const s = Math.floor(fracMs / 1000);
      return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
    }
    if (!data1904 && v > 0 && v < 60) dia += 1;   // dia 29/02/1900 inexistente do Excel
    return { $d: iso((data1904 ? EPOCA_1904 : EPOCA_1900) + dia * DIA + fracMs) };
  }
  const DATAS_NATIVAS = new Set([14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47]);
  const DURACOES_NATIVAS = new Set([46]);
  function formatoDeData(codigo) {
    if (codigo == null) return false;
    const f = codigo.split(";")[0].replace(/".*?"|\[(?!hh?\]|mm?\]|ss?\])[^\]]*\]/g, "");
    return /(^|[^_\\])[dmhysDMHYS]/.test(f);
  }
  const formatoDeDuracao = (codigo) => /\[hh?\](:mm(:ss(\.0*)?)?)?|\[mm?\](:ss(\.0*)?)?|\[ss?\](\.0*)?/i.test(codigo || "");

  /* ---------------------------------------------------------------- ZIP (.xlsx) */
  async function inflar(bytes) {
    const fluxo = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    return new Uint8Array(await new Response(fluxo).arrayBuffer());
  }
  function lerZip(bytes) {
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let fim = -1;
    for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
      if (dv.getUint32(i, true) === 0x06054b50) { fim = i; break; }
    }
    if (fim < 0) throw new ErroPlanilha("Não foi possível abrir o arquivo. Verifique se é uma planilha .xlsx válida.");
    const total = dv.getUint16(fim + 10, true);
    let p = dv.getUint32(fim + 16, true);
    const entradas = {};
    const nomeDec = new TextDecoder("utf-8");
    for (let k = 0; k < total; k++) {
      if (dv.getUint32(p, true) !== 0x02014b50) throw new ErroPlanilha("Não foi possível abrir o arquivo. Verifique se é uma planilha .xlsx válida.");
      const metodo = dv.getUint16(p + 10, true), tamanho = dv.getUint32(p + 20, true);
      const nLen = dv.getUint16(p + 28, true), xLen = dv.getUint16(p + 30, true), cLen = dv.getUint16(p + 32, true);
      const local0 = dv.getUint32(p + 42, true);
      const nome = nomeDec.decode(bytes.subarray(p + 46, p + 46 + nLen));
      entradas[nome] = { metodo, tamanho, local0 };
      p += 46 + nLen + xLen + cLen;
    }
    return {
      tem: (nome) => nome in entradas,
      async texto(nome) {
        const e = entradas[nome];
        if (!e) return null;
        const inicio = e.local0 + 30 + dv.getUint16(e.local0 + 26, true) + dv.getUint16(e.local0 + 28, true);
        const dados = bytes.subarray(inicio, inicio + e.tamanho);
        const cru = e.metodo === 0 ? dados : e.metodo === 8 ? await inflar(dados) : null;
        if (!cru) throw new ErroPlanilha("Não foi possível abrir o arquivo. Verifique se é uma planilha .xlsx válida.");
        return new TextDecoder("utf-8").decode(cru);
      },
    };
  }
  function caminho(alvo) {
    if (alvo.startsWith("/")) return alvo.slice(1);
    const partes = ("xl/" + alvo).split("/"), out = [];
    for (const s of partes) { if (s === "..") out.pop(); else if (s !== ".") out.push(s); }
    return out.join("/");
  }
  const colunaDe = (ref) => { let n = 0; for (const ch of ref.replace(/[0-9$]/g, "").toUpperCase()) n = n * 26 + ch.charCodeAt(0) - 64; return n; };
  const textoRico = (no) => no.filhos.filter((f) => ["t", "r"].includes(local(f.nome)))
    .map((f) => (local(f.nome) === "t" ? textoTotal(f) : (filho(f, "t") ? textoTotal(filho(f, "t")) : ""))).join("");

  async function lerXlsx(bytes) {
    const zip = lerZip(bytes);
    if (!zip.tem("xl/workbook.xml")) throw new ErroPlanilha("Não foi possível abrir o arquivo. Verifique se é uma planilha .xlsx válida.");
    const wb = lerXml(await zip.texto("xl/workbook.xml"));
    const rels = {};
    const relsXml = await zip.texto("xl/_rels/workbook.xml.rels");
    if (relsXml) for (const r of lerXml(relsXml).filhos) rels[r.attrs.Id] = r.attrs.Target;
    const pr = filho(wb, "workbookPr");
    const data1904 = !!pr && ["1", "true"].includes(String(pr.attrs.date1904 || "").toLowerCase());
    const compartilhadas = [];
    const ss = await zip.texto("xl/sharedStrings.xml");
    if (ss) for (const si of lerXml(ss).filhos) compartilhadas.push(textoRico(si));
    const datas = new Set(), duracoes = new Set();
    const st = await zip.texto("xl/styles.xml");
    if (st) {
      const estilos = lerXml(st);
      const proprios = {};
      const nf = filho(estilos, "numFmts");
      if (nf) for (const f of nf.filhos) proprios[+f.attrs.numFmtId] = f.attrs.formatCode;
      const xfs = filho(estilos, "cellXfs");
      (xfs ? xfs.filhos : []).forEach((xf, i) => {
        const id = +(xf.attrs.numFmtId || 0);
        const codigo = id in proprios ? proprios[id] : null;
        if (codigo != null ? formatoDeData(codigo) : DATAS_NATIVAS.has(id)) datas.add(i);
        if (codigo != null ? formatoDeDuracao(codigo) : DURACOES_NATIVAS.has(id)) duracoes.add(i);
      });
    }
    const folhas = filho(wb, "sheets") ? filho(wb, "sheets").filhos : [];
    const planilhas = [];
    for (const s of folhas) {
      const relId = Object.entries(s.attrs).find(([k]) => local(k) === "id" && prefixo(k))?.[1];
      const alvo = rels[relId] ? caminho(rels[relId]) : null;
      if (!alvo || !alvo.includes("worksheets/")) continue;   // abas de gráfico não são planilhas
      planilhas.push({ nome: s.attrs.name, estado: s.attrs.state || "visible", alvo });
    }
    const visiveis = planilhas.filter((p) => p.estado === "visible");
    const usadas = visiveis.length ? visiveis : planilhas;
    const abas = [];
    for (const p of usadas) {
      const xml = await zip.texto(p.alvo);
      const dados = xml ? filho(lerXml(xml), "sheetData") : null;
      const linhas = [];
      let numLinha = 0;
      for (const row of dados ? filhos(dados, "row") : []) {
        numLinha = row.attrs.r ? +row.attrs.r : numLinha + 1;
        const celulas = [];
        let col = 0;
        for (const c of filhos(row, "c")) {
          col = c.attrs.r ? colunaDe(c.attrs.r) : col + 1;
          const tipo = c.attrs.t || "n", estilo = +(c.attrs.s || 0);
          const vNo = filho(c, "v");
          let v = vNo ? (textoTotal(vNo) || null) : null;
          if (tipo === "inlineStr") { const is = filho(c, "is"); v = is ? textoRico(is) : null; }
          else if (v !== null) {
            if (tipo === "n") {
              const n = numero(v);
              v = datas.has(estilo) && typeof n === "number" ? deExcel(n, data1904, duracoes.has(estilo)) : n;
            } else if (tipo === "s") v = compartilhadas[+v];
            else if (tipo === "b") v = !!+v;
            else if (tipo === "d") v = { $d: v };
          }
          while (celulas.length < col - 1) celulas.push(null);
          celulas[col - 1] = v;
        }
        while (linhas.length && linhas[linhas.length - 1][0] < numLinha - 1) {   // linhas vazias no meio
          linhas.push([linhas[linhas.length - 1][0] + 1, []]);
        }
        if (!linhas.length) for (let n = 1; n < numLinha; n++) linhas.push([n, []]);
        linhas.push([numLinha, celulas]);
      }
      abas.push(linhas);
    }
    return { planilhas: usadas.map((p) => p.nome), abas };
  }

  /* ---------------------------------------------------------------- .xls (XML do Excel 2003) */
  const NS_SS = "urn:schemas-microsoft-com:office:spreadsheet";
  const OLE = [0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1];
  /* Respeita a codificação declarada no XML (como o ElementTree), ex.: encoding="ISO-8859-1". */
  function textoXml(bytes) {
    if (bytes[0] === 0xff && bytes[1] === 0xfe) return new TextDecoder("utf-16le").decode(bytes);
    if (bytes[0] === 0xfe && bytes[1] === 0xff) return new TextDecoder("utf-16be").decode(bytes);
    const inicio = new TextDecoder("latin1").decode(bytes.subarray(0, 200));
    const m = /^\s*<\?xml[^>]*encoding\s*=\s*["']([A-Za-z0-9._-]+)["']/.exec(inicio);
    let rotulo = m ? m[1].toLowerCase() : "utf-8";
    try { return new TextDecoder(rotulo, { fatal: rotulo === "utf-8" }).decode(bytes); }
    catch { throw new ErroPlanilha("XML inválido"); }
  }

  function lerXls(bytes) {
    if (OLE.every((b, i) => bytes[i] === b)) {
      throw new ErroPlanilha("Este .xls é o formato binário antigo do Excel, que não é aceito. Abra no Excel e salve como .xlsx ou CSV.");
    }
    let raizNo;
    try {
      raizNo = lerXml(textoXml(bytes));
    } catch {
      throw new ErroPlanilha("Não foi possível ler o arquivo .xls. Verifique se ele não está corrompido ou salve como .xlsx ou CSV.");
    }
    const ns = {};
    for (const [k, v] of Object.entries(raizNo ? raizNo.attrs : {})) {
      if (k === "xmlns") ns[""] = v; else if (k.startsWith("xmlns:")) ns[k.slice(6)] = v;
    }
    const doSS = (nome) => ns[prefixo(nome)] === NS_SS;
    if (!raizNo || local(raizNo.nome) !== "Workbook" || !doSS(raizNo.nome)) {
      throw new ErroPlanilha("O arquivo .xls não está em um formato de planilha reconhecido. Salve como .xlsx ou CSV.");
    }
    const sub = (no, nome) => no.filhos.filter((f) => local(f.nome) === nome && doSS(f.nome));
    const attr = (no, nome) => { for (const [k, v] of Object.entries(no.attrs)) if (local(k) === nome && prefixo(k) && ns[prefixo(k)] === NS_SS) return v; return null; };
    const folhas = sub(raizNo, "Worksheet");
    if (!folhas.length) throw new ErroPlanilha("A planilha está vazia.");
    const nomes = folhas.map((w, i) => attr(w, "Name") || `Planilha ${i + 1}`);
    const abas = folhas.map((w) => {
      const tabela = sub(w, "Table")[0];
      const linhas = [];
      let numLinha = 0;
      for (const row of tabela ? sub(tabela, "Row") : []) {
        const idx = attr(row, "Index");
        numLinha = idx ? +idx : numLinha + 1;
        const valores = [];
        let col = 0;
        for (const cell of sub(row, "Cell")) {
          const cidx = attr(cell, "Index");
          col = cidx ? +cidx : col + 1;
          while (valores.length < col - 1) valores.push(null);
          valores.push(valorXls(sub(cell, "Data")[0], attr));
        }
        linhas.push([numLinha, valores]);
      }
      return linhas;
    });
    return { planilhas: nomes, abas };
  }
  function valorXls(no, attr) {
    if (!no) return null;
    const texto = textoInicial(no);
    if (texto === null) return null;
    const tipo = attr(no, "Type");
    if (tipo === "Number") {   // Python: float(texto); int se for inteiro
      const v = Number(texto.trim());
      if (!/^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$/.test(texto) || !Number.isFinite(v)) return { $x: texto };
      return Math.abs(v) > SEGURO ? { $x: texto } : v;
    }
    if (tipo === "DateTime") return { $d: texto };
    if (tipo === "Boolean") return ["1", "true", "True"].includes(texto.trim());
    return texto;
  }

  /* ---------------------------------------------------------------- CSV (como o csv do Python) */
  function decodificarTexto(bytes) {
    try { return new TextDecoder("utf-8", { fatal: true }).decode(bytes); }   // remove o BOM, como utf-8-sig
    catch { return new TextDecoder("windows-1252").decode(bytes); }
  }
  function lerCsv(bytes) {
    const texto = decodificarTexto(bytes);
    const primeira = texto.split(/\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/).find((l) => l.trim()) || "";
    const candidatos = [";", ",", "\t", "|"];
    let sep = ";";
    if (candidatos.some((d) => primeira.includes(d))) {
      let melhor = -1;
      for (const d of candidatos) { const n = primeira.split(d).length - 1; if (n > melhor) { melhor = n; sep = d; } }
    }
    const linhas = [];
    let i = 0, linhaFisica = 0;
    const N = texto.length;
    while (i < N) {
      const campos = [];
      let campo = "", emAspas = false, inicioCampo = true, fimRegistro = false;
      while (!fimRegistro) {
        if (i >= N) {   // fim do arquivo (aspas sem fechar: o csv do Python aceita o campo como está)
          if (!/[\r\n]$/.test(texto)) linhaFisica++;   // última linha sem quebra também conta
          campos.push(campo);
          break;
        }
        const ch = texto[i];
        if (emAspas) {
          if (ch === '"') {
            if (texto[i + 1] === '"') { campo += '"'; i += 2; continue; }
            emAspas = false; i++; continue;
          }
          if (ch === "\r" || ch === "\n") {
            campo += ch; i++;
            if (ch === "\r" && texto[i] === "\n") { campo += "\n"; i++; }
            linhaFisica++;
            continue;
          }
          campo += ch; i++; continue;
        }
        if (ch === '"' && inicioCampo) { emAspas = true; inicioCampo = false; i++; continue; }
        if (ch === sep) { campos.push(campo); campo = ""; inicioCampo = true; i++; continue; }
        if (ch === "\r" || ch === "\n") {
          i++;
          if (ch === "\r" && texto[i] === "\n") i++;
          linhaFisica++;
          if (campos.length || campo !== "" || !inicioCampo) campos.push(campo);
          fimRegistro = true;
          continue;
        }
        campo += ch; inicioCampo = false; i++;
      }
      linhas.push([linhaFisica, campos]);
    }
    return { planilhas: [], abas: [linhas] };
  }

  /* ---------------------------------------------------------------- API pública */
  async function ler(arquivo) {
    const nome = arquivo.name || "";
    const ext = (nome.match(/\.[^.]+$/) || [""])[0].toLowerCase();
    if (![".xlsx", ".csv", ".xls"].includes(ext)) {
      throw new ErroPlanilha("Formato não aceito. Envie um arquivo Excel (.xlsx), CSV ou .xls (XML do Excel 2003).");
    }
    if (arquivo.size > 25 * 1024 * 1024) throw new ErroPlanilha("Arquivo muito grande (limite de 25 MB).");
    // {name, bytes: Uint8Array} nos testes; File no navegador (File.bytes() é um método, não os dados).
    const bytes = arquivo.bytes instanceof Uint8Array ? arquivo.bytes : new Uint8Array(await arquivo.arrayBuffer());
    const lido = ext === ".csv" ? lerCsv(bytes) : ext === ".xls" ? lerXls(bytes) : await lerXlsx(bytes);
    return { arquivo: nome, ...lido };
  }

  /* Divide as linhas em lotes de até ~800 KB / 4.000 linhas e envia em sequência.
     `post(url, json)` deve devolver a resposta JSON (ou lançar o erro). */
  async function enviar(post, lido, extras, aoProgredir) {
    const lotes = [];
    lido.abas.forEach((linhas, aba) => {
      let atual = [], tamanho = 0;
      for (const linha of linhas) {
        const t = JSON.stringify(linha).length + 1;
        if (atual.length && (tamanho + t > 800000 || atual.length >= 4000)) { lotes.push({ aba, linhas: atual }); atual = []; tamanho = 0; }
        atual.push(linha); tamanho += t;
      }
      lotes.push({ aba, linhas: atual });
    });
    let token = null, resposta = null;
    for (let i = 0; i < lotes.length; i++) {
      const final = i === lotes.length - 1;
      const corpo = { ...lotes[i], final };
      if (token) corpo.token = token; else Object.assign(corpo, { arquivo: lido.arquivo, planilhas: lido.planilhas });
      if (final) Object.assign(corpo, extras || {});
      resposta = await post("/api/importacao/lote", corpo);
      token = resposta.token;
      if (aoProgredir) aoProgredir(i + 1, lotes.length);
    }
    return resposta;
  }

  const api = { ler, enviar, ErroPlanilha, _interno: { lerCsv, lerXls, lerXlsx, lerXml, deExcel, formatoDeData } };
  raiz.Planilhas = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
