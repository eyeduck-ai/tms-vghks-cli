from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .config import DEFAULT_GEMINI_MODEL, GeminiQuizConfig
from .quiz import QuestionBank, QuizQuestion, suggest_answer


GEMINI_GENERATE_CONTENT_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


@dataclass(slots=True)
class QuizAnswerResolution:
    answers: dict[str, list[str]] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def selected_answer_count(self) -> int:
        return sum(len(answers) for answers in self.answers.values())

    @property
    def source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for source in self.sources.values():
            counts[source] = counts.get(source, 0) + 1
        return counts


class GeminiQuizError(RuntimeError):
    pass


PostFunc = Callable[..., Any]


class GeminiQuizClient:
    def __init__(self, config: GeminiQuizConfig, post_func: PostFunc | None = None) -> None:
        self.config = config
        self._post = post_func or requests.post

    def answer_question_indexes(
        self,
        questions: list[QuizQuestion],
        course_title: str,
        item_title: str,
    ) -> dict[str, list[int]]:
        if not questions:
            return {}
        if not self.config.enabled:
            raise GeminiQuizError("gemini_api_key_missing")

        id_to_question = {_gemini_question_id(index): question for index, question in enumerate(questions)}
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": _gemini_prompt(
                                questions=questions,
                                course_title=course_title,
                                item_title=item_title,
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": _gemini_response_schema(),
            },
        }
        response = self._post(
            GEMINI_GENERATE_CONTENT_URL.format(model=self.config.model),
            headers={
                "Content-Type": "application/json",
                "X-goog-api-key": self.config.api_key,
            },
            json=payload,
            timeout=self.config.timeout_seconds,
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code >= 400:
            raise GeminiQuizError(f"gemini_http_{status_code}")
        try:
            body = response.json()
        except Exception as exc:
            raise GeminiQuizError("gemini_response_not_json") from exc

        text = _extract_candidate_text(body)
        parsed = _loads_json_object(text)
        rows = parsed.get("answers")
        if not isinstance(rows, list):
            raise GeminiQuizError("gemini_answers_missing")

        answers: dict[str, list[int]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            question_id = row.get("id")
            indexes = row.get("option_indexes")
            if question_id not in id_to_question or not isinstance(indexes, list):
                continue
            valid_indexes: list[int] = []
            for value in indexes:
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    valid_indexes.append(value)
            answers[str(question_id)] = valid_indexes
        return answers


def resolve_quiz_answers(
    questions: list[QuizQuestion],
    course_title: str,
    item_title: str,
    question_bank: QuestionBank | None,
    quiz_policy: str,
    gemini_config: GeminiQuizConfig | None = None,
    gemini_client: GeminiQuizClient | None = None,
) -> QuizAnswerResolution:
    resolution = QuizAnswerResolution()
    unresolved: list[QuizQuestion] = []

    for question in questions:
        key = question_key(question)
        entry = question_bank.match(question, course_title, item_title) if question_bank else None
        if entry and entry.answers:
            resolution.answers[key] = list(entry.answers)
            resolution.sources[key] = "question_bank"
        else:
            unresolved.append(question)

    if not unresolved:
        return resolution

    if quiz_policy != "auto":
        resolution.missing = [question_key(question) for question in unresolved]
        return resolution

    unresolved = _apply_gemini_answers(
        unresolved=unresolved,
        course_title=course_title,
        item_title=item_title,
        gemini_config=gemini_config or GeminiQuizConfig(),
        gemini_client=gemini_client,
        resolution=resolution,
    )

    for question in unresolved:
        key = question_key(question)
        answers = suggest_answer(question)
        if answers:
            resolution.answers[key] = answers
            resolution.sources[key] = "heuristic"
        else:
            resolution.missing.append(key)

    return resolution


def question_key(question: QuizQuestion) -> str:
    return question.name or question.text


def _apply_gemini_answers(
    unresolved: list[QuizQuestion],
    course_title: str,
    item_title: str,
    gemini_config: GeminiQuizConfig,
    gemini_client: GeminiQuizClient | None,
    resolution: QuizAnswerResolution,
) -> list[QuizQuestion]:
    if not gemini_config.enabled and gemini_client is None:
        resolution.notes.append("gemini_api_key_missing")
        return unresolved

    client = gemini_client or GeminiQuizClient(gemini_config)
    try:
        indexes_by_id = client.answer_question_indexes(unresolved, course_title, item_title)
    except Exception as exc:
        resolution.issues.append(f"gemini_failed:{exc}")
        return unresolved

    remaining: list[QuizQuestion] = []
    for index, question in enumerate(unresolved):
        key = question_key(question)
        indexes = indexes_by_id.get(_gemini_question_id(index), [])
        answers = _answers_from_indexes(question, indexes)
        if answers:
            resolution.answers[key] = answers
            resolution.sources[key] = "gemini"
        else:
            resolution.issues.append(f"gemini_invalid_answer:{key}")
            remaining.append(question)
    return remaining


def _answers_from_indexes(question: QuizQuestion, indexes: list[int]) -> list[str]:
    answers: list[str] = []
    seen: set[int] = set()
    for index in indexes:
        if index in seen or index < 0 or index >= len(question.options):
            continue
        answers.append(question.options[index])
        seen.add(index)
        if not question.multiple:
            break
    return answers


def _gemini_question_id(index: int) -> str:
    return f"q{index + 1}"


def _gemini_prompt(questions: list[QuizQuestion], course_title: str, item_title: str) -> str:
    rows = []
    for index, question in enumerate(questions):
        rows.append(
            {
                "id": _gemini_question_id(index),
                "text": question.text,
                "multiple": question.multiple,
                "options": [
                    {
                        "index": option_index,
                        "text": option,
                    }
                    for option_index, option in enumerate(question.options)
                ],
            }
        )
    payload = {
        "course": course_title,
        "item": item_title,
        "questions": rows,
    }
    return (
        "Answer these Traditional Chinese continuing-education quiz questions. "
        "Choose only option indexes from each question's provided options. "
        "If a question is multiple choice, return every correct option index; otherwise return one best option index. "
        "Return JSON matching the response schema.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _gemini_response_schema() -> dict[str, Any]:
    return {
        "type": "OBJECT",
        "properties": {
            "answers": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "id": {"type": "STRING"},
                        "option_indexes": {
                            "type": "ARRAY",
                            "items": {"type": "INTEGER"},
                        },
                    },
                    "required": ["id", "option_indexes"],
                },
            }
        },
        "required": ["answers"],
    }


def _extract_candidate_text(body: dict[str, Any]) -> str:
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise GeminiQuizError("gemini_candidates_missing")
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise GeminiQuizError("gemini_parts_missing")
    texts = [part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    text = "".join(texts).strip()
    if not text:
        raise GeminiQuizError("gemini_text_missing")
    return text


def _loads_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise GeminiQuizError("gemini_json_invalid")
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise GeminiQuizError("gemini_json_not_object")
    return value


__all__ = [
    "DEFAULT_GEMINI_MODEL",
    "GeminiQuizClient",
    "GeminiQuizConfig",
    "GeminiQuizError",
    "QuizAnswerResolution",
    "question_key",
    "resolve_quiz_answers",
]
