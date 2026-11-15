# Notas de versão

## v1.0.0 — rascunho (S11-6)

A tag sai quando a Sprint 11 fechar: servidor no ar, checklist de `docs/seguranca.md` assinado e
teste de carga da ADR-015 repetido no servidor.

### O que a 1.0 faz
- **Bot nas reuniões:** entra no Google Meet pelo link, espera ser admitido, grava o áudio da aba
  e registra quem estava e quem falou (pelas legendas do Meet). Teams e Zoom estão escritos, mas
  **não validados**.
- **Transcrição e resumo:** Whisper local; resumo com visão geral, tópicos, tarefas com
  responsável e decisões com estado (aprovada, proposta, pendente…), seguindo o contrato do
  agente (`SPEC_CONTRACT.md`). Reuniões longas são resumidas em partes.
- **Chat sobre a reunião:** respostas verificadas contra a transcrição antes de sair (citações,
  horários, quem disse). Na avaliação do contrato, passou de 3/8 para 8/8.
- **Contas:** cadastro, login, sessões por aparelho, recuperação de senha por e-mail e limpeza de
  sessões pelo admin. Cada usuário vê só as próprias reuniões; dois usuários no mesmo link
  compartilham uma gravação e recebem reuniões independentes.
- **Vários usuários ao mesmo tempo:** filas separadas para transcrição e resumo, o 1º envio de
  cada usuário passa na frente do 2º de qualquer outro, posição na fila visível e limite de
  pedidos por minuto.
- **API para um front novo:** contrato OpenAPI em `docs/api/openapi.json`.
- **Operação:** compose de produção com HTTPS (Caddy), backup e restauração pelo compose,
  `/health` com banco, fila e workers, alerta por e-mail, roteiro em `docs/operacao.md`.

### Limites conhecidos
- O bot enxerga o nome de exibição dos participantes, não o e-mail.
- Fala sem legenda do Meet fica como "Speaker N".
- Discord fica para depois da 1.0 (exige bot oficial).
- Não há memória entre reuniões: o chat responde só sobre a reunião aberta.

### Atualizando a partir da versão de desenvolvimento
- O serviço `worker` foi dividido em `worker-transcribe` e `worker-llm`: suba com `--remove-orphans`.
- O backup agora é `docker compose run --rm backup` (`scripts/backup.sh` continua como atalho).
- O venv local é `.venv` em Python 3.11; o `venv/` antigo (3.14) pode ser apagado depois de trocar o worker do bot.
