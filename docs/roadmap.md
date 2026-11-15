# Roteiro para produção — Meet Agent

Ordem por prioridade: primeiro o que impede o fluxo de funcionar, depois o que impede de
manter, depois o que impede de expor, e só então evolução. Cada sprint tem um critério de saída
verificável; não se inicia a seguinte sem ele.

Decisões de arquitetura: [docs/adr/README.md](adr/README.md).
Convenção de id: `S<sprint>-<n>`. Tipo: `fix`, `feat`, `refactor`, `chore`.

## Sprint 0 — Linha de base (1 a 2 dias)

**Concluída em 2026-11-14.** Bot gravou uma reunião real de 10 min.

Objetivo: estado atual versionado, testes verdes, bot gravando.

| Id | Tipo | Tarefa | Pronto quando |
|----|------|--------|---------------|
| S0-1 | chore | Ignorar `data/` inteiro, `security_config.json`, `security_audits/`, `htmlcov/`, `.qodo/`; criar branch e commitar a árvore atual | `git status` limpo; nenhum arquivo de `data/` rastreado |
| S0-2 | fix | `chrome.runstime` → `chrome.runtime` em `extension/background.js:16` | Uma reunião real gera `data/audio/<id>.webm` com tamanho > 0 |
| S0-3 | fix | Remover `test/test_audio_capture_local.py` (testa arquivo apagado) | `pytest` coleta sem erro |
| S0-4 | fix | `LangChainLLMService.summarize` usar o cliente injetado; provedor decidido no construtor | Os 3 testes de `test_prompt_builder.py` passam sem Ollama no ar |
| S0-5 | fix | Renomear `dockerfile.streamlit` → `Dockerfile.streamlit`; corrigir `requirements_diarization.txt` | `docker compose build` passa |
| S0-6 | fix | Status `fallback_legendas` fora do enum; `MONGO_DB_NAME` vs `MONGO_DB` | `GET /meetings/{id}/status` não devolve 500; nome do banco vem do `.env` |

Saída: `pytest` verde com o gate de cobertura atual, e um `.webm` real gravado pelo bot.

## Sprint 1 — Consolidação (ADR-001 a 005)

**Concluída em 2026-11-14.** Desvios registrados em [adr/README.md](adr/README.md). S1-8 não foi necessária.

Objetivo: um caminho de execução, um schema, uma fonte de status. É a sprint que mais apaga código.

| Id | Tipo | Tarefa | Pronto quando |
|----|------|--------|---------------|
| S1-1 | refactor | Enum de status e campos `status`/`error` na entidade `Meeting`; `update_status` no contrato do repositório | Teste de repositório cobre gravação e leitura de status nos dois backends |
| S1-2 | refactor | Repositório Mongo síncrono com PyMongo e schema único; JSON persistindo os mesmos campos | Ida e volta de um `Meeting` completo sem perda de campo; Beanie e Motor fora dos requirements |
| S1-3 | feat | `ProcessMeetingUC` com status por etapa, erro persistido e idempotência por id | Teste: falha de transcrição deixa `status=error` com mensagem; reprocessar o mesmo id não duplica |
| S1-4 | refactor | Celery, upload da API e Streamlit chamando só `ProcessMeetingUC` | Nenhuma outra sequência transcrever→resumir no código |
| S1-5 | refactor | Remover orquestradores, agentes, repositórios e storages órfãos (lista na ADR-001 e ADR-009 fase 1), `_FallbackTask`, scripts quebrados | Busca pelos nomes removidos não retorna nada; testes verdes |
| S1-6 | refactor | Portas num só lugar (`domain/interfaces/`); remover `interface/` e `repositories/` | Imports atualizados; testes verdes |
| S1-7 | refactor | Settings como única leitura de ambiente, validação na partida em collab, `.env.example` | Subir collab sem `MONGO_URI` falha com mensagem clara |
| S1-8 | chore | Script de migração dos documentos existentes para o schema novo | Reuniões antigas aparecem na UI com status correto |

