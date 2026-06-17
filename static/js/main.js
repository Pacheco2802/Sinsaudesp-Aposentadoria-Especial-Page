'use strict';

// ── Masks ────────────────────────────────────────────────────────────────────

function maskCPF(e) {
  let v = e.target.value.replace(/\D/g, '').slice(0, 11);
  if (v.length > 9) v = v.replace(/(\d{3})(\d{3})(\d{3})(\d{1,2})/, '$1.$2.$3-$4');
  else if (v.length > 6) v = v.replace(/(\d{3})(\d{3})(\d{1,3})/, '$1.$2.$3');
  else if (v.length > 3) v = v.replace(/(\d{3})(\d{1,3})/, '$1.$2');
  e.target.value = v;
}

function maskPhone(e) {
  let v = e.target.value.replace(/\D/g, '').slice(0, 11);
  if (v.length > 10) v = v.replace(/(\d{2})(\d{5})(\d{4})/, '($1) $2-$3');
  else if (v.length > 6) v = v.replace(/(\d{2})(\d{4,5})(\d{0,4})/, '($1) $2-$3');
  else if (v.length > 2) v = v.replace(/(\d{2})(\d{0,5})/, '($1) $2');
  e.target.value = v;
}

function maskCEP(e) {
  let v = e.target.value.replace(/\D/g, '').slice(0, 8);
  if (v.length > 5) v = v.replace(/(\d{5})(\d{1,3})/, '$1-$2');
  e.target.value = v;
}

// ── Page detection ───────────────────────────────────────────────────────────

const ETAPA2_TOKEN = document.body?.dataset?.etapa2Token || null;
const IS_ETAPA2 = !!ETAPA2_TOKEN;

// ── Upload ───────────────────────────────────────────────────────────────────

const SESSION_ID = crypto.randomUUID();
const uploadedFiles = {};    // rowId → { tipo, file_id, nome_original, done: bool }
let docRowCount = 0;

// Vagas fixas por página
const DOCS_ETAPA1 = [
  { tipo: 'CNIS', label: 'Extrato CNIS (Meu INSS)', obrigatorio: false },
];

const DOCS_ETAPA2 = [
  { tipo: 'RG',       label: 'RG',                          obrigatorio: true },
  { tipo: 'CPF',      label: 'CPF (se não estiver no RG)',  obrigatorio: false },
  { tipo: 'CTPS',     label: 'Carteira de Trabalho (CTPS)', obrigatorio: true },
  { tipo: 'Holerite', label: 'Holerite / Contracheque',     obrigatorio: true },
  { tipo: 'PPP',      label: 'PPP',                         obrigatorio: false },
];

const DOCS_FIXOS = IS_ETAPA2 ? DOCS_ETAPA2 : DOCS_ETAPA1;
const TOTAL_OBRIGATORIOS = DOCS_FIXOS.filter(d => d.obrigatorio).length;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function getUploadList() {
  return document.getElementById('upload-list-etapa2') || document.getElementById('upload-list');
}

// Cria uma linha de upload. Se def for null, é um documento extra (tipo "Outro", removível).
function addDocRow(def) {
  const list = getUploadList();
  if (!list) return;

  const rowId = `row-${++docRowCount}`;
  const tipo = def ? def.tipo : 'Outro';
  const label = def ? def.label : 'Documento extra';
  const obrigatorio = def ? def.obrigatorio : false;
  const removivel = !def;

  const badge = obrigatorio
    ? '<span class="doc-badge obrig">Obrigatório</span>'
    : '<span class="doc-badge opc">Opcional</span>';

  const html = `
  <div class="upload-row" id="${rowId}" data-tipo="${tipo}" data-obrig="${obrigatorio}">
    <div class="upload-row-header">
      <div class="doc-label">
        <span class="doc-name">${escapeHtml(label)}</span>
        ${badge}
      </div>
      <div class="upload-btn-wrap">
        <label class="upload-label-btn" for="file-${rowId}">
          📎 Selecionar arquivo
        </label>
        <input type="file" class="upload-input" id="file-${rowId}"
               accept=".pdf,.jpg,.jpeg,.png"
               onchange="handleFileSelect('${rowId}')">
        ${removivel ? `<button type="button" class="remove-doc-btn" onclick="removeRow('${rowId}')">✕</button>` : ''}
      </div>
    </div>
    <div class="upload-progress"><div class="upload-progress-bar" id="prog-${rowId}"></div></div>
    <div class="upload-status" id="status-${rowId}">Nenhum arquivo selecionado</div>
  </div>`;

  list.insertAdjacentHTML('beforeend', html);
}

