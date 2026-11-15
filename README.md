# Meet Agent

Bot que entra em reuniões do Google Meet, grava o áudio, transcreve, identifica quem falou,
gera um resumo com tarefas e decisões e responde perguntas sobre a reunião.

Decisões de arquitetura: [docs/adr/README.md](docs/adr/README.md). Roteiro: [docs/roadmap.md](docs/roadmap.md).

## Como funciona

```
Streamlit / API ──► fila "bot" ──► worker do bot (host) ──► Chromium entra no Meet
                                        │  áudio em chunks ──► API /capture ──► data/audio/<id>.webm
                                        │  legendas e presentes ──► data/audio/<id>.speakers.jsonl
                                        ▼
                    fila "celery" ──► worker de processamento ──► Whisper ──► nomes ──► LLM ──► MongoDB
```

- Um único pipeline (`use_cases/process_meeting.py`) faz áudio → transcrição → resumo e grava o
  status a cada etapa: `pending`, `recording`, `transcribing`, `summarizing`, `done`, `error`.
- O texto vem do Whisper. As legendas do Meet são usadas só para saber **quem** falou e quando.
- O bot entra com microfone e câmera desligados: só escuta.

| Pasta | Conteúdo |
|-------|----------|
| `domain/` | Entidades e interfaces (portas) |
| `use_cases/` | Regras: pipeline, resumo, chat, alinhamento de falantes |
| `infrastructure/` | Whisper, LLM, repositórios, bot (`recorder/`), e-mail, exportação, agenda |
| `presentation/` | `container.py` (única composição) e a interface Streamlit |
| `api/` | FastAPI |
| `worker/` | Tasks Celery |

## Subir o ambiente (produção local)

Pré-requisitos: Docker Desktop, Python 3.11, [Ollama](https://ollama.com) com o modelo configurado
(`ollama pull qwen2.5:7b`).

```bash
cp .env.example .env            # preencha; API_KEY é obrigatória (comando para gerar está no arquivo)

# 1. Serviços: api :8001, streamlit :8501, redis :6380, mongo :27018
docker compose up -d --build

# 2. Ambiente do bot, no host (ele usa o Chromium e o login do Google desta máquina)
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
playwright install chromium
python -m infrastructure.recorder.bot_setup     # uma vez: login manual da conta do bot

# 3. Worker do bot, no host (deixe rodando)
python main.py --bot-worker
```

Abra http://localhost:8501, cole o link do Meet e envie o bot. Quem organiza a reunião precisa
**admitir** o bot quando ele pedir para entrar. A tela acompanha o andamento sozinha.

Depois de atualizar o código: `docker compose up -d --build` e reinicie o worker do bot.

### Modo de desenvolvimento (sem Docker)

Com `APP_MODE=solo` não há fila nem MongoDB: as reuniões ficam em `data/meetings/*.json`.

```bash
python main.py --api      # API
python main.py            # interface
python -m infrastructure.recorder.run_bot --url "https://meet.google.com/xxx-xxxx-xxx"
```

## API

Todas as rotas exigem o cabeçalho `X-API-Key`, exceto `/health`.

| Rota | Função |
|------|--------|
| `GET /health` | Confere banco e fila; 503 se algum não responde |
| `GET /meetings/` | Lista as reuniões |
| `POST /meetings/upload` | Envia um áudio para processar (teto `MAX_UPLOAD_MB`) |
| `POST /meetings/bot` | Manda o bot para um link do Meet |
| `GET /meetings/{id}/status` | Status, erro e resumo |
| `POST /meetings/{id}/process` | Reprocessa o áudio já gravado |
| `POST /meetings/{id}/chat` | Pergunta sobre a reunião (a conversa fica guardada) |
| `PATCH /meetings/{id}/tasks/{n}` | Marca ou desmarca a tarefa `n` |
| `GET /meetings/{id}/export.md` | Reunião em Markdown |
| `POST /meetings/{id}/tasks/email` | Envia as tarefas por e-mail (requer SMTP no `.env`) |
| `POST /capture/*` | Uso interno do bot; token por sessão |

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8001/meetings/
```

## Recursos opcionais

| Recurso | Como ligar |
|---------|------------|
| E-mail de tarefas | `SMTP_USER` e `SMTP_PASSWORD` no `.env` |
| Título pela agenda | `ENABLE_CALENDAR=true`, com `credentials.json` do Google Calendar |
| Exportação Markdown | Sempre disponível (botão na interface e rota da API) |

## Operação

- **Logs:** toda linha traz o id da reunião. `docker compose logs worker | grep <id>` segue uma reunião.
- **Backup:** `scripts/backup.sh` gera `backups/mongo-<data>.archive.gz` e apaga gravações com mais
  de 30 dias (`AUDIO_RETENTION_DAYS`). Restaurar: `scripts/backup.sh restore <arquivo>`.
- **Reunião com erro:** a mensagem aparece na interface; o botão "Reprocessar áudio gravado" refaz
  a transcrição e o resumo sem precisar de nova reunião.
- **Diagnóstico de nomes:** `data/audio/<id>.speakers.jsonl` mostra se as legendas ligaram
  (`captions`), quem estava (`participants`) e quem falou (`speaker`).

## Limites conhecidos

- O bot enxerga o **nome de exibição** de cada participante, não o e-mail.
- Trechos em que o Meet não gerou legenda ficam como "Speaker N" (com uma única pessoa além do
  bot, todas as falas são atribuídas a ela).
- O Meet muda a tela sem aviso; os seletores ficam em `infrastructure/recorder/playwright_bot_recorder.py`.
- Reuniões longas são resumidas em partes e o chat consulta só os trechos ligados à pergunta
  (ADR-010). `LLM_CONTEXT_CHARS` ajusta o tamanho de cada parte.
- O worker do bot grava uma reunião por vez.

## Testes e dependências

```bash
python -m pytest            # o teste com reunião real só roda com MEET_TEST_URL exportada
```

As versões exatas ficam em `requirements.txt` e `requirements-dev.txt`, geradas a partir dos `.in`
em Python 3.11. Para atualizar:

```bash
docker run --rm -v "$PWD":/w -w /w python:3.11-slim sh -c \
  "pip install pip-tools && pip-compile --strip-extras -o requirements.txt requirements.in \
   && pip-compile --strip-extras -o requirements-dev.txt requirements-dev.in"
```

`requirements-diarization.in` lista as dependências da diarização por pyannote (opcional, não travadas).
