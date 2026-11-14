"""
test_audio_capture_local.py — Diagnóstico local do pipeline de captura de áudio.

Abre um Chromium real via Playwright, cria um stream de áudio sintético
(oscilador Web Audio API) e testa os 3 caminhos possíveis de captura:

  1. BroadcastChannel Worker → janela principal (caminho primário do bot)
  2. Tracks WebRTC no main thread via window.__rtcAudioTracks (fallback A)
  3. Elementos <audio>/<video> no DOM com .captureStream() (fallback B — novo)

Uso:
  python -m pytest test/test_audio_capture_local.py -v -s
  # ou diretamente:
  python test/test_audio_capture_local.py
"""
from __future__ import annotations

import asyncio
import base64
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

JS_DIR = REPO_ROOT / "infrastructure" / "recorder"
AUDIO_CAPTURE_JS = (JS_DIR / "audio_capture.js").read_text(encoding="utf-8")


# ── HTML com stream de áudio sintético ──────────────────────────────────────
# Cria um oscilador de 440 Hz, converte em MediaStream e expõe como:
#   • window.__syntheticStream  — para o caminho de tracks WebRTC (fallback A)
#   • Um Worker dedicado que simula o BroadcastChannel do Meet (caminho 1)

LOCAL_PAGE_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Audio Capture Diag</title></head>
<body>
<script>
// ── Cria stream de áudio sintético (440 Hz) ─────────────────────────────────
const ctx = new AudioContext();
const osc = ctx.createOscillator();
osc.frequency.value = 440;
osc.start();
const dest = ctx.createMediaStreamDestination();
osc.connect(dest);
const syntheticStream = dest.stream;

// Expõe o stream como __rtcAudioTracks para o fallback A do audio_capture.js
window.__rtcAudioTracks = syntheticStream.getAudioTracks();

// Expõe também via elemento <audio> para o fallback DOM (caminho 3)
const audioEl = document.createElement('audio');
audioEl.srcObject = syntheticStream;
audioEl.muted = true;
audioEl.autoplay = true;
document.body.appendChild(audioEl);

window.__syntheticStream = syntheticStream;
console.log('[diag] Stream sintético criado: ' + syntheticStream.id +
  ' | tracks: ' + syntheticStream.getTracks().length +
  ' | estado: ' + ctx.state);
</script>
</body>
</html>"""

WORKER_SIMULATE_JS = """
// Simula o Worker do Meet enviando chunks via BroadcastChannel
const bc = new BroadcastChannel('__meetAudioWorker');

const ctx = new AudioContext();
const osc = ctx.createOscillator();
osc.frequency.value = 880;  // tom diferente para distinguir do fallback
osc.start();
const dest = ctx.createMediaStreamDestination();
osc.connect(dest);