function removeRow(rowId) {
  const row = document.getElementById(rowId);
  if (row) row.remove();
  delete uploadedFiles[rowId];
  updateSubmitButton();
}

async function handleFileSelect(rowId) {
  const fileInput = document.getElementById(`file-${rowId}`);
  const statusEl = document.getElementById(`status-${rowId}`);
  const progressWrap = document.querySelector(`#${rowId} .upload-progress`);
  const progressBar = document.getElementById(`prog-${rowId}`);
  const row = document.getElementById(rowId);

  const file = fileInput.files[0];
  if (!file) return;

  if (file.size > 10 * 1024 * 1024) {
    statusEl.textContent = '❌ Arquivo muito grande (máx 10MB)';
    statusEl.className = 'upload-status error';
    return;
  }

  const tipo = row.dataset.tipo;
  statusEl.textContent = 'Enviando…';
  statusEl.className = 'upload-status uploading';
  progressWrap.style.display = 'block';
  progressBar.style.width = '0%';

  const fd = new FormData();
  fd.append('file', file);
  fd.append('tipo', tipo);
  fd.append('session_id', SESSION_ID);

  try {
    const result = await uploadXHR('/api/upload', fd, (pct) => {
      progressBar.style.width = pct + '%';
    });

    uploadedFiles[rowId] = { tipo, file_id: result.file_id, nome_original: result.nome_original, done: true };
    statusEl.textContent = `✅ ${file.name}`;
    statusEl.className = 'upload-status done';
    row.classList.add('uploaded');
  } catch (err) {
    statusEl.textContent = `❌ ${err.message || 'Erro no upload'}`;
    statusEl.className = 'upload-status error';
    progressWrap.style.display = 'none';
  }

  updateSubmitButton();
}

function uploadXHR(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);

    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    });

    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); }
        catch { resolve({}); }
      } else {
        let msg = 'Erro no servidor';
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
        reject(new Error(msg));
      }
    });

    xhr.addEventListener('error', () => reject(new Error('Falha na conexão')));
    xhr.send(formData);
  });
}

// ── ViaCEP (busca de endereço) ───────────────────────────────────────────────

async function buscarCEP() {
  const cepInput = document.getElementById('cep');
  if (!cepInput) return;
  const digits = cepInput.value.replace(/\D/g, '');
  if (digits.length !== 8) return;

  try {
    const resp = await fetch(`https://viacep.com.br/ws/${digits}/json/`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.erro) return;

    const setIfEmptyOrAuto = (id, value) => {
      const el = document.getElementById(id);
      if (el && value) el.value = value;
    };
    setIfEmptyOrAuto('logradouro', data.logradouro);
    setIfEmptyOrAuto('bairro', data.bairro);
    setIfEmptyOrAuto('cidade', data.localidade);
    if (data.uf) {
      const ufEl = document.getElementById('uf');
      if (ufEl) ufEl.value = data.uf;
    }
  } catch {
    // silencioso: usuário preenche manualmente
  }
}

// ── Agenda de horários ───────────────────────────────────────────────────────

let horarioSelecionado = null;

