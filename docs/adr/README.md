# ADRs — Meet Agent

Mapa das decisões de arquitetura necessárias para levar o projeto a produção.
Base: análise ponta a ponta de 2026-11-14.

Status possíveis: **Proposta** (aguarda aceite), **Aceita**, **Adiada**, **Substituída**.
Uma ADR só vira código depois de aceita. Ao aceitar, troque o status e a data aqui.

| # | Decisão | Status | Sprint | Reversível? |
|---|---------|--------|--------|-------------|
| 001 | Pipeline único de processamento | Aceita, implementada em 2026-11-14 | 1 | Sim |
| 002 | Documento único de reunião e status persistido | Aceita, implementada em 2026-11-14 | 1 | Com migração |
| 003 | Acesso ao MongoDB síncrono com PyMongo | Aceita, implementada em 2026-11-14 | 1 | Sim |
| 004 | Modos de execução: collab é produção, solo é desenvolvimento | Aceita, implementada em 2026-11-14 | 1 | Sim |
| 005 | Configuração centralizada e validada na partida | Aceita, implementada em 2026-11-14 | 1 | Sim |
| 006 | Bot como worker dedicado, despachado por fila | Aceita, implementada em 2026-11-14 (bot no host) | 2 | Sim |
| 007 | Autenticação e limites da API | Proposta | 3 | Sim |
| 008 | Runtime e dependências travadas | Proposta | 3 | Sim |
| 009 | Legendas e identificação de falantes | Proposta (decisão do dono do produto) | 4 | Sim |
| 010 | Contexto do LLM em reuniões longas | Adiada (medir antes) | 4 | Sim |

### Desvios na implementação da Sprint 1

- **ADR-002:** não foi criado `update_status` no repositório; `save(meeting)` com `status` e `error`
  na entidade cobre o caso com uma operação a menos no contrato.
- **ADR-002:** `participant_info`, `speaker_observations` e `speech_blocks` saíram da entidade, por
  não terem nenhum produtor. Voltam na forma definida pela ADR-009 fase 2, se ela for aceita.
- **ADR-002:** não há script de migração. Documentos antigos sem `status` têm o valor derivado na
  leitura (`done` se há resumo) e passam a tê-lo gravado no próximo `save`.
- **ADR-001:** as rotas `/meetings/bot*` e o `RecordMeetingUC` foram removidos em vez de adaptados,
  porque bloqueavam a API pela reunião inteira. O despacho do bot pela API volta na ADR-006.
- **ADR-001:** sem retry automático no Celery; a falha fica no documento e reprocessar o id é seguro.

---

## ADR-001 — Pipeline único de processamento

**Contexto.** Existem cinco implementações de "áudio → transcrição → resumo → salvar":
`worker/tasks.py::process_meeting_task_fn`, `MeetingPipelineOrchestrator`, `ETLAgent`,
`ETLFlowAgent` e o código inline em `api/routers/meetings.py::_process_sync` e no Streamlit.
Elas divergem em tratamento de erro, status e campos preenchidos. Em collab, `POST /meetings/bot`
dispara duas delas para a mesma reunião, com ids diferentes.

**Decisão.** Um único caso de uso, `ProcessMeetingUC` em `use_cases/process_meeting.py`,
contendo a sequência validar → transcrever → resumir → salvar. Ele recebe `meeting_id`,
`audio_path` e `title`, grava status e erro no repositório a cada etapa (ADR-002) e é idempotente
por `meeting_id`. Quem muda é só o executor: task Celery em collab, thread em solo.

**Alternativas descartadas.**
- Manter `MeetingPipelineOrchestrator` como base: engole falhas e não registra erro.
- Manter os "agentes" como camada de orquestração: nenhum deles decide nada; são sequências fixas com outro nome.

**Consequência.** Removidos: `agents/orchestrator_agent.py`, `agents/etl_agent.py`,
`infrastructure/agents/` inteiro, `infrastructure/pipeline/`, `_process_sync`, `_process_bot_audio`
e o processamento inline do Streamlit. `CollectionAgent` e `MemoryManager` saem junto se, ao
remover, nenhuma tela depender deles além do botão Refresh.

**Trade-off aceito.** Perde-se o rastreio passo a passo do `PipelineStep`; o status persistido cobre o que a UI mostra.

**Reverte-se assim.** `git revert` do commit de remoção; nenhum dado é alterado.

---

## ADR-002 — Documento único de reunião e status persistido

**Contexto.** Dois `MeetingDocument` apontam para a coleção `meetings`
(`mongo_documents.py` e `mongodb_documents.py`). O que está em uso descarta `participant_info`,
`speech_blocks`, `speaker_observations` e `status`. O status vive em quatro lugares (Redis,
dict na API, dict no worker, derivado do documento) e falhas deixam a reunião em "pending" para sempre.

**Decisão.**
- Um schema, em um arquivo, com os campos da entidade `Meeting` mais `status`, `error`,
  `created_at`, `updated_at`.
- `status` é um enum único no domínio: `pending`, `recording`, `transcribing`, `summarizing`, `done`, `error`.
- O documento é a única fonte de status. Redis volta a ser só broker do Celery.
- `started_at` passa a ser o início real da gravação, informado pelo bot.
- `IMeetingRepository` ganha `update_status(meeting_id, status, error="")`.
- Os mesmos campos passam a ser persistidos pelo repositório JSON.

**Alternativas descartadas.**
- Status no Redis com TTL: some após 24 h e não sobrevive a restart sem persistência.
- Coleção separada de jobs: mais uma junção para um dado de cardinalidade 1:1.

**Trade-off aceito.** Uma escrita a mais no Mongo por etapa do pipeline.

**Reverte-se assim.** Os campos novos são aditivos; documentos antigos continuam legíveis com defaults.
Script de migração preenche `status="done"` onde há resumo.

---

## ADR-003 — Acesso ao MongoDB síncrono com PyMongo

**Contexto.** `storage_mongodb.py` usa Beanie + Motor. Como Streamlit, Celery e parte da API são
síncronos, cada operação cria uma thread, um event loop e um `AsyncIOMotorClient` novo, que nunca
é fechado. O volume de dados é pequeno (uma reunião por documento, dezenas por dia).

**Decisão.** Repositório síncrono com PyMongo e um `MongoClient` por processo. Rotas da FastAPI
que tocam o banco viram `def` (o FastAPI as executa em threadpool). Saem Beanie, Motor e os
métodos `*_async` do repositório.

**Alternativas descartadas.**
- Manter Motor e corrigir o ciclo de vida do cliente: continua exigindo duas APIs (sync e async) em todo repositório.
- Tornar tudo assíncrono: Streamlit e Celery não são, e o gargalo do sistema é Whisper/LLM, não I/O de banco.

**Trade-off aceito.** Sem I/O assíncrono de banco na API. Irrelevante no volume atual; revisar se
a API passar de dezenas de requisições por segundo.

**Reverte-se assim.** A interface `IMeetingRepository` não muda; trocar a implementação é local.

---

## ADR-004 — Modos de execução: collab é produção, solo é desenvolvimento

**Contexto.** O código tem ramificações `is_solo`/`is_collab` em API, worker, container e UI,
além de um fallback de fila em memória "para Windows".

**Decisão.** Produção é sempre collab (Mongo + Redis + Celery). Solo permanece como modo de
desenvolvimento local, com repositório JSON e o mesmo `ProcessMeetingUC` rodando em thread.
A diferença entre os modos fica restrita ao container de DI: repositório e executor.
O fallback `_FallbackTask`/`_worker_loop` é removido.

**Alternativas descartadas.**
- Remover o modo solo: encarece o ciclo de desenvolvimento (exige Mongo e Redis para testar um prompt).
- Tratar solo como produto: dobra a superfície de teste sem usuário que justifique.

**Reverte-se assim.** Reintroduzir ramificações; nenhum dado envolvido.

---

## ADR-005 — Configuração centralizada e validada na partida

**Contexto.** `.env` define `MONGO_DB_NAME`, mas `Settings` lê `MONGO_DB`; o valor é ignorado em
silêncio (`extra="ignore"`). `DbAdminAgent`, `ArquivosStorage` e `CollectionAgent` leem
`os.environ` direto. `secret_key` tem default de desenvolvimento. O código usa
`getattr(settings, "campo", default)` para campos que existem.

**Decisão.**
- `config/settings.py` é a única leitura de ambiente; nenhum `os.environ` fora dele.
- Em collab, a partida falha com mensagem clara se faltar `MONGO_URI`, `REDIS_URL`, chave de API
  ou credencial do provedor de LLM escolhido.
- `.env.example` versionado com todas as chaves e sem valores.
- `getattr(settings, ...)` trocado por acesso direto.

**Reverte-se assim.** Mudança local em settings.

---

## ADR-006 — Bot como worker dedicado, despachado por fila

**Contexto.** Hoje o bot nasce de duas formas: subprocesso do Streamlit (`start_bot`) ou
`POST /meetings/bot`, que bloqueia o event loop da API pela reunião inteira. A extensão posta
chunks em `127.0.0.1:8000`, fixo no código, o que só funciona com tudo no mesmo host. A UI lê
status de um arquivo `bot_session_*.json` que não é mais gravado.

**Decisão.**
- Entrar em reunião é uma task Celery em fila própria (`bot`), consumida por um serviço
  `bot-worker` com Chrome, Xvfb e o perfil do Chrome em volume. Concorrência 1 por réplica:
  uma réplica grava uma reunião por vez.
- O `meeting_id` é criado no despacho, o documento nasce com status `recording`, e o mesmo id é
  usado como sessão de captura e nome do `.webm`.
- O endpoint de captura vem de `CAPTURE_ENDPOINT` (settings), não de constante.
- Ao fim da gravação o bot-worker enfileira `process_meeting` (ADR-001).
- UI e API só despacham e leem status do documento. Somem `start_bot` por subprocesso e o
  `send_bot` bloqueante.

**Alternativas descartadas.**
- Subprocesso a partir da API: acopla o ciclo de vida do bot ao da API e não escala além de um host.
- Serviço de bot de terceiros (Recall.ai e similares): resolve a fragilidade do DOM do Meet, mas
  cria custo por hora e dependência externa; reavaliar se a manutenção dos seletores virar rotina.

**Trade-off aceito.** Capacidade limitada ao número de réplicas do bot-worker.

**Risco conhecido.** Extensões MV3 exigem Chrome com interface ou o headless novo; validar no
container antes de fechar a sprint. A conta Google do bot pode cair em desafio de login em IP de
datacenter.

**Reverte-se assim.** O recorder continua executável por CLI (`run_bot`), que permanece como ferramenta de diagnóstico.

**Implementação (2026-11-14) — o bot-worker roda no host, não em container.** O destino atual é
Docker Compose na máquina do dono do projeto. API, worker de processamento, Streamlit, Mongo e
Redis ficam no compose; o worker do bot (`python main.py --bot-worker`) roda direto no macOS e
consome a fila `bot` pelo Redis publicado em `localhost:6380`. Motivo: a sessão Google salva pelo
Chromium do macOS não é legível por um Chromium Linux dentro de container, e refazer o login lá
exigiria tela virtual com acesso remoto. A task `join_meeting` é a mesma nos dois arranjos; levar o
bot para um container quando houver servidor é trabalho de imagem e login, não de código.
Os caminhos de áudio são relativos (`data/audio/<id>.webm`) para valerem tanto no host quanto em `/app`.

---

## ADR-007 — Autenticação e limites da API

**Contexto.** Nenhuma rota tem autenticação. `/capture/chunk` grava em disco sem limite.
`/meetings/bot/done` aceita um `audio_path` arbitrário. `/meetings/upload` lê o arquivo inteiro
em memória. O conjunto de sessões finalizadas vive em memória do processo.

