# Notas de versão

## v1.0.0 — 2026-11-16 (produção local)

Primeira versão fechada. Roda completa com Docker numa máquina (macOS validado; Windows com
Docker Desktop preparado, sem teste real). A implantação num servidor com HTTPS está pronta no
repositório (`docker-compose.prod.yml`, `docs/operacao.md`, `docs/seguranca.md`), mas não foi feita:
é a Sprint 11.

### O que a 1.0 faz
- **Bot nas reuniões**, em container: entra no Google Meet pelo link, espera ser admitido, grava o
  áudio e registra quem estava e quem falou (legendas do Meet). Sai 50 s depois de ficar sozinho.
  Credenciado pela interface (botão "Credenciar bot"). Zoom e Teams: escritos, **não validados**.
- **Transcrição e resumo** (Whisper local + LLM local): visão geral, tópicos, tarefas com
  responsável, decisões com estado (aprovada, proposta, pendente…), seguindo o contrato do agente.
- **Chat sobre a reunião**, verificado contra a transcrição antes de responder (avaliação do
  contrato: 3/8 → 7-8/8), com **ações**: e-mail (só para endereço que o usuário escreveu e só com
  confirmação), documentos em PDF, Word, Excel, CSV, HTML com gráficos e Markdown, e notas de memória.
- **Memória entre reuniões** (§14): decisões e pendências das outras reuniões do mesmo usuário,
  com origem e data, e preferências que o usuário pede para lembrar.
- **Contas**: cadastro, login, sessões, recuperação de senha por e-mail, admin; cada usuário vê só
  as próprias reuniões; mesmo link para duas pessoas = uma gravação, duas reuniões.
- **Vários usuários ao mesmo tempo**: filas separadas para transcrição e resumo, prioridade justa,
  posição na fila, limite de pedidos por minuto.
- **Acompanhamento**: tempo de cada etapa, quanto ela costuma levar, aviso e botão "Retomar"
  quando uma etapa trava (worker reiniciado no meio).
- **Observabilidade**: métricas (JSON e Prometheus), logs de todos os serviços num lugar só,
  linha do tempo por reunião, saúde dos workers por batimento.
- **Agenda do Google** (opcional): próximas reuniões com "Mandar o bot"; aguardando as credenciais.
- **Rede local**: interface e API acessíveis de outros aparelhos; banco e fila só na máquina.
- **API para um front novo**: contrato OpenAPI em `docs/api/openapi.json`.

### Corrigido nesta versão (achados em uso)
- Pergunta no chat durante o resumo apagava o resumo (e o resumo apagava perguntas).
- Botões do Meet ("Aumentar zoom", "Liberar a apresentação…") viravam participantes.
- `/health` dava "sem worker" durante todo trabalho normal.
- Transcrição vazia com o bot em container (filtro anti-alucinação descartava fala nítida).
- Demora do LLM: janela de contexto variável recarregava o modelo (~50 s por troca).

### Limites conhecidos
- Máquina com 16 GB fica em swap com Ollama + Whisper + Docker: transcrição e respostas lentas.
  Propostas na ADR-021 (Ollama com flash attention e cache q8, Whisper `small`, contrato compacto).
- O bot enxerga o nome de exibição, não o e-mail; fala sem legenda fica como "Speaker N".
- Discord depois da 1.0 (exige bot oficial). Sem HTTPS fora do compose de produção.

### Atualizando a partir da versão de desenvolvimento
- `docker compose up -d --build --remove-orphans` (o serviço `worker` virou `worker-transcribe` e `worker-llm`).
- Backup: `docker compose run --rm backup`.
- Credenciais do Google ficam em `data/google/` (fora do git).
