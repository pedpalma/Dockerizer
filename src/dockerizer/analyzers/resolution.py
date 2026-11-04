"""Perguntas para informações não identificadas com segurança e aplicação das respostas."""

from __future__ import annotations

import shlex

from dockerizer.analyzers.infrastructure import SPECS, has_usage_signal
from dockerizer.models.architecture import (
    Command,
    Confidence,
    Framework,
    InfraService,
    ProcessKind,
    ProjectAnalysis,
    Question,
    QuestionKind,
    ServiceDecision,
    ServiceKind,
)

YES = "sim"
NO = "nao"


class AnswerError(ValueError):
    """Resposta inválida para uma pergunta."""


def build_questions(analysis: ProjectAnalysis) -> list[Question]:
    questions: list[Question] = []
    for app in analysis.applications:
        for process in app.processes:
            if process.kind is ProcessKind.STATIC and process.command is not None:
                continue
            command = process.command
            if command is None or command.confidence is Confidence.SUGGESTED:
                choices = [c.display() for c in process.candidates]
                default = command.display() if command else (choices[0] if choices else None)
                questions.append(
                    Question(
                        id=f"command:{app.name}:{process.name}",
                        kind=QuestionKind.COMMAND,
                        prompt=(
                            f"Comando de inicialização do processo '{process.name}' da aplicação "
                            f"'{app.name}' ({app.path})"
                        ),
                        target=f"command:{app.name}:{process.name}",
                        default=default,
                        choices=choices,
                        help="Não foi possível identificá-lo com segurança. "
                        "Nenhum script do projeto foi executado durante a análise.",
                    )
                )
            if process.kind is ProcessKind.WEB and process.port is None:
                questions.append(
                    Question(
                        id=f"port:{app.name}:{process.name}",
                        kind=QuestionKind.TEXT,
                        prompt=f"Porta em que a aplicação '{app.name}' escuta dentro do container",
                        target=f"port:{app.name}:{process.name}",
                    )
                )
        if app.has_framework(Framework.REACT) and app.static_output_dir is None:
            questions.append(
                Question(
                    id=f"static_dir:{app.name}",
                    kind=QuestionKind.TEXT,
                    prompt=f"Diretório gerado pelo build da aplicação '{app.name}' (ex.: dist)",
                    target=f"static_dir:{app.name}",
                )
            )
    for service in analysis.services:
        if service.decision is ServiceDecision.UNDETERMINED:
            questions.append(_service_question(service))
    questions.extend(_broker_questions(analysis))
    return questions


def _service_question(service: InfraService) -> Question:
    spec = SPECS[service.kind]
    evidence = "; ".join(e.description for e in service.evidence[:4])
    return Question(
        id=f"service:{service.kind.value}",
        kind=QuestionKind.CHOICE,
        prompt=f"Criar serviço {spec.label} local no Compose?",
        target=f"service:{service.kind.value}",
        choices=[YES, NO],
        default=YES if has_usage_signal(service) else NO,
        help=f"{service.reason} Evidências: {evidence}. "
        "Responda 'nao' se o projeto utiliza um serviço externo ou não precisa dele.",
    )


def _broker_questions(analysis: ProjectAnalysis) -> list[Question]:
    questions = []
    brokers = {ServiceKind.REDIS, ServiceKind.RABBITMQ}
    for app in analysis.applications:
        if not app.has_framework(Framework.CELERY) or app.process("worker") is None:
            continue
        candidates = [s for s in analysis.services if s.kind in brokers]
        if candidates:
            continue
        questions.append(
            Question(
                id=f"broker:{app.name}",
                kind=QuestionKind.CHOICE,
                prompt=f"Qual broker o Celery da aplicação '{app.name}' utiliza?",
                target=f"broker:{app.name}",
                choices=["redis", "rabbitmq", "externo"],
                help="Nenhuma configuração de broker foi encontrada. "
                "'externo' não cria serviço (configure CELERY_BROKER_URL manualmente).",
            )
        )
    return questions


def apply_answer(analysis: ProjectAnalysis, question: Question, answer: str) -> None:
    answer = answer.strip()
    if not answer:
        raise AnswerError("Resposta vazia.")
    kind, _, rest = question.target.partition(":")
    if kind == "command":
        app_name, _, process_name = rest.partition(":")
        app = analysis.app(app_name)
        process = app.process(process_name) if app else None
        if process is None:
            raise AnswerError(f"Processo não encontrado: {rest}")
        try:
            argv = shlex.split(answer)
        except ValueError as exc:
            raise AnswerError(f"Comando inválido: {exc}") from exc
        process.command = Command(
            argv=argv, source="confirmado pelo usuário", confidence=Confidence.CONFIRMED
        )
    elif kind == "port":
        app_name, _, process_name = rest.partition(":")
        app = analysis.app(app_name)
        process = app.process(process_name) if app else None
        if process is None:
            raise AnswerError(f"Processo não encontrado: {rest}")
        if not answer.isdigit() or not 1 <= int(answer) <= 65535:
            raise AnswerError("Informe uma porta entre 1 e 65535.")
        process.port = int(answer)
        if process.dev_port is None:
            process.dev_port = int(answer)
    elif kind == "static_dir":
        app = analysis.app(rest)
        if app is None:
            raise AnswerError(f"Aplicação não encontrada: {rest}")
        if answer.startswith("/") or ".." in answer.split("/"):
            raise AnswerError("Informe um caminho relativo dentro da aplicação.")
        app.static_output_dir = answer.strip("/")
    elif kind == "service":
        service = analysis.service(ServiceKind(rest))
        if service is None:
            raise AnswerError(f"Serviço não encontrado: {rest}")
        if answer not in (YES, NO):
            raise AnswerError("Responda 'sim' ou 'nao'.")
        service.decision = ServiceDecision.LOCAL if answer == YES else ServiceDecision.DISCARDED
        service.reason = "Decisão confirmada pelo usuário."
    elif kind == "broker":
        if answer not in ("redis", "rabbitmq", "externo"):
            raise AnswerError("Escolha redis, rabbitmq ou externo.")
        if answer == "externo":
            return
        broker = ServiceKind(answer)
        existing = analysis.service(broker)
        if existing is None:
            analysis.services.append(
                InfraService(
                    kind=broker,
                    decision=ServiceDecision.LOCAL,
                    image=SPECS[broker].image,
                    used_by=[rest],
                    reason="Broker do Celery confirmado pelo usuário.",
                )
            )
        else:
            existing.decision = ServiceDecision.LOCAL
            if rest not in existing.used_by:
                existing.used_by.append(rest)
    else:
        raise AnswerError(f"Pergunta desconhecida: {question.target}")
