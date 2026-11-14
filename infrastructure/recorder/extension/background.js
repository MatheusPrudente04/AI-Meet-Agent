/**
 * background.js — Service worker da extensão de captura (tabCapture MV3).
 *
 * Estratégia anti "message port closed":
 *   O comando de START chega via content.js (FROM_PAGE) — isso satisfaz a
 *   regra de activeTab do tabCapture (sender.tab.id). Mas NÃO dependemos da
 *   porta de resposta desse sendMessage (que é frágil em MV3 com mensagens
 *   aninhadas SW→offscreen). Em vez disso, gravamos o resultado em
 *   chrome.storage.local, e o recorder lê via __meetAgentStatus (sw.evaluate),
 *   que é um canal que NÃO exige contexto de aba e sempre funciona.
 */

let creating = null;

async function ensureOffscreen() {
  const existing = await chrome.runstime.getContexts({
    contextTypes: ['OFFSCREEN_DOCUMENT'],
  });
  if (existing.length > 0) return;
  if (creating) { await creating; return; }
  creating = chrome.offscreen.createDocument({
    url: 'offscreen.html',
    reasons: ['USER_MEDIA'],
    justification: 'Gravar o áudio da aba do Google Meet para transcrição.',
  });
  await creating;
  creating = null;
}

async function startCapture({ tabId, session, endpoint, chunkMs }) {
  // Marca "iniciando" para o recorder saber que o comando foi recebido.
  await chrome.storage.local.set({
    captureStatus: { recording: false, chunks: 0, finalized: false, error: null, phase: 'starting' },
  });

  let targetTabId = tabId;
  if (!targetTabId) {
    const tabs = await chrome.tabs.query({ url: 'https://meet.google.com/*' });
    if (!tabs.length) throw new Error('Nenhuma aba do Google Meet encontrada');
    targetTabId = tabs[0].id;
  }

  // content.js está rodando na aba → Chrome libera tabCapture para essa aba.
  // sender.tab.id (passado como tabId) é a credencial de aba exigida.
  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId });

  await ensureOffscreen();

  // Notifica o offscreen (broadcast). Não seguramos a porta do FROM_PAGE por isto.
  await chrome.runtime.sendMessage({
    target: 'offscreen',
    type: 'OFFSCREEN_START',
    streamId,
    session,
    endpoint,
    chunkMs: chunkMs || 2000,
  });

  // CONFIRMAÇÃO via storage — é isto que o recorder lê (canal confiável).
  await chrome.storage.local.set({
    captureStatus: { recording: true, chunks: 0, finalized: false, error: null, phase: 'recording' },
    lastStart: { ok: true, tabId: targetTabId, ts: Date.now() },
  });

  return { ok: true, tabId: targetTabId, streamId };
}

async function stopCapture() {
  try {
    await chrome.runtime.sendMessage({ target: 'offscreen', type: 'OFFSCREEN_STOP' });
  } catch (_) { /* offscreen pode já ter fechado */ }
  return { ok: true };
}

// Roda startCapture e PERSISTE qualquer erro no storage (o recorder lê de lá).
async function startCaptureSafe(args) {
  try {
    return await startCapture(args);
  } catch (e) {
    await chrome.storage.local.set({
      captureStatus: { recording: false, chunks: 0, finalized: false, error: String(e), phase: 'error' },
      lastStart: { ok: false, error: String(e), ts: Date.now() },
    });
    return { ok: false, error: String(e) };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.target === 'offscreen') return false;

  if (msg.type === 'FROM_PAGE') {
    const tabId = sender.tab && sender.tab.id;
    if (msg.action === 'START') {
      // Dispara o start. Tentamos responder pela porta (best-effort), mas o
      // recorder NÃO depende disso — ele confirma via storage/__meetAgentStatus.
      startCaptureSafe({ tabId, session: msg.session, endpoint: msg.endpoint, chunkMs: msg.chunkMs })
        .then((r) => { try { sendResponse(r); } catch (_) {} })
        .catch(() => {});
      return true; // mantém a porta se ela sobreviver; se fechar, storage cobre
    }
    if (msg.action === 'STOP') {
      stopCapture()
        .then((r) => { try { sendResponse(r); } catch (_) {} })
        .catch(() => {});
      return true;
    }
    return false;
  }

  if (msg.type === 'CAPTURE_STATUS') {
    chrome.storage.local.get('captureStatus').then((s) =>
      sendResponse(s.captureStatus || { recording: false, chunks: 0 })
    );
    return true;
  }

  return false;
});

// Status via sw.evaluate — canal confiável, não exige contexto de aba.
globalThis.__meetAgentStop = () => stopCapture();
globalThis.__meetAgentStatus = async () => {
  const s = await chrome.storage.local.get('captureStatus');
  return s.captureStatus || { recording: false, chunks: 0 };
};