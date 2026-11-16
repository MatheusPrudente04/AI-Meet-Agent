# Repositório de reuniões - Interface de contrato para persistência de reuniões, 
# garantindo a independência do domínio em relação à tecnologia de armazenamento utilizada (ex: banco de dados, arquivos, etc.)
from abc import ABC, abstractmethod
from typing import Optional 
from domain.entities.meeting import Meeting

# Definindo classe de contrato para persistência de reuniões, com métodos para salvar, buscar por ID, listar todas e deletar reuniões por ID
class IMeetingRepository(ABC):
    """Contrato para persistência de reuniões"""

    # Metodo de salvar ou atualizar uma reunião
    @abstractmethod
    def save(self, meeting: Meeting) -> None:
        """Persiste ou atualize uma reunião."""
        ...

    # Metodo de buscar de reunião por ID, retornando a reunião ou None se não encontrar 
    @abstractmethod
    def find_by_id(self, meeting_id: str) -> Optional[Meeting]:
        """Retorna reunião por ID ou None se não encontrar"""
        ...

    # Metodo de listar todas as reuniões salvas, organizadas por data de inicio da reunião 
    @abstractmethod
    def list_all(self, owner_id: Optional[str] = None) -> list[Meeting]:
        """Reuniões organizadas por data. Com `owner_id`, só as daquele usuário."""
        ...

    @abstractmethod
    def find_by(self, **fields) -> list[Meeting]:
        """Reuniões cujos campos são iguais aos informados (ex.: source_id="...")."""
        ...

    # Metodo de deletar reunião por ID, removendo a reunnião do armazenamento persistente
    @abstractmethod
    def delete(self, meeting_id: str) -> None:
        """Remove uma reunião pelo ID"""
        ...

    def count_by_status(self) -> dict[str, int]:
        """Reuniões por status (observabilidade). Implementações podem contar no banco."""
        counts: dict[str, int] = {}
        for m in self.list_all():
            counts[m.status.value] = counts.get(m.status.value, 0) + 1
        return counts

    # A conversa tem dono próprio: só o chat escreve nela, e só ela. `save` não toca em
    # chat_history (preserva o que está gravado). Com save do documento inteiro, a resposta do
    # chat gravada depois do resumo apagava o resumo (e o resumo apagava perguntas), 2026-11-15.
    def append_chat(self, meeting_id: str, messages: list[dict]) -> None:
        raise NotImplementedError

    def set_chat_history(self, meeting_id: str, chat_history: list[dict]) -> None:
        raise NotImplementedError