**Decisão.**
- Chave de API em cabeçalho (`X-API-Key`) para todas as rotas exceto `/health`, comparada em tempo constante.
- Rotas `/capture/*` exigem token por sessão, gerado no despacho do bot e entregue à extensão junto com o START.
- Limite de tamanho por chunk e por sessão; upload gravado em streaming com teto de `MAX_UPLOAD_MB`.
- Nenhuma rota recebe caminho de arquivo do cliente; o caminho é derivado do `meeting_id` no servidor.
- Estado de "sessão finalizada" sai da memória e passa a ser o status do documento.
- `/meetings/bot/done` é removida (substituída pela ADR-006).

**Alternativas descartadas.**
- OAuth/JWT com usuários: não há modelo de usuário nem multi-tenant hoje. Entra quando houver mais de um time usando.
- Só isolamento de rede: a extensão roda num navegador que abre páginas externas.

**Trade-off aceito.** Uma chave compartilhada não identifica quem fez a chamada.

**Reverte-se assim.** Dependência do FastAPI removível por rota.

---

## ADR-008 — Runtime e dependências travadas

**Contexto.** Todas as dependências usam `>=`, sem lockfile. O venv local é Python 3.14; as imagens
Docker são 3.11. `requirements.txt` e `requirements-collab.txt` se sobrepõem;
`requirements_diarization.txt` tem uma linha inválida. O compose referencia `Dockerfile.streamlit`
e o arquivo é `dockerfile.streamlit`.

**Decisão.**
- Python 3.11 como versão única, local e em imagem.
- `requirements.in` (base), `requirements-dev.in` (testes) e `requirements-diarization.in` (opcional),
  compilados em `.txt` com versões exatas por `pip-compile`.
- Uma imagem para api, worker e streamlit; uma imagem separada para o bot-worker (Chrome + Xvfb).
- CI roda `pytest` e o build das imagens a cada push.

**Alternativas descartadas.**
- Poetry ou migração de gerenciador: troca de ferramenta sem ganho para um projeto sem publicação de pacote.

**Reverte-se assim.** Os `.in` preservam as faixas atuais.

---

## ADR-009 — Legendas e identificação de falantes

**Contexto.** O bot liga as legendas do Meet, mas nada as lê. As coleções `arquivos` e
`arquivos_mesclados`, o `DbAdminAgent`, o `BlockBasedTranscriber` e o fallback por legendas do
worker existem sem fonte de dados. Hoje os participantes saem como "Speaker 0/1" por pausa.

**Decisão proposta.** Em duas fases.
- Fase 1 (Sprint 1): remover o código sem fonte (`ArquivosStorage`, `DbAdminAgent`,
  `BlockBasedTranscriber`, fallback por legendas). Produção sai com Whisper e pseudo-diarização.
- Fase 2 (Sprint 4): capturar legendas no `content.js` da extensão, que já roda na aba do Meet,
  e enviá-las para `/capture/captions`. O resultado entra no próprio documento da reunião como
  lista `{speaker, text, ts}` e é usado para nomear os falantes da transcrição do Whisper por
  sobreposição de tempo. Sem coleções auxiliares.

**Alternativas.**
- Só Whisper + pyannote: nomes reais nunca aparecem, mas não depende do DOM do Meet.
- Legendas como transcrição principal: gratuito e com nomes, porém com qualidade inferior ao Whisper large-v3 em português.

**Trade-off aceito.** A fase 2 depende de seletores do Meet, que mudam sem aviso; precisa de teste de fumaça recorrente.

**Pendente.** O dono do produto decide se a fase 2 entra no roteiro.

---

## ADR-010 — Contexto do LLM em reuniões longas

**Contexto.** Resumo e chat enviam a transcrição inteira no prompt. Uma hora de reunião gira em
torno de 10 a 15 mil tokens (estimativa, não medido). Modelos locais pequenos não comportam isso.

**Decisão.** Adiada. Na Sprint 4, medir tokens por reunião nas gravações reais e a taxa de falha
por provedor. Só então escolher entre: exigir modelo com janela suficiente em produção,
resumo em etapas (por trecho, depois consolidação) ou recuperação por trechos no chat.

**Enquanto isso.** O pipeline registra o tamanho da transcrição e falha com erro explícito quando
o provedor recusa por contexto, em vez de truncar em silêncio.
