"""
tests/unit/infrastructure/test_prompt_builder.py

O PromptBuilder é uma classe pura — testável sem chamar nenhuma API.
"""

import json
import pytest
from domain.entities.meeting_type import MeetingType
from infrastructure.llm.prompt_builder import PromptBuilder

# TODOs para futuro:
# Adicionar testes de edge cases e validação de formato JSON


@pytest.fixture
def builder() -> PromptBuilder:
    return PromptBuilder()

# Teste para o PromptBuilder, garatindo que os prompts geradas para cada tipo de reunião contenham as intruções esperadas
class TestPromptBuilderSummarize:

    def test_system_prompt_contem_regras_base(self, builder):
        prompt = builder.build_summarize_system(MeetingType.GENERAL)
        assert "JSON válido" in prompt
        assert "português do Brasil" in prompt
        assert "overview" in prompt
        assert "tasks" in prompt

    def test_planning_inclui_instrucoes_de_riscos(self, builder):
        prompt = builder.build_summarize_system(MeetingType.PLANNING)
        assert "riscos" in prompt.lower()
        assert "estimativa" in prompt.lower()

    def test_retro_inclui_instrucoes_went_well(self, builder):
        prompt = builder.build_summarize_system(MeetingType.RETROSPECTIVE)
        assert "went_well" in prompt
        assert "to_improve" in prompt
        assert "action_items" in prompt

    def test_one_on_one_inclui_instrucoes_de_feedback(self, builder):
        prompt = builder.build_summarize_system(MeetingType.ONE_ON_ONE)
        assert "feedbacks" in prompt
        assert "confidencialidade" in prompt.lower()

    def test_review_inclui_delivered_not_delivered(self, builder):
        prompt = builder.build_summarize_system(MeetingType.REVIEW)
        assert "delivered" in prompt
        assert "not_delivered" in prompt

    def test_interview_inclui_strengths_concerns(self, builder):
        prompt = builder.build_summarize_system(MeetingType.INTERVIEW)
        assert "strengths" in prompt
        assert "concerns" in prompt

    def test_brainstorm_inclui_ideas(self, builder):
        prompt = builder.build_summarize_system(MeetingType.BRAINSTORM)
        assert "ideas" in prompt

    def test_few_shot_incluido_em_planning(self, builder):
        prompt = builder.build_summarize_system(MeetingType.PLANNING)
        assert "Exemplo de saída" in prompt

    def test_few_shot_nao_incluido_em_general(self, builder):
        prompt = builder.build_summarize_system(MeetingType.GENERAL)
        assert "Exemplo de saída" not in prompt

    def test_user_prompt_inclui_transcricao(self, builder):
        transcript = "Fulano: Bom dia. Ciclana: Olá."
        prompt = builder.build_summarize_user(transcript)
        assert "Fulano: Bom dia. Ciclana: Olá." in prompt
        assert "TRANSCRIÇÃO" in prompt

    def test_prompts_diferentes_por_tipo(self, builder):
        planning = builder.build_summarize_system(MeetingType.PLANNING)
        retro    = builder.build_summarize_system(MeetingType.RETROSPECTIVE)
        assert planning != retro

    def test_todos_tipos_geram_prompt(self, builder):
        """Nenhum tipo deve levantar exceção ao gerar o prompt."""
        for mt in MeetingType:
            prompt = builder.build_summarize_system(mt)
            assert len(prompt) > 100

# Classe teste para o PrompBuilder na construção do prompt de chat, garatindo que o prompt de system
class TestPromptBuilderChat:

    def test_chat_system_inclui_transcricao(self, builder):
        transcript = "Ana: Vamos começar. Carlos: Ok."
        prompt = builder.build_chat_system(transcript)
        assert "Ana: Vamos começar. Carlos: Ok." in prompt

    def test_chat_system_instrui_sobre_ausencia_de_info(self, builder):
        prompt = builder.build_chat_system("transcrição qualquer")
        assert "não" in prompt.lower() and ("invente" in prompt.lower() or "informa" in prompt.lower())

    def test_chat_system_instrui_citar_contexto(self, builder):
        prompt = builder.build_chat_system("transcrição")
        assert "SPEAKER" in prompt or "contexto" in prompt.lower()


class TestMeetingType:

    def test_label_em_portugues(self):
        assert MeetingType.PLANNING.label == "Planejamento"
        assert MeetingType.RETROSPECTIVE.label == "Retrospectiva"
        assert MeetingType.ONE_ON_ONE.label == "1:1"

    def test_focus_areas_nao_vazio(self):
        for mt in MeetingType:
            assert len(mt.focus_areas) > 0

    def test_planning_foca_em_tarefas_e_riscos(self):
        areas = MeetingType.PLANNING.focus_areas
        assert any("tarefa" in a.lower() or "objetivo" in a.lower() for a in areas)

    def test_retro_foca_em_melhoria(self):
        areas = MeetingType.RETROSPECTIVE.focus_areas
        assert any("melhor" in a.lower() or "ação" in a.lower() for a in areas)

# Integração do PromptBuilder com o agente (ADR-014): o tipo de reunião chega ao prompt do resumo.
class TestIntegracaoAgenteComTipo:

    @staticmethod
    def _agent():
        import json
        from infrastructure.llm.meeting_agent import MeetingAgent

        class Model:
            calls = []
            def complete(self, messages, as_json=False):
                self.calls.append(messages)
                return json.dumps({"overview": "ok", "topics": [], "tasks": [], "decisions": []})

        model = Model()
        return MeetingAgent(model=model, contract=""), model

    def test_summarize_passa_meeting_type_ao_builder(self):
        agent, model = self._agent()
        agent.summarize("transcrição", MeetingType.PLANNING)
        system_content = model.calls[-1][0]["content"]
        assert "planning" in system_content.lower() or "planejamento" in system_content.lower()

    def test_general_e_planning_geram_system_prompts_diferentes(self):
        agent, model = self._agent()
        agent.summarize("transcrição", MeetingType.GENERAL)
        agent.summarize("transcrição", MeetingType.PLANNING)
        assert model.calls[0][0]["content"] != model.calls[1][0]["content"]
