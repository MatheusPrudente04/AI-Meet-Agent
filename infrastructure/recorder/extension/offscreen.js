/**
 * offscreen.js — Documento offscreen que efetivamente grava o áudio.
 *
 * Fluxo:
 *   background.js obtém streamId via chrome.tabCapture.getMediaStreamId()
 *   → envia mensagem OFFSCREEN_START aqui
 *   → getUserMedia({ chromeMediaSource: 'tab', chromeMediaSourceId: streamId })
 *   → MediaRecorder grava em chunks e envia via POST para o FastAPI
 */

let recorder = null;
let chunkSeq = 0;
let captureSession = null;
let captureEndpoint = null;
let captureToken = '';
let startedAt = null;

// Offscreen só tem chrome.runtime (sem chrome.storage): o status vai por
// mensagem ao service worker, que é quem grava no storage.
function report(status) {
  status.startedAt = startedAt;
  return chrome.runtime.sendMessage({ type: 'OFFSCREEN_STATUS', status }).catch(() => {});
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.target !== 'offscreen') return;

  if (msg.type === 'OFFSCREEN_START') {
    startRecording(msg).catch((e) => {
      console.error('[offscreen] startRecording error:', e);
      report({ recording: false, error: String(e), chunks: 0 });
    });
  }

  if (msg.type === 'OFFSCREEN_STOP') {
    stopRecording();
  }
});

async function startRecording({ streamId, session, endpoint, chunkMs, token }) {
  if (recorder && recorder.state !== 'inactive') {
    console.warn('[offscreen] já gravando — ignorando START duplicado');
    return;
  }

  captureSession = session;
  captureEndpoint = endpoint;
  captureToken = token || '';
  chunkSeq = 0;

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: 'tab',
        chromeMediaSourceId: streamId,
      },
    },
    video: false,
  });

  // Re-roteia para ctx.destination para que os participantes continuem ouvindo.
  const ctx = new AudioContext();
  const src = ctx.createMediaStreamSource(stream);
  src.connect(ctx.destination);

  const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus'
    : 'audio/webm';

  recorder = new MediaRecorder(stream, { mimeType });

  // Um envio por vez: dois POSTs simultâneos podem chegar fora de ordem e corromper o webm.
  let sending = Promise.resolve();
  const send = async (data, seq) => {
    try {
      const resp = await fetch(`${captureEndpoint}/capture/chunk?session=${captureSession}&seq=${seq}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream', 'X-Capture-Token': captureToken },
        body: data,
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      await report({ recording: true, chunks: seq + 1, error: null });
    } catch (err) {
      console.error('[offscreen] chunk upload error:', err);
    }
  };

  recorder.ondataavailable = (e) => {
    if (!e.data || e.data.size === 0) return;
    const seq = chunkSeq++;
    sending = sending.then(() => send(e.data, seq));
  };

  recorder.onstop = async () => {
    await sending;  // finaliza só depois do último pedaço
    try {
      await fetch(`${captureEndpoint}/capture/finalize?session=${captureSession}`, {
        method: 'POST',
        headers: { 'X-Capture-Token': captureToken },
      });
    } catch (err) {
      console.error('[offscreen] finalize error:', err);
    }
    await report({ recording: false, chunks: chunkSeq, finalized: true, error: null });
  };

  recorder.start(chunkMs || 2000);
  startedAt = Date.now();
  await report({ recording: true, chunks: 0, finalized: false, error: null });
  console.log('[offscreen] gravação iniciada, session=', session);
}

function stopRecording() {
  if (!recorder || recorder.state === 'inactive') {
    console.warn('[offscreen] recorder inativo — nada a parar');
    return;
  }
  recorder.stop();
  console.log('[offscreen] gravação parada, total chunks=', chunkSeq);
}
