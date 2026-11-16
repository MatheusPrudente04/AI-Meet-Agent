// content.js — Ponte entre a página (page.evaluate) e o service worker.
//
// Anti "message port closed": NÃO espera o callback do sendMessage (a porta
// frágil). Despacha o comando ao SW e responde imediatamente à página com
// { dispatched: true }. A confirmação REAL de que a captura iniciou é feita
// pelo recorder via __meetAgentStatus (storage), não por esta resposta.

window.addEventListener('message', (event) => {
  if (event.source !== window) return;
  if (!event.data || event.data.type !== 'MEET_AGENT_CAPTURE') return;

  const { action, session, endpoint, chunkMs, token } = event.data;

  // Dispara ao SW. Passamos um callback vazio só para silenciar lastError;
  // não dependemos da resposta dele.
  try {
    chrome.runtime.sendMessage(
      { type: 'FROM_PAGE', action, session, endpoint, chunkMs, token },
      () => { void chrome.runtime.lastError; } // absorve "port closed" sem quebrar
    );
  } catch (e) {
    console.error('[meet-agent] sendMessage falhou:', e);
  }

  // Responde JÁ à página — o comando foi despachado. A confirmação de
  // gravação vem por outro canal (status/storage), não por aqui.
  window.postMessage(
    { type: 'MEET_AGENT_RESPONSE', action, response: { ok: true, dispatched: true } },
    '*'
  );
});

console.log('[meet-agent content] pronto');