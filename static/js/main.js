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

// ── Upload ───────────────────────────────────────────────────────────────────

const SESSION_ID = crypto.randomUUID();
const uploadedFiles = {};    // rowId → { tipo, file_id, nome_original, done: bool }
let docRowCount = 0;

// Documentos com vaga fixa (cada tipo aparece uma única vez)
const DOCS_FIXOS = [
  { tipo: 'RG',       label: 'RG',                          obrigatorio: true },
  { tipo: 'CPF',      label: 'CPF (se não estiver no RG)',  obrigatorio: false },
  { tipo: 'CTPS',     label: 'Carteira de Trabalho (CTPS)', obrigatorio: true },
  { tipo: 'Holerite', label: 'Holerite / Contracheque',     obrigatorio: true },
  { tipo: 'PPP',      label: 'PPP',                         obrigatorio: false },
];

const TOTAL_OBRIGATORIOS = DOCS_FIXOS.filter(d => d.obrigatorio).length;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Cria uma linha de upload. Se def for null, é um documento extra (tipo "Outro", removível).
function addDocRow(def) {
  const list = document.getElementById('upload-list');
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
      if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100));
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

// ── ZapSign ──────────────────────────────────────────────────────────────────

let zapSignDocToken = null;

async function iniciarAssinatura() {
  const nome = document.getElementById('nome_completo')?.value?.trim();
  const cpf = document.getElementById('cpf')?.value?.trim();
  const checkbox = document.getElementById('procuracao_aceite');

  if (!nome || nome.length < 3) {
    alert('Por favor, preencha o nome completo antes de assinar.');
    return;
  }
  if (!cpf || cpf.replace(/\D/g, '').length !== 11) {
    alert('Por favor, preencha o CPF antes de assinar.');
    return;
  }
  if (checkbox && !checkbox.checked) {
    alert('Por favor, leia e aceite os termos da procuração antes de assinar.');
    return;
  }

  const btn = document.getElementById('btn-iniciar-assinatura');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Gerando documento…';

  try {
    const resp = await fetch('/api/zapsign/criar-documento', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nome, cpf }),
    });

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

let zapSignPollTimer = null;

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

function updateSubmitButton() {
  const btn = document.getElementById('btn-submit');
  if (!btn) return;

  const hasSignature = !!zapSignDocToken || !!document.getElementById('zapsign_doc_token')?.value;

  const enviados = new Set(
    Object.values(uploadedFiles).filter(f => f.done).map(f => f.tipo)
  );
  const obrigatoriosEnviados = DOCS_FIXOS
    .filter(d => d.obrigatorio)
    .filter(d => enviados.has(d.tipo)).length;
  const todosObrigatorios = obrigatoriosEnviados === TOTAL_OBRIGATORIOS;

  // Atualiza o contador de progresso
  const msg = document.getElementById('upload-progress-msg');
  if (msg) {
    msg.textContent = `${obrigatoriosEnviados} de ${TOTAL_OBRIGATORIOS} documentos obrigatórios enviados`;
    msg.classList.toggle('completo', todosObrigatorios);
  }

  btn.disabled = !(hasSignature && todosObrigatorios);
}

function docsObrigatoriosFaltando() {
  const enviados = new Set(
    Object.values(uploadedFiles).filter(f => f.done).map(f => f.tipo)
  );
  return DOCS_FIXOS.filter(d => d.obrigatorio && !enviados.has(d.tipo)).map(d => d.label);
}

async function submitForm(e) {
  e.preventDefault();

  const sessionField = document.getElementById('session_id_field');
  const csrfField = document.getElementById('csrf_token_field');

  if (sessionField) sessionField.value = SESSION_ID;
  if (csrfField) csrfField.value = getCookie('csrf_token');

  const requiredFields = ['nome_completo', 'cpf', 'telefone', 'email', 'hospital', 'cargo', 'tempo_servico'];
  let valid = true;
  for (const id of requiredFields) {
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
    return;
  }

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
  btn.innerHTML = '<span class="spinner"></span>Enviando cadastro…';

  const form = e.target;
  const fd = new FormData(form);

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
    btn.innerHTML = 'Enviar cadastro';
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
    showToast('Status atualizado com sucesso!');
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

  // Vagas fixas de documentos (cada tipo uma única vez)
  if (document.getElementById('upload-list')) {
    DOCS_FIXOS.forEach(def => addDocRow(def));
  }

  // Form submit
  document.getElementById('cadastro-form')?.addEventListener('submit', submitForm);

  // Adicionar documento extra (tipo "Outro", removível)
  document.getElementById('add-doc-btn')?.addEventListener('click', () => addDocRow(null));

  updateSubmitButton();
});