Saída: upload manual funciona em solo e em collab pelo mesmo caso de uso, e uma falha forçada
aparece como erro na UI.

## Sprint 2 — Bot em produção (ADR-006)

**Implementada em 2026-11-14; falta o critério de saída** (três reuniões reais pelo fluxo da fila).
Validado: compose completo no ar, upload real processado no container até `done`, fila `bot`
entregando ao worker do host e falha persistida. S2-6 feita em 2026-11-15:
`python -m infrastructure.recorder.run_bot --url <link> --smoke 30` entra, grava 30 s, confere
áudio, legendas, presentes e bloco do bot, apaga o que criou e sai com código 1 se algo faltou.

Como subir:

    docker compose up -d --build        # api :8001, streamlit :8501, redis :6380, mongo :27018
    python main.py --bot-worker         # no host, em outro terminal

Objetivo: URL do Meet entra, reunião resumida sai, sem intervenção e sem bloquear nada.

| Id | Tipo | Tarefa | Pronto quando |
|----|------|--------|---------------|
| S2-1 | feat | Task `join_meeting` na fila `bot`; `meeting_id` criado no despacho e usado como sessão de captura | `POST /meetings/bot` responde em menos de 1 s com o id |
| S2-2 | refactor | Recorder recebe o id e o endpoint de captura por parâmetro; grava `started_at` real; atualiza status | Documento passa por `recording` → `transcribing` → `summarizing` → `done` |
| S2-3 | feat | Worker do bot na fila `bot`, concorrência 1. **No host por enquanto** (ver ADR-006); container fica para quando houver servidor | Bot grava uma reunião despachada pela fila |
| S2-4 | refactor | Streamlit despacha pela fila e mostra status do documento; remover subprocesso e leitura de `bot_session_*.json` | UI mostra o andamento sem Refresh manual do processo |
| S2-5 | fix | Encerramento: áudio parcial é processado quando o bot cai ou recebe SIGTERM | Matar o container no meio da reunião ainda gera resumo do que foi gravado |
| S2-6 | feat | Teste de fumaça do bot contra uma reunião de teste, executável sob demanda | Um comando valida entrar, gravar 30 s e sair |

Saída: três reuniões reais seguidas processadas de ponta a ponta no ambiente de compose.

## Sprint 2B — Quem estava e quem falou o quê (ADR-009 fase 2)

Antecipada da Sprint 4 em 2026-11-14: hoje o chat responde "Speaker 0 / Speaker 1" e o produto
precisa do nome de cada participante. Vem antes do endurecimento porque a implantação atual é
local; a Sprint 3 continua obrigatória antes de expor a API fora da máquina.

Objetivo: a reunião guarda quem esteve presente e a transcrição atribui cada fala ao nome real.

| Id | Tipo | Tarefa | Pronto quando |
|----|------|--------|---------------|
| S2B-1 | chore | Diagnóstico do DOM do Meet: com `BOT_DEBUG_DOM=true` o bot salva amostras dos blocos de participantes e de legendas durante uma reunião real | Arquivo `data/audio/<id>.dom.jsonl` de uma reunião com 2+ pessoas; seletores escolhidos a partir dele |
| S2B-2 | feat | Extensão: observar legendas (nome de quem fala + instante) e a lista de participantes, e enviar para a API | Eventos chegam em `data/audio/<id>.speakers.jsonl` durante a reunião |
| S2B-3 | feat | API: `POST /capture/speakers` grava os eventos da sessão | Teste cobre gravação e sessão inválida |
| S2B-4 | feat | Alinhamento: atribuir a cada segmento do Whisper o nome com maior sobreposição de tempo, tolerando o atraso das legendas; sem evidência, mantém "Speaker N" | Função pura com testes: sobreposição, atraso, troca rápida de falante, ausência de eventos |
| S2B-5 | feat | `ProcessMeetingUC` aplica o alinhamento; `participants` passa a ser a lista de nomes reais (sem o bot) | Reunião real: transcrição com nomes; campo `participants` com os presentes |
| S2B-6 | feat | Chat e resumo recebem a lista de participantes no prompt | Pergunta "quem estava na reunião?" devolve os nomes; tarefas saem com responsável pelo nome |
| S2B-7 | chore | Teste de fumaça dos seletores, executável sob demanda (entra com o S2-6) | Um comando acusa quando o Meet mudou a tela |

