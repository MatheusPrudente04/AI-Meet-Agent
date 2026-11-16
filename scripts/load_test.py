"""Teste de carga com N usuários (S9-1, ADR-015).

    python -m scripts.load_test --api http://localhost:8001 --audio reuniao.wav \\
        --uploads 4,1,1 --readers 8 --read-seconds 0

Roda contra uma API em collab com cadastro aberto, **num banco de rascunho**: cria um
usuário por item de --uploads (ex.: 4,1,1 = o 1º envia 4 áudios, os outros 1 cada,
nessa ordem), acompanha cada reunião até done/error e, ao mesmo tempo, mede a latência
das rotas de leitura com --readers threads. Confere que ninguém vê reunião de outro.

Saída: latência p50/p95 por rota, espera na fila e tempo total por reunião, e a ordem
em que as reuniões começaram a ser transcritas.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
import uuid
from collections import defaultdict

import httpx

lat: dict[str, list[float]] = defaultdict(list)
errors: dict[str, int] = defaultdict(int)
lock = threading.Lock()


def timed(client: httpx.Client, route: str, method: str, url: str, **kw) -> httpx.Response:
    t0 = time.perf_counter()
    r = client.request(method, url, **kw)
    with lock:
        lat[route].append(time.perf_counter() - t0)
        if r.status_code >= 400:
            errors[f"{route} {r.status_code}"] += 1
    return r


def pct(values: list[float], p: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))] if values else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8001")
    ap.add_argument("--audio", required=True)
    ap.add_argument("--uploads", default="4,1,1", help="envios por usuário, na ordem de envio")
    ap.add_argument("--readers", type=int, default=8, help="threads lendo GET /meetings e /status sem parar")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()

    client = httpx.Client(base_url=args.api, timeout=120)
    run = uuid.uuid4().hex[:6]
    users = []
    for i, n in enumerate(int(x) for x in args.uploads.split(",")):
        email = f"carga{run}-{i}@example.com"
        r = client.post("/auth/register", json={"name": f"Usuário {i}", "email": email, "password": "senha-forte-123"})
        r.raise_for_status()
        users.append({"i": i, "n": n, "headers": {"Authorization": f"Bearer {r.json()['token']}"}, "ids": []})

    audio = open(args.audio, "rb").read()
    submitted = {}
    for u in users:  # o 1º usuário envia tudo antes dos outros: é o cenário de justiça
        for k in range(u["n"]):
            r = timed(client, "POST /upload", "POST", "/meetings/upload", headers=u["headers"],
                      params={"title": f"u{u['i']}-{k}"}, files={"file": ("a.wav", audio, "audio/wav")})
            r.raise_for_status()
            mid = r.json()["meeting_id"]
            u["ids"].append(mid)
            submitted[mid] = (u["i"], k, time.time())

    stop = threading.Event()

    def reader(n: int) -> None:
        c = httpx.Client(base_url=args.api, timeout=120)
        u = users[n % len(users)]
        while not stop.is_set():
            listing = timed(c, "GET /meetings", "GET", "/meetings/", headers=u["headers"])
            if listing.status_code == 200:
                seen = {m["meeting_id"] for m in listing.json()}
                if not seen <= set(u["ids"]):
                    with lock:
                        errors["DADO CRUZADO entre usuários"] += 1
            for mid in u["ids"]:
                timed(c, "GET /status", "GET", f"/meetings/{mid}/status", headers=u["headers"])

    threads = [threading.Thread(target=reader, args=(n,), daemon=True) for n in range(args.readers)]
    for t in threads:
        t.start()

    started, finished, positions = {}, {}, defaultdict(list)
    owner_headers = {mid: users[i]["headers"] for mid, (i, _, _) in submitted.items()}
    deadline = time.time() + args.timeout
    while len(finished) < len(submitted) and time.time() < deadline:
        for mid in submitted:
            if mid in finished:
                continue
            s = client.get(f"/meetings/{mid}/status", headers=owner_headers[mid]).json()
            if s["queue_position"]:
                positions[mid].append(s["queue_position"])
            if s["status"] in ("transcribing", "summarizing") and mid not in started:
                started[mid] = time.time()
            if s["status"] in ("done", "error"):
                finished[mid] = (time.time(), s["status"], s["error"])
                started.setdefault(mid, finished[mid][0])
        time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=150)

    print(f"\n== Latência das rotas ({args.readers} leitores) ==")
    for route, values in sorted(lat.items()):
        print(f"{route:16} n={len(values):5}  p50={pct(values, 50) * 1000:7.0f} ms  "
              f"p95={pct(values, 95) * 1000:7.0f} ms  máx={max(values) * 1000:7.0f} ms")
    print("\n== Reuniões, na ordem em que começaram ==")
    for mid in sorted(submitted, key=lambda m: started.get(m, float("inf"))):
        i, k, t_sub = submitted[mid]
        end, status, err = finished.get(mid, (None, "timeout", ""))
        wait = started.get(mid, time.time()) - t_sub
        total = (end - t_sub) if end else float("nan")
        first_pos = positions[mid][0] if positions[mid] else 0
        print(f"usuário {i} envio {k}: espera {wait:6.1f} s, total {total:6.1f} s, "
              f"1ª posição vista {first_pos:2}, {status} {err[:60]}")
    print("\n== Erros ==")
    for k, v in sorted(errors.items()):
        print(f"{k}: {v}")
    if not errors:
        print("nenhum")
    ok = len(finished) == len(submitted) and all(s == "done" for _, s, _ in finished.values()) \
        and not errors.get("DADO CRUZADO entre usuários")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