ctx.resume().then(() => {
    const mr = new MediaRecorder(dest.stream, {
        mimeType: 'audio/webm;codecs=opus',
        audioBitsPerSecond: 64000,
    });
    mr.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) {
            bc.postMessage({ type: 'chunk', blob: e.data });
            console.log('[worker-sim] chunk enviado: ' + e.data.size + ' bytes');
        }
    };
    mr.start(500);
    bc.postMessage({ type: 'started' });
    console.log('[worker-sim] Worker simulado iniciado');
}).catch(e => {
    bc.postMessage({ type: 'error', message: String(e) });
});
"""

COLLECT_JS = """
(async () => {
    if (!window.__meetCapture) return {error: '__meetCapture não existe'};
    try {
        const b64 = await window.__meetCapture.stop();
        const state = window.__meetCapture.getState();
        const err = window.__meetCapture._error;
        return { b64, state, error: err };
    } catch(e) {
        return { error: String(e) };
    }
})()
"""


async def run_diag(out_dir: Path, headless: bool = True, wait_s: float = 4.0):
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "diag.html"
    html_path.write_text(LOCAL_PAGE_HTML, encoding="utf-8")

    results = {}

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(out_dir / "profile"),
            headless=headless,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(html_path.as_uri())
        await asyncio.sleep(1)  # deixa AudioContext inicializar

        # Verifica estado antes de injetar o capturador
        try:
            pre = await page.evaluate("""() => ({
                tracks: (window.__rtcAudioTracks || []).map(t => ({kind: t.kind, state: t.readyState})),
                contextState: window.__syntheticStream ? 'stream-ok' : 'no-stream',
            })""")
            results["pre_inject"] = pre
            print(f"[diag] Pré-injeção: {pre}")
        except Exception as e:
            print(f"[diag] Erro pré-injeção: {e}")

        # Simula o Worker enviando via BroadcastChannel (caminho primário)
        print("[diag] Iniciando Worker simulado (BroadcastChannel)...")
        try:
            await page.evaluate(WORKER_SIMULATE_JS)
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[diag] Worker simulado falhou: {e}")
            results["worker_sim_error"] = str(e)

        # Injeta audio_capture.js
        print("[diag] Injetando audio_capture.js...")
        try:
            await page.evaluate(AUDIO_CAPTURE_JS)
            init_state = await page.evaluate(
                "window.__meetCapture ? window.__meetCapture.getState() : {}"
            )
            init_err = await page.evaluate(
                "window.__meetCapture ? window.__meetCapture._error : 'não inicializado'"
            )
            results["init_state"] = init_state
            results["init_error"] = init_err
            print(f"[diag] Estado inicial: {init_state} | erro: {init_err}")
        except Exception as e:
            print(f"[diag] Erro ao injetar audio_capture.js: {e}")
            results["inject_error"] = str(e)

        # Aguarda chunks acumularem
        print(f"[diag] Aguardando {wait_s}s para acumular chunks...")
        t0 = time.time()
        while time.time() - t0 < wait_s:
            await asyncio.sleep(0.5)
            try:
                snap = await page.evaluate(
                    "window.__meetCapture ? window.__meetCapture.getState() : {}"
                )
                chunks = snap.get("chunks", 0)
                recording = snap.get("recording", False)
                if chunks > 0:
                    print(f"[diag] ✅ {chunks} chunk(s) acumulados | gravando={recording}")
                    break
            except Exception:
                pass

        # Snapshot final
        try:
            final_snap = await page.evaluate(
                "window.__meetCapture ? window.__meetCapture.getState() : {}"
            )
            results["final_state"] = final_snap
            print(f"[diag] Estado final: {final_snap}")
        except Exception as e:
            results["final_state_error"] = str(e)

        # Coleta o áudio
        print("[diag] Coletando áudio...")
        try:
            collect = await page.evaluate(COLLECT_JS)
            b64 = collect.get("b64", "")
            state = collect.get("state", {})
            err = collect.get("error")
            results["collect"] = {
                "bytes": len(base64.b64decode(b64)) if b64 else 0,
                "state": state,
                "error": err,
            }
            if b64:
                audio_bytes = base64.b64decode(b64)
                webm_path = out_dir / "diag_capture.webm"
                webm_path.write_bytes(audio_bytes)
                print(f"[diag] ✅ {len(audio_bytes)} bytes capturados → {webm_path}")
            else:
                print(f"[diag] ❌ ZERO BYTES capturados | state={state} | error={err}")
        except Exception as e:
            print(f"[diag] Erro na coleta: {e}")
            results["collect_error"] = str(e)

        # Console logs do browser
        logs = []
        page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
        await asyncio.sleep(0.5)
        results["console_logs"] = logs

        await ctx.close()

    return results


def main():
    import tempfile
    out = Path(tempfile.mkdtemp(prefix="diag_audio_"))
    print(f"\n{'='*60}")
    print("DIAGNÓSTICO DE CAPTURA DE ÁUDIO — pipeline local")
    print(f"{'='*60}")
    print(f"Saída: {out}\n")

    try:
        results = asyncio.run(run_diag(out, headless=False, wait_s=5.0))
    except ImportError:
        print("❌ Playwright não instalado. Execute: pip install playwright && playwright install chromium")
        return 1

    print(f"\n{'='*60}")
    print("RESULTADO:")
    for k, v in results.items():
        print(f"  {k}: {v}")

    collected = results.get("collect", {})
    bytes_cap = collected.get("bytes", 0)
    if bytes_cap > 0:
        print(f"\n✅ SUCESSO — {bytes_cap} bytes capturados")
        print(f"   Arquivo: {out / 'diag_capture.webm'}")
        return 0
    else:
        print(f"\n❌ FALHA — 0 bytes capturados")
        print(f"   Erro: {collected.get('error')}")
        print(f"   Estado captura: {collected.get('state')}")
        print(f"\nVerifique os logs do console acima para diagnóstico.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