async function carregarHorarios() {
  const dataInput = document.getElementById('agendamento_data');
  const grid = document.getElementById('horarios-grid');
  const hiddenHora = document.getElementById('agendamento_hora');
  if (!dataInput || !grid) return;

  horarioSelecionado = null;
  if (hiddenHora) hiddenHora.value = '';
  updateSubmitButton();

  const data = dataInput.value;
  if (!data) {
    grid.innerHTML = '<span class="horarios-hint">Escolha uma data para ver os horários disponíveis</span>';
    return;
  }

  const diaSemana = new Date(data + 'T12:00:00').getDay();
  if (diaSemana === 0 || diaSemana === 6) {
    grid.innerHTML = '<span class="horarios-hint" style="color:#c62828;">Atendimentos apenas em dias úteis. Escolha outra data.</span>';
    return;
  }

  grid.innerHTML = '<span class="horarios-hint">Carregando horários…</span>';

  try {
    const resp = await fetch(`/api/agenda/horarios?data=${encodeURIComponent(data)}`);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      grid.innerHTML = `<span class="horarios-hint" style="color:#c62828;">${escapeHtml(err.detail || 'Não foi possível carregar os horários')}</span>`;
      return;
    }
    const payload = await resp.json();

    if (payload.bloqueado) {
      const motivo = payload.motivo ? ` (${escapeHtml(payload.motivo)})` : '';
      grid.innerHTML = `<span class="horarios-hint" style="color:#c62828;">Sem atendimento nesta data${motivo}. Escolha outro dia.</span>`;
      return;
    }

    const { horarios } = payload;
    const livres = horarios.filter(h => h.disponivel);
    if (!livres.length) {
      grid.innerHTML = '<span class="horarios-hint" style="color:#c62828;">Nenhum horário disponível nesta data. Escolha outro dia.</span>';
      return;
    }

    grid.innerHTML = horarios.map(h => `
      <button type="button" class="horario-btn" data-hora="${h.hora}"
              ${h.disponivel ? '' : 'disabled'}
              onclick="selecionarHorario('${h.hora}')">
        ${h.hora}
      </button>`).join('');
  } catch {
    grid.innerHTML = '<span class="horarios-hint" style="color:#c62828;">Erro ao carregar horários. Tente novamente.</span>';
  }
}

function selecionarHorario(hora) {
  horarioSelecionado = hora;
  const hiddenHora = document.getElementById('agendamento_hora');
  if (hiddenHora) hiddenHora.value = hora;

  document.querySelectorAll('.horario-btn').forEach(btn => {
    btn.classList.toggle('selecionado', btn.dataset.hora === hora);
  });
  updateSubmitButton();
}

// ── ZapSign (somente etapa 2) ────────────────────────────────────────────────

let zapSignDocToken = null;
let zapSignPollTimer = null;

async function iniciarAssinatura() {
  const checkbox = document.getElementById('procuracao_aceite');
  if (checkbox && !checkbox.checked) {
    alert('Por favor, leia e aceite os termos da procuração antes de assinar.');
    return;
  }

  const btn = document.getElementById('btn-iniciar-assinatura');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Gerando documento…';

  try {
    const resp = await fetch(`/api/etapa2/${ETAPA2_TOKEN}/zapsign`, { method: 'POST' });

    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || 'Erro ao gerar documento');
    }

    const { signer_token, doc_token } = await resp.json();

    document.getElementById('zapsign_doc_token').value = doc_token;
    document.getElementById('zapsign-iframe').src =
      `https://app.zapsign.com.br/verificar/${signer_token}`;

    document.getElementById('zapsign-modal-overlay').classList.add('open');
    iniciarVerificacaoAssinatura(doc_token);
  } catch (err) {
    alert(`Erro ao criar documento de assinatura: ${err.message}`);
    btn.disabled = false;
    btn.textContent = 'Assinar digitalmente';
  }
}

function iniciarVerificacaoAssinatura(docToken) {
  pararVerificacaoAssinatura();
  // Consulta a ZapSign a cada 4s para detectar quando o documento foi assinado
  zapSignPollTimer = setInterval(() => checarStatusAssinatura(docToken, false), 4000);
}

function pararVerificacaoAssinatura() {
  if (zapSignPollTimer) {
    clearInterval(zapSignPollTimer);
    zapSignPollTimer = null;
  }
}

async function checarStatusAssinatura(docToken, manual) {
  try {
    const resp = await fetch(`/api/zapsign/status?doc_token=${encodeURIComponent(docToken)}`);
    if (!resp.ok) throw new Error('Falha ao consultar status');
    const { signed } = await resp.json();
    if (signed) {
      finalizarAssinatura();
    } else if (manual) {
      const msg = document.getElementById('zapsign-status-msg');
      if (msg) msg.textContent = '⏳ Ainda não detectamos a assinatura. Conclua a assinatura e tente de novo.';
    }
  } catch (err) {
    if (manual) {
      const msg = document.getElementById('zapsign-status-msg');
      if (msg) msg.textContent = '⚠️ Não foi possível verificar agora. Tente novamente em instantes.';
    }
  }
}