Saída: reunião real com 3 pessoas em que o chat lista os 3 nomes e a transcrição atribui as falas corretamente.

**Estado em 2026-11-15:** S2B-1 a S2B-6 implementadas e validadas numa reunião real com a conta
do dono como anfitriã: o bot esperou a admissão, ligou as legendas, registrou o anfitrião como
presente e retirou o próprio nome. Detalhes na ADR-009.
- A leitura das legendas ficou no recorder, não na extensão; `POST /capture/speakers` não existe.
- S2B-7 é o mesmo comando da S2-6 (`run_bot --smoke`); passou numa reunião real em 2026-11-15.
- **Falta:** validar o nome de terceiros nas legendas com duas ou mais pessoas falando (critério de
  saída).

Limites conhecidos, para não prometer o que o Meet não entrega:
- O bot enxerga o **nome de exibição** de cada participante, não o e-mail nem um id estável da conta.
- Quem entrou e nunca falou aparece em `participants`, mas não na transcrição.
- A atribuição depende das legendas do Meet estarem ligadas (o bot liga) e do Meet reconhecer a fala;
  trechos sem legenda ficam como "Speaker N".
- Os seletores do Meet mudam sem aviso; por isso o S2B-7.

## Sprint 3 — Endurecimento (ADR-007, ADR-008)

**Implementada em 2026-11-15.** Pendências: recriar o venv local em 3.11 (S3-4), ver o CI rodar no
GitHub depois do push (S3-6) e o critério de saída (implantar num servidor limpo). S3-3 já estava
feita desde a Sprint 1. Os `print` que restam são saída de linha de comando (`main.py --test`,
`bot_setup`, `run_bot`).

Objetivo: seguro para expor e reproduzível para implantar.

| Id | Tipo | Tarefa | Pronto quando |
|----|------|--------|---------------|
| S3-1 | feat | Chave de API em todas as rotas; token por sessão em `/capture/*` | Chamada sem credencial devolve 401; teste cobre |
| S3-2 | fix | Upload em streaming com teto; limite por chunk e por sessão de captura | Arquivo acima do teto devolve 413 sem encher memória |
| S3-3 | fix | Remover `/meetings/bot/done` e qualquer caminho de arquivo vindo do cliente | Nenhum schema de entrada tem campo de caminho |
| S3-4 | chore | Python 3.11, `pip-compile`, requirements separados, imagem única para api/worker/streamlit | Build reproduzível; venv local recriado em 3.11 com testes verdes |
| S3-5 | feat | Logging padrão com `meeting_id` em toda linha; remover `print`; `/health` checando Mongo e Redis | Dá para seguir uma reunião pelos logs só com o id |
| S3-6 | chore | CI: testes e build de imagens a cada push | Push com teste quebrado falha no CI |
| S3-7 | chore | Backup do volume do Mongo e política de retenção dos `.webm` | Restauração testada uma vez; áudio antigo é apagado por regra |
| S3-8 | chore | Reescrever o README para o que existe | Alguém sobe o ambiente seguindo só o README |

Saída: implantação em um servidor limpo a partir do repositório, com API fechada por chave.

## Sprint 4 — Qualidade e escala (ADR-009, ADR-010)

**Implementada em 2026-11-15.** Medições e decisão na ADR-010. S4-5: exportação Markdown sempre
ligada, e-mail de tarefas com `SMTP_USER`/`SMTP_PASSWORD`, agenda com `ENABLE_CALENDAR` (dá o
título da reunião). E-mail e agenda foram testados com serviços simulados, não com SMTP nem com a
API do Google de verdade.

