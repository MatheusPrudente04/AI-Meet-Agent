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
| 007 | Autenticação e limites da API | Aceita, implementada em 2026-11-15 | 3 | Sim |
| 008 | Runtime e dependências travadas | Aceita, implementada em 2026-11-15 (venv local segue em 3.14) | 3 | Sim |
| 009 | Legendas e identificação de falantes | Implementada (fase 2 em 2026-11-15) | 2B | Sim |
| 010 | Contexto do LLM em reuniões longas | Aceita, implementada em 2026-11-15 | 4 | Sim |
| 011 | Login de usuários e sessões no MongoDB | Aceita, implementada em 2026-11-15 | 5 | Com migração |
| 012 | Bot multiplataforma (Meet, Teams, Zoom; Discord adiado) | Aceita; Meet validado, Teams e Zoom não | 5 | Sim |
| 013 | Contrato OpenAPI para um novo front | Aceita, implementada em 2026-11-15 | 5 | Sim |

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

**Como ficou (2026-11-15).**
- O token de captura é `HMAC-SHA256(API_KEY, id da reunião)`: não precisa ser guardado, e o
  navegador do bot recebe só o token daquela reunião, nunca a chave.
- `/capture/chunk` só aceita reunião existente em `pending` ou `recording`; fora disso, 409.
- Sem `API_KEY` (desenvolvimento em solo) a API fica aberta; em collab a partida falha sem ela.
- A interface Streamlit não passa pela API (usa o container direto), então não usa a chave.

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

**Como ficou (2026-11-15).** `requirements.in`, `requirements-dev.in` e `requirements-diarization.in`;
os dois primeiros compilados em Python 3.11 (comando no README). CI em `.github/workflows/ci.yml`.
Desvios: a diarização não foi travada (opcional, puxa torch); a imagem do bot-worker não existe,
porque o bot roda no host (ADR-006); o venv local do desenvolvedor continua em 3.14 e o CI é quem
garante o 3.11.

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

**Fase 2 aceita em 2026-11-14** e antecipada para a Sprint 2B, a pedido do dono do produto: o chat
precisa responder quem estava na reunião e quem disse o quê, com o nome de cada pessoa.

Detalhamento da fase 2:
- **Das legendas usamos só o nome de quem fala e o instante, não o texto.** O texto continua vindo
  do Whisper, que é melhor em português. Assim o idioma configurado nas legendas deixa de importar.
- **Presença** vem dos blocos de participantes da tela; **autoria das falas** vem das legendas.
- Os eventos ficam num arquivo por sessão (`<id>.speakers.jsonl`) ao lado do áudio; o documento da
  reunião guarda o resultado (nomes em `participants` e na transcrição), não os eventos brutos.
- Alinhamento por sobreposição de tempo entre evento de legenda e segmento do Whisper. Sem
  evidência suficiente o segmento mantém "Speaker N": nome errado é pior que nome ausente.
- O Meet expõe ao bot o nome de exibição, não e-mail nem id de conta.

Alternativa descartada na fase 2: detectar o falante pela animação de áudio dos blocos de vídeo.
Depende de classes CSS ofuscadas, mais instáveis que a região de legendas.

**Como ficou (2026-11-15).** A leitura é feita pelo recorder (Playwright consulta a página a cada
segundo), não pela extensão. O que as reuniões reais ensinaram:
- A sala de espera também mostra "Sair da chamada". O bot só se considera admitido quando existe o
  botão de legendas ou mais de um participante; antes disso o atalho `c` não faz nada.
- As legendas são ligadas pelo atalho `c` e a tentativa se repete a cada 10 s enquanto a região
  não existir.
- `data-use-raw-local-presentation-stream` aparece em todos os blocos e não identifica o do bot.
  O bloco do bot é o único com o botão "Planos de fundo e efeitos"; o nome dele vira um evento
  `self` e é retirado da lista de presentes.
- Com um único participante além do bot, toda fala é atribuída a ele mesmo sem legenda.
- **Não validado:** nome de terceiros nas legendas com duas ou mais pessoas falando.

---

## ADR-010 — Contexto do LLM em reuniões longas

**Contexto.** Resumo e chat enviavam a transcrição inteira no prompt.

**Medições (2026-11-15, reuniões reais, `qwen2.5:7b` no Ollama 0.24).**
- 2,64 caracteres por token; cerca de 295 tokens por minuto de reunião.
- 1 h ≈ 18 mil tokens; 2 h ≈ 35 mil, acima da janela do modelo (32.768).
- O Ollama, sem `num_ctx`, usa 4.096 tokens e **descarta o início do prompt sem avisar**. Qualquer
  reunião acima de ~13 min já era resumida e respondida com transcrição cortada.