function verificarAssinaturaManual() {
  const docToken = document.getElementById('zapsign_doc_token').value;
  if (!docToken) return;
  const msg = document.getElementById('zapsign-status-msg');
  if (msg) msg.textContent = '🔄 Verificando…';
  checarStatusAssinatura(docToken, true);
}

function finalizarAssinatura() {
  pararVerificacaoAssinatura();
  zapSignDocToken = document.getElementById('zapsign_doc_token').value;
  document.getElementById('zapsign-modal-overlay').classList.remove('open');

  const confirmEl = document.getElementById('assinatura-confirmada');
  if (confirmEl) confirmEl.classList.add('show');

  const btn = document.getElementById('btn-iniciar-assinatura');
  if (btn) { btn.disabled = true; btn.innerHTML = '✓ Documento assinado'; }

  updateSubmitButton();
}

function fecharModalZapSign() {
  pararVerificacaoAssinatura();
  document.getElementById('zapsign-modal-overlay').classList.remove('open');
  if (!zapSignDocToken) {
    const btn = document.getElementById('btn-iniciar-assinatura');
    if (btn) { btn.disabled = false; btn.textContent = 'Assinar digitalmente'; }
  }
}

// Caminho rápido: se a ZapSign enviar o aviso de assinatura concluída, finaliza na hora
window.addEventListener('message', (e) => {
  if (e.origin !== 'https://app.zapsign.com.br') return;

  const isSignedEvent =
    e.data === 'zs-doc-signed' ||
    (typeof e.data === 'object' && (e.data?.event === 'zs-doc-signed' || e.data?.type === 'signed'));

  if (isSignedEvent) {
    finalizarAssinatura();
  }
});

// ── Submit validation ─────────────────────────────────────────────────────────

function docsObrigatoriosFaltando() {
  const enviados = new Set(
    Object.values(uploadedFiles).filter(f => f.done).map(f => f.tipo)
  );
  return DOCS_FIXOS.filter(d => d.obrigatorio && !enviados.has(d.tipo)).map(d => d.label);
}

function updateSubmitButton() {
  const btn = document.getElementById('btn-submit');
  if (!btn) return;

  const enviados = new Set(
    Object.values(uploadedFiles).filter(f => f.done).map(f => f.tipo)
  );
  const obrigatoriosEnviados = DOCS_FIXOS
    .filter(d => d.obrigatorio)
    .filter(d => enviados.has(d.tipo)).length;
  const todosObrigatorios = obrigatoriosEnviados === TOTAL_OBRIGATORIOS;

  const msg = document.getElementById('upload-progress-msg');
  if (msg && TOTAL_OBRIGATORIOS > 0) {
    msg.textContent = `${obrigatoriosEnviados} de ${TOTAL_OBRIGATORIOS} documentos obrigatórios enviados`;
    msg.classList.toggle('completo', todosObrigatorios);
  }

  if (IS_ETAPA2) {
    const hasSignature = !!zapSignDocToken || !!document.getElementById('zapsign_doc_token')?.value;
    btn.disabled = !(hasSignature && todosObrigatorios);
  } else {
    // Etapa 1: exige horário de atendimento escolhido (demais campos validados no envio)
    btn.disabled = !horarioSelecionado;
  }
}

function validarCamposObrigatorios(ids) {
  let valid = true;
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (!el.value.trim()) {
      el.classList.add('error');
      valid = false;
    } else {
      el.classList.remove('error');
    }
  }
  if (!valid) {
    const firstErr = document.querySelector('.error');
    if (firstErr) firstErr.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  return valid;
}

// ── Submit: Etapa 1 (cadastro + agendamento) ─────────────────────────────────

