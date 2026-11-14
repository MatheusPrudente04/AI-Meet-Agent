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

Objetivo: URL do Meet entra, reunião resumida sai, sem intervenção e sem bloquear nada.

| Id | Tipo | Tarefa | Pronto quando |
|----|------|--------|---------------|
| S2-1 | feat | Task `join_meeting` na fila `bot`; `meeting_id` criado no despacho e usado como sessão de captura | `POST /meetings/bot` responde em menos de 1 s com o id |
| S2-2 | refactor | Recorder recebe o id e o endpoint de captura por parâmetro; grava `started_at` real; atualiza status | Documento passa por `recording` → `transcribing` → `summarizing` → `done` |
| S2-3 | feat | Imagem e serviço `bot-worker` (Chrome + Xvfb, perfil em volume, concorrência 1) | Bot grava uma reunião rodando dentro do container |
| S2-4 | refactor | Streamlit despacha pela fila e mostra status do documento; remover subprocesso e leitura de `bot_session_*.json` | UI mostra o andamento sem Refresh manual do processo |
| S2-5 | fix | Encerramento: áudio parcial é processado quando o bot cai ou recebe SIGTERM | Matar o container no meio da reunião ainda gera resumo do que foi gravado |
| S2-6 | feat | Teste de fumaça do bot contra uma reunião de teste, executável sob demanda | Um comando valida entrar, gravar 30 s e sair |

Saída: três reuniões reais seguidas processadas de ponta a ponta no ambiente de compose.

## Sprint 3 — Endurecimento (ADR-007, ADR-008)

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

Objetivo: melhorar o resultado, com medição antes de cada mudança.

| Id | Tipo | Tarefa | Pronto quando |
|----|------|--------|---------------|
| S4-1 | chore | Medir tokens por reunião, tempo de transcrição e taxa de falha por provedor | Números registrados na ADR-010 |
| S4-2 | feat | Estratégia de contexto escolhida na ADR-010 | Reunião de 2 h resume e responde no chat sem erro de contexto |
| S4-3 | feat | Captura de legendas pela extensão e nomeação de falantes (ADR-009 fase 2, se aceita) | Transcrição mostra nomes reais em reunião de teste com 3 pessoas |
| S4-4 | feat | Persistir conclusão de tarefas e histórico de chat | Marcar tarefa sobrevive a recarregar a página |
| S4-5 | feat | Reativar o que já existe e está desligado, um por vez: e-mail de tarefas, exportação Markdown, Google Calendar | Cada um com teste e chave de configuração própria |

## Fora do roteiro por enquanto

Multiusuário com login, mais de uma organização, outras plataformas além do Meet, troca de
Streamlit por outro front. Entram quando houver demanda concreta; a ADR-007 já registra o gatilho
para autenticação por usuário.