- Leitura do prompt nesta máquina: ~70 tokens/s (9,7 mil tokens em 140 s).

**Decisão.**
- O Ollama é chamado pela API nativa com `num_ctx` do tamanho do prompt, em degraus fixos
  (4096 … 32768) para não recarregar o modelo a cada pedido. Acima de `OLLAMA_NUM_CTX_MAX` o erro
  é explícito.
- `LLM_CONTEXT_CHARS` (padrão 30.000, ~11 mil tokens) é quanto de transcrição vai num pedido.
- **Resumo:** transcrição maior é dividida por linhas, cada parte é resumida e os resultados são
  reunidos na ordem da reunião.
- **Chat:** transcrição maior vai como lista de presentes + resumo + até 8 mil caracteres dos
  trechos com mais palavras em comum com a pergunta, o mais relacionado primeiro. Com a janela
  cheia (28 mil caracteres) e trechos em ordem cronológica, o modelo respondeu "não foi
  mencionado" para um dado que estava no contexto; focado, acertou e caiu de ~150 s para 5–30 s.
- **Verificado** numa transcrição sintética de 2 h (100 mil caracteres): resumo em 4 partes sem
  erro (700 s nesta máquina) e chat correto em 4 perguntas de controle.
- O documento guarda `metrics` (tempos de transcrição e resumo, tamanho, número de partes).

**Alternativas descartadas.**
- Exigir modelo de janela grande: funciona com provedores pagos, mas a implantação atual é local.
- Busca vetorial para o chat: nova dependência e índice para manter; o casamento por palavra
  resolve perguntas diretas. Reavaliar se as respostas em reuniões longas perderem contexto.

**Trade-off aceito.** No resumo em partes a visão geral é a sequência das visões parciais e uma
tarefa citada em duas partes pode aparecer duas vezes. O chat de reunião longa pode não achar
resposta que dependa de sinônimo.

**Reverte-se assim.** `LLM_CONTEXT_CHARS` alto desliga a divisão para modelos de janela grande.

---

## ADR-011 — Login de usuários e sessões no MongoDB

**Contexto.** A ADR-007 deixou uma chave compartilhada, que não identifica quem chama, e
registrou o gatilho: mais de uma pessoa usando. O dono do produto pediu cadastro, login
multiusuário e as sessões guardadas no MongoDB.

**Decisão.**
- **Usuário:** coleção `users` (`name`, `email` único em minúsculas, `password_hash`). A senha é
  guardada como scrypt (biblioteca padrão) com sal por usuário, mínimo de 8 caracteres.
- **Sessão:** token aleatório de 256 bits entregue ao cliente; a coleção `sessions` guarda só o
  SHA-256 dele, com `user_id`, `created_at`, `expires_at`, `user_agent` e `ip`. Um índice TTL em
  `expires_at` apaga as vencidas. Validade: `SESSION_DAYS` (30). Logout apaga a sessão.
- **Dono da reunião:** `Meeting.owner_id`. Cada usuário lista e abre só as próprias reuniões; a
  de outro responde 404 (não 403), para não revelar que existe.
- **Reuniões anteriores ao login:** a primeira conta cadastrada herda todas.
- **Credenciais da API:** `Authorization: Bearer <token>` (usuário) ou `X-API-Key` (chave de
  serviço, enxerga tudo). `/capture/*` segue com o token por reunião da ADR-007.
- **Interface:** o Streamlit ganhou a tela de entrar e criar conta, e filtra tudo pelo usuário.
- **Solo não tem login** (ADR-004: é modo de desenvolvimento).

**Alternativas descartadas.**
- JWT sem estado: não dá para encerrar uma sessão antes do prazo, e o pedido era guardá-las no banco.
- bcrypt/argon2: dependência nova para o que o scrypt da biblioteca padrão já entrega.
- Provedor externo (Google, Auth0): a implantação é local e sem domínio público.

**Trade-off aceito.**
- Não há limite de tentativas de login nem confirmação de e-mail. Aceitável com a API só na
  máquina local; **obrigatório resolver antes de expor na internet**, junto com HTTPS.
- Não há recuperação de senha (depende do e-mail com SMTP real, previsto para a Sprint 6).
- No Streamlit o login vive na sessão do navegador: atualizar a página (F5) pede login de novo.
- `ALLOW_REGISTRATION=true` deixa qualquer pessoa com acesso à porta criar conta; feche com
  `false` depois de cadastrar quem deve entrar.

**Verificado.** Testes cobrem cadastro, login, logout, sessão vencida, isolamento entre dois
usuários em todas as rotas e herança das reuniões antigas. Contra o MongoDB do compose (banco
de rascunho): e-mail duplicado recusado pelo índice único, índice TTL criado, sessão guardada
como hash. **Não verificado:** a tela de login do Streamlit com uma conta real criada pelo dono.