async function submitCadastro(e) {
  e.preventDefault();

  const sessionField = document.getElementById('session_id_field');
  const csrfField = document.getElementById('csrf_token_field');
  if (sessionField) sessionField.value = SESSION_ID;
  if (csrfField) csrfField.value = getCookie('csrf_token');

  const requiredFields = [
    'nome_completo', 'cpf', 'rg', 'data_nascimento', 'telefone', 'email',
    'estado_civil', 'nacionalidade',
    'cep', 'logradouro', 'numero', 'bairro', 'cidade', 'uf',
    'hospital', 'cargo', 'tempo_servico', 'agendamento_data',
  ];
  if (!validarCamposObrigatorios(requiredFields)) return;

  if (!document.querySelector('input[name="filiado"]:checked')) {
    alert('Informe se você é filiado(a) ao SinSaúdeSP.');
    return;
  }
  if (!document.querySelector('input[name="modalidade_atendimento"]:checked')) {
    alert('Escolha a modalidade do atendimento (online ou presencial).');
    return;
  }
  if (!horarioSelecionado) {
    alert('Escolha um horário para o atendimento.');
    return;
  }

  const btn = document.getElementById('btn-submit');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Enviando cadastro…';

  const fd = new FormData(e.target);

  try {
    const resp = await fetch('/api/cadastro', { method: 'POST', body: fd });
    const data = await resp.json();

    if (!resp.ok) {
      throw new Error(data.detail || 'Erro ao enviar cadastro');
    }

    if (data.redirect) {
      window.location.href = data.redirect;
    }
  } catch (err) {
    alert(`Erro: ${err.message}`);
    btn.disabled = false;
    btn.innerHTML = 'Confirmar cadastro e agendamento';
    // Recarrega horários: o escolhido pode ter sido ocupado
    carregarHorarios();
    updateSubmitButton();
  }
}

// ── Submit: Etapa 2 (documentação + procuração) ──────────────────────────────

