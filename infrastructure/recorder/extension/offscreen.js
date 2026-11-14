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

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.target !== 'offscreen') return;

  if (msg.type === 'OFFSCREEN_START') {
    startRecording(msg).catch((e) => {
      console.error('[offscreen] startRecording error:', e);
      chrome.storage.local.set({ captureStatus: { recording: false, error: String(e), chunks: 0 } });
    });
  }

  if (msg.type === 'OFFSCREEN_STOP') {
    stopRecording();
  }
});

async function startRecording({ streamId, session, endpoint, chunkMs }) {
  if (recorder && recorder.state !== 'inactive') {
    console.warn('[offscreen] já gravando — ignorando START duplicado');
    return;
  }

  captureSession = session;
  captureEndpoint = endpoint;
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

  recorder.ondataavailable = async (e) => {
    if (!e.data || e.data.size === 0) return;
    const seq = chunkSeq++;
    try {
      await fetch(`${captureEndpoint}/capture/chunk?session=${captureSession}&seq=${seq}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: e.data,
      });
      await chrome.storage.local.set({
        captureStatus: { recording: true, chunks: seq + 1, error: null },
      });
    } catch (err) {
      console.error('[offscreen] chunk upload error:', err);
    }
  };

  recorder.onstop = async () => {
    try {
      await fetch(`${captureEndpoint}/capture/finalize?session=${captureSession}`, {
        method: 'POST',
      });
    } catch (err) {
      console.error('[offscreen] finalize error:', err);
    }
    await chrome.storage.local.set({
      captureStatus: { recording: false, chunks: chunkSeq, finalized: true, error: null },
    });
  };

  recorder.start(chunkMs || 2000);
  await chrome.storage.local.set({
    captureStatus: { recording: true, chunks: 0, finalized: false, error: null },
  });
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