**Reverte-se assim.** Sem `Bearer`, a chave de serviço continua abrindo tudo; `owner_id` é um
campo a mais no documento.

---

## ADR-012 — Bot multiplataforma

**Contexto.** O bot só abria links do Google Meet. O pedido é gravar também Teams, Zoom e Discord.

**Decisão.**
- A captura não muda: é o áudio da aba, pela mesma extensão (agora autorizada nos domínios do
  Teams e do Zoom). O que varia por plataforma fica em `infrastructure/recorder/platforms.py`:
  endereço que abre a reunião no navegador, passos até entrar e como ler o estado da chamada.
- O link é validado no despacho (`detect_platform`): https e domínio de uma plataforma conhecida.
- **Meet:** fluxo existente, inalterado. Única plataforma com nome de quem fala (legendas).
- **Teams e Zoom:** o bot entra como convidado pelo cliente web, com o nome `BOT_NAME`, microfone
  e câmera desligados. O link do Zoom é convertido para o cliente web (`app.zoom.us/wc/<id>/join`).
  A transcrição sai com "Speaker N".
- **Discord: adiado.** Automatizar uma conta de usuário pelo navegador viola os termos do Discord
  e arrisca banir a conta. O caminho correto é um bot oficial (token do Developer Portal) que
  entra no canal de voz e recebe o áudio, sem navegador: outra arquitetura de captura, que
  precisa do token e de um servidor de teste. O link do Discord é recusado com essa explicação.

**Estado (2026-11-15), sem rodeio.**
- Meet: validado em reunião real, de novo depois desta mudança (teste de fumaça).
- Zoom: confirmado só que o endereço do cliente web existe e redireciona como esperado. Os
  passos de entrada (nome, termos de uso, "áudio do computador") **nunca rodaram numa reunião**.
- Teams: **nada foi executado contra o Teams**; os seletores vêm da tela conhecida do cliente web.
- Nos dois, o primeiro uso deve ser `run_bot --url <link> --smoke 30` com `BOT_DEBUG_DOM=true`.
  O bot avisa no log que a plataforma não foi validada, e `GET /config` informa `validated: false`.

**Riscos conhecidos fora do Meet.**
- Sem contagem de participantes confiável, o bot não sai sozinho quando fica só: sai quando a
  reunião é encerrada ou a aba para de produzir áudio (`SILENCE_TIMEOUT`).
- O Zoom exige aceitar os termos de uso a cada navegador novo; o bot aceita para conseguir entrar.
- Reuniões que exigem conta logada (política da organização) não aceitam o bot como convidado.
- Se o botão de mudo mudar, o bot pode entrar com o microfone aberto: conferir no teste de fumaça.

**Alternativas descartadas.**
- APIs oficiais de gravação (Graph, Zoom Cloud Recording): exigem conta paga e consentimento de
  administrador de cada organização; o bot convidado funciona com qualquer link.
- Um recorder por plataforma: duplicaria captura, admissão e fim de reunião, que são iguais.

**Reverte-se assim.** Remover a entrada da plataforma em `PLATFORMS` volta a recusar o link.

---

## ADR-013 — Contrato OpenAPI para um novo front

**Contexto.** O Streamlit chama o container direto, sem passar pela API. Um front novo precisa
de uma API completa e documentada antes de ser escrito.

**Decisão.**
- O contrato é o OpenAPI gerado pelo FastAPI: Swagger em `/docs`, arquivo versionado em
  `docs/api/openapi.json` (gerado por `python -m api.export_openapi`, dentro do container).
- A API passou a cobrir tudo o que o Streamlit faz: cadastro e sessão (`/auth/*`), detalhe
  completo da reunião com transcrição e conversa (`GET /meetings/{id}`), exclusão
  (`DELETE /meetings/{id}`) e `GET /config` (cadastro aberto? e-mail ligado? quais plataformas?).
- Toda rota tem resumo, modelos de resposta e erros documentados; os esquemas de segurança
  (`Bearer` e `X-API-Key`) aparecem no botão Authorize do Swagger.
- Um teste falha se as rotas ou os modelos do arquivo divergirem do código.

**Fora do escopo, de propósito.** Nenhum front novo foi escrito e o Streamlit não foi migrado
para a API. Paginação da lista e atualização por push (WebSocket/SSE) ficam para quando o
front precisar: hoje o acompanhamento é consulta periódica a `/status`.

**Antes de um front em outro domínio:** incluir a origem dele em `ALLOWED_ORIGINS`.

**Reverte-se assim.** As rotas novas são aditivas.