Objetivo: melhorar o resultado, com medição antes de cada mudança.

| Id | Tipo | Tarefa | Pronto quando |
|----|------|--------|---------------|
| S4-1 | chore | Medir tokens por reunião, tempo de transcrição e taxa de falha por provedor | Números registrados na ADR-010 |
| S4-2 | feat | Estratégia de contexto escolhida na ADR-010 | Reunião de 2 h resume e responde no chat sem erro de contexto |
| S4-3 | — | Movida para a Sprint 2B a pedido do dono do produto | — |
| S4-4 | feat | Persistir conclusão de tarefas e histórico de chat | Marcar tarefa sobrevive a recarregar a página |
| S4-5 | feat | Reativar o que já existe e está desligado, um por vez: e-mail de tarefas, exportação Markdown, Google Calendar | Cada um com teste e chave de configuração própria |

## Sprint 5 — Usuários, plataformas e contrato da API (ADR-011, 012, 013)

**Implementada em 2026-11-15**, com as ressalvas de S5-5 e S5-6.

Objetivo: cada pessoa com a própria conta e as próprias reuniões; bot além do Meet; API pronta
para um front novo.

| Id | Tipo | Tarefa | Estado |
|----|------|--------|--------|
| S5-1 | feat | Cadastro e login; senha com scrypt; sessões na coleção `sessions` do MongoDB com validade e TTL | Feito; testado contra o Mongo do compose |
| S5-2 | feat | `owner_id` na reunião; cada usuário vê só as suas; a primeira conta herda as antigas | Feito; teste cobre todas as rotas |
| S5-3 | feat | Tela de entrar e criar conta no Streamlit; botão Sair | Feito; falta o dono criar a primeira conta |
| S5-4 | feat | Camada de plataformas no bot; link validado no despacho | Feito; Meet revalidado em reunião real |
| S5-5 | feat | Teams e Zoom pelo cliente web, como convidado | **Escrito, não validado**: precisa de uma reunião real de cada |
| S5-6 | feat | Discord | **Adiado** (ADR-012): exige bot oficial com token, não navegador |
| S5-7 | chore | OpenAPI completo em `/docs` e `docs/api/openapi.json`; rotas que faltavam para um front | Feito |

Saída: duas contas, cada uma enviando o bot a uma reunião e vendo só a própria. **Pendente** (depende de pessoas).

## Sprint 6 — Validação com serviços reais (próxima)

| Id | Tipo | Tarefa | Pronto quando |
|----|------|--------|---------------|
| S6-1 | chore | Teste de fumaça do bot no Zoom e no Teams, corrigindo seletores a partir do `.dom.jsonl` | `run_bot --smoke` verde nas duas |
| S6-2 | feat | Nome de quem fala no Teams e no Zoom (legendas de cada um) | Transcrição com nomes fora do Meet |
| S6-3 | chore | E-mail de tarefas com SMTP real | E-mail recebido numa caixa de verdade |
| S6-4 | chore | Google Calendar com a API real | Título do bot vindo de um evento da agenda |
| S6-5 | feat | Recuperação de senha por e-mail e limite de tentativas de login | Pré-requisito para expor a API |
| S6-6 | feat | Discord por bot oficial (token do Developer Portal) | Canal de voz gravado e resumido |
| S6-7 | chore | venv local em Python 3.11; CI verde no GitHub | Mesma versão no host e nos containers |

## Fica para a implantação em produção

Servidor limpo a partir do repositório (critério de saída da Sprint 3), HTTPS, domínio e
`ALLOWED_ORIGINS` do front novo.

## Fora do roteiro por enquanto

Mais de uma organização (times com reuniões compartilhadas) e o front novo em si: o contrato
está em `docs/api/openapi.json` (ADR-013).