async function submitEtapa2(e) {
  e.preventDefault();

  const sessionField = document.getElementById('session_id_field');
  const csrfField = document.getElementById('csrf_token_field');
  if (sessionField) sessionField.value = SESSION_ID;
  if (csrfField) csrfField.value = getCookie('csrf_token');

  const faltando = docsObrigatoriosFaltando();
  if (faltando.length > 0) {
    alert('Faltam documentos obrigatórios:\n\n• ' + faltando.join('\n• '));
    return;
  }

  if (!zapSignDocToken && !document.getElementById('zapsign_doc_token')?.value) {
    alert('Por favor, assine o documento de procuração antes de enviar.');
    return;
  }

  const btn = document.getElementById('btn-submit');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Enviando documentação…';

  const fd = new FormData(e.target);

  try {
    const resp = await fetch(`/api/etapa2/${ETAPA2_TOKEN}`, { method: 'POST', body: fd });
    const data = await resp.json();

    if (!resp.ok) {
      throw new Error(data.detail || 'Erro ao enviar documentação');
    }

    if (data.redirect) {
      window.location.href = data.redirect;
    }
  } catch (err) {
    alert(`Erro: ${err.message}`);
    btn.disabled = false;
    btn.innerHTML = 'Enviar documentação';
    updateSubmitButton();
  }
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function getCookie(name) {
  const match = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
  return match ? decodeURIComponent(match[1]) : '';
}

// ── Admin: status update ──────────────────────────────────────────────────────

async function updateStatus(cadastroId, selectEl) {
  const status = selectEl.value;
  try {
    const resp = await fetch(`/admin/cadastro/${cadastroId}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    if (!resp.ok) throw new Error('Erro ao atualizar status');
    const badge = document.getElementById('status-badge');
    if (badge) {
      badge.className = `badge badge-${status}`;
      badge.textContent = status.replace('_', ' ');
    }
    showToast('Status atualizado!');
  } catch (err) {
    alert(`Erro: ${err.message}`);
  }
}

async function saveNota(cadastroId) {
  const nota = document.getElementById('nota-input')?.value || '';
  try {
    const resp = await fetch(`/admin/cadastro/${cadastroId}/nota`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nota }),
    });
    if (!resp.ok) throw new Error('Erro ao salvar nota');
    showToast('Nota salva!');
  } catch (err) {
    alert(`Erro: ${err.message}`);
  }
}

async function liberarEtapa2(cadastroId) {
  if (!confirm('Liberar a etapa 2 (documentação + procuração) para este cadastro?\nUm e-mail com o link será enviado à pessoa.')) return;
  try {
    const resp = await fetch(`/admin/cadastro/${cadastroId}/liberar-etapa2`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Erro ao liberar etapa 2');

    if (data.email_enviado) {
      showToast('Etapa 2 liberada! E-mail enviado.');
      setTimeout(() => location.reload(), 1200);
    } else {
      alert(
        'A etapa 2 foi liberada, mas o E-MAIL NÃO PÔDE SER ENVIADO.\n\n' +
        'Verifique as variáveis SMTP no Railway (logs do serviço mostram o motivo).\n\n' +
        'Enquanto isso, copie o link a seguir e envie manualmente para a pessoa (ex: WhatsApp).'
      );
      window.prompt('Link da etapa 2 — Ctrl+C para copiar:', data.link);
      location.reload();
    }
  } catch (err) {
    alert(`Erro: ${err.message}`);
  }
}

async function excluirCadastro(cadastroId) {
  if (!confirm('Excluir este cadastro PERMANENTEMENTE?\n\nIsso apaga os dados e todos os documentos enviados. Esta ação não pode ser desfeita.')) return;
  try {
    const resp = await fetch(`/admin/cadastro/${cadastroId}/excluir`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Erro ao excluir cadastro');
    showToast('Cadastro excluído.');
    setTimeout(() => { window.location.href = '/admin'; }, 1000);
  } catch (err) {
    alert(`Erro: ${err.message}`);
  }
}

function copiarLinkEtapa2(token) {
  const link = `${window.location.origin}/etapa2/${token}`;
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(link)
      .then(() => showToast('Link copiado!'))
      .catch(() => window.prompt('Link da etapa 2 — Ctrl+C para copiar:', link));
  } else {
    window.prompt('Link da etapa 2 — Ctrl+C para copiar:', link);
  }
}

function showToast(msg) {
  const t = document.createElement('div');
  t.textContent = msg;
  Object.assign(t.style, {
    position: 'fixed', bottom: '24px', right: '24px',
    background: '#2E7D32', color: 'white',
    padding: '12px 20px', borderRadius: '8px',
    fontFamily: 'Inter, sans-serif', fontSize: '0.9rem',
    boxShadow: '0 4px 12px rgba(0,0,0,0.2)', zIndex: 9999,
    animation: 'fadeIn 0.2s ease',
  });
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // Masks
  document.getElementById('cpf')?.addEventListener('input', maskCPF);
  document.getElementById('telefone')?.addEventListener('input', maskPhone);
  const cepEl = document.getElementById('cep');
  if (cepEl) {
    cepEl.addEventListener('input', maskCEP);
    cepEl.addEventListener('blur', buscarCEP);
  }

  // Pré-seleciona "É filiado?" se vier na URL
  const filiadoParam = new URLSearchParams(location.search).get('filiado');
  if (filiadoParam === 'sim') {
    const r = document.querySelector('input[name="filiado"][value="true"]');
    if (r) r.checked = true;
  } else if (filiadoParam === 'nao') {
    const r = document.querySelector('input[name="filiado"][value="false"]');
    if (r) r.checked = true;
  }

  // Vagas de documentos
  if (getUploadList()) {
    DOCS_FIXOS.forEach(def => addDocRow(def));
  }

  // Documento extra (apenas etapa 2)
  document.getElementById('add-doc-btn')?.addEventListener('click', () => addDocRow(null));

  // Agenda (etapa 1): limites de data e carregamento de horários
  const dataInput = document.getElementById('agendamento_data');
  if (dataInput) {
    const amanha = new Date();
    amanha.setDate(amanha.getDate() + 1);
    const limite = new Date();
    limite.setDate(limite.getDate() + 60);
    dataInput.min = amanha.toISOString().split('T')[0];
    dataInput.max = limite.toISOString().split('T')[0];
    dataInput.addEventListener('change', carregarHorarios);
  }

  // Form submit
  document.getElementById('cadastro-form')?.addEventListener('submit', submitCadastro);
  document.getElementById('etapa2-form')?.addEventListener('submit', submitEtapa2);

  updateSubmitButton();
});

// ── Lead modal (landing page only) ───────────────────────────────────────────
(function () {
  const overlay = document.getElementById('lead-modal-overlay');
  if (!overlay) return;

  function openModal() { overlay.classList.add('open'); document.getElementById('lead-email')?.focus(); }
  function closeModal() { overlay.classList.remove('open'); }

  // Intercepta todos os links /cadastro
  document.querySelectorAll('a[href="/cadastro"]').forEach(function (link) {
    link.addEventListener('click', function (e) {
      e.preventDefault();
      openModal();
    });
  });

  document.getElementById('lead-form')?.addEventListener('submit', async function (e) {
    e.preventDefault();
    const email = document.getElementById('lead-email').value.trim();
    const termos = document.getElementById('lead-termos').checked;
    const marketing = document.getElementById('lead-marketing').checked;
    const errEl = document.getElementById('lead-error');
    const btn = document.getElementById('lead-btn');

    errEl.textContent = '';
    if (!termos) {
      errEl.textContent = 'Você precisa aceitar os termos para continuar.';
      return;
    }

    btn.disabled = true;
    btn.textContent = 'Aguarde…';
    try {
      const resp = await fetch('/api/lead', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, consentimento_termos: termos, consentimento_marketing: marketing }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || 'Erro ao registrar e-mail');
      sessionStorage.setItem('lead_id', data.lead_id);
      window.location.href = '/cadastro';
    } catch (err) {
      errEl.textContent = err.message;
      btn.disabled = false;
      btn.textContent = 'Continuar para o cadastro';
    }
  });
})();

// ── Preenche lead_id no formulário de cadastro ───────────────────────────────
(function () {
  const f = document.getElementById('lead_id_field');
  if (f) f.value = sessionStorage.getItem('lead_id') || '';
})();

// ── Analytics de sessão (cadastro page only) ─────────────────────────────────
(function () {
  if (!document.getElementById('cadastro-form')) return;

  const LEAD_ID = sessionStorage.getItem('lead_id') || null;
  const SESSION_ID = (function () {
    let s = sessionStorage.getItem('_sess_id');
    if (!s) { s = crypto.randomUUID(); sessionStorage.setItem('_sess_id', s); }
    return s;
  })();
  const SECTIONS_SEEN = new Set();
  const FIELD_SENT = new Set();
  const FIELD_FOCUS_TIME = {};
  let LAST_SECTION = null;
  let FORM_SUBMITTED = false;

  function sendEvento(tipo, payload) {
    const body = JSON.stringify({ session_id: SESSION_ID, lead_id: LEAD_ID, tipo: tipo, payload: payload || null });
    fetch('/api/analytics/evento', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body,
      keepalive: true,
    }).catch(function () {});
  }

  // Visibilidade das seções
  const observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting && entry.intersectionRatio >= 0.3) {
        const secao = entry.target.dataset.secao;
        if (secao && !SECTIONS_SEEN.has(secao)) {
          SECTIONS_SEEN.add(secao);
          LAST_SECTION = secao;
          sendEvento('secao_vista', { secao: secao });
        }
      }
    });
  }, { threshold: 0.3 });

  document.querySelectorAll('.form-card[data-secao]').forEach(function (card) {
    observer.observe(card);
  });

  // Tempo por campo
  document.querySelectorAll('input, select, textarea').forEach(function (el) {
    const fname = el.name || el.id;
    if (!fname) return;
    el.addEventListener('focus', function () { FIELD_FOCUS_TIME[fname] = Date.now(); });
    el.addEventListener('blur', function () {
      if (FIELD_FOCUS_TIME[fname] && el.value && !FIELD_SENT.has(fname)) {
        FIELD_SENT.add(fname);
        sendEvento('campo_blur', { campo: fname, tempo_ms: Date.now() - FIELD_FOCUS_TIME[fname] });
      }
      delete FIELD_FOCUS_TIME[fname];
    });
  });

  // Abandono
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden' && !FORM_SUBMITTED) {
      const reqs = document.querySelectorAll('input[required], select[required]');
      const filled = Array.from(reqs).filter(function (el) { return el.value.trim(); }).length;
      sendEvento('abandono', {
        ultima_secao: LAST_SECTION,
        progresso_pct: reqs.length ? Math.round(filled / reqs.length * 100) : 0,
      });
    }
  });

  // Marca como submetido
  document.getElementById('cadastro-form').addEventListener('submit', function () {
    FORM_SUBMITTED = true;
  }, { capture: true });
})();
