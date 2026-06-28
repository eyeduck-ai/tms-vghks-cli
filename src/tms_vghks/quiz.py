from __future__ import annotations

import json
from dataclasses import dataclass, field
import re
from datetime import date
from pathlib import Path

from .parsers import normalize_text

QUESTION_BANK_LATEST_ALIAS = "latest"
QUESTION_BANK_GLOB = "question-bank-*.jsonl"
QUESTION_BANK_FILENAME_RE = re.compile(r"^question-bank-(\d{8})\.jsonl$")


@dataclass(slots=True)
class QuizQuestion:
    text: str
    options: list[str]
    name: str | None = None
    multiple: bool = False


@dataclass(slots=True)
class QuestionBankEntry:
    question: str
    answers: list[str]
    course: str | None = None
    item: str | None = None
    options: list[str] = field(default_factory=list)
    verified: bool = False
    score: float | None = None
    updated: str | None = None
    quiz_stage: str | None = None
    source_status: str | None = None
    trusted_for_auto: bool = True
    confidence: float | None = None


class QuestionBank:
    def __init__(self, entries: list[QuestionBankEntry] | None = None) -> None:
        self.entries = entries or []

    @classmethod
    def from_markdown(cls, markdown: str) -> "QuestionBank":
        entries: list[QuestionBankEntry] = []
        blocks = re.split(r"\n\s*\n", markdown or "")
        current: dict[str, str] = {}

        def flush() -> None:
            if not current.get("question"):
                return
            answers = _split_answers(current.get("answer") or current.get("answers") or "")
            entries.append(
                QuestionBankEntry(
                    question=current["question"],
                    answers=answers,
                    course=current.get("course"),
                    item=current.get("item"),
                    options=_split_answers(current.get("options") or ""),
                    verified=(current.get("verified", "").lower() == "true"),
                    score=_parse_float(current.get("score")),
                    updated=current.get("updated"),
                )
            )

        for block in blocks:
            lines = [line.strip("- *") for line in block.splitlines() if line.strip()]
            found = False
            for line in lines:
                match = re.match(r"(?P<key>[A-Za-z ]+|題目|答案|課程|項目|選項|已驗證|分數|更新)[:：]\s*(?P<value>.+)", line)
                if not match:
                    continue
                found = True
                key = _canonical_key(match.group("key"))
                if key == "question" and current.get("question"):
                    flush()
                    current.clear()
                current[key] = match.group("value").strip()
            if not found and current.get("question"):
                flush()
                current.clear()
        flush()
        return cls(entries)

    @classmethod
    def from_path(cls, path: str | Path) -> "QuestionBank":
        source = resolve_question_bank_path(path)
        if source is None:
            raise FileNotFoundError("no dated question-bank-*.jsonl file was found")
        if source.suffix.lower() == ".jsonl":
            return cls.from_historical_jsonl(source)
        with source.open("r", encoding="utf-8") as handle:
            return cls.from_markdown(handle.read())

    @classmethod
    def from_historical_jsonl(cls, path: str | Path) -> "QuestionBank":
        entries: list[QuestionBankEntry] = []
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                entry = entry_from_jsonl_row(row)
                if entry and entry.trusted_for_auto:
                    entries.append(entry)
        return cls(entries)

    def match(self, question: QuizQuestion, course: str | None = None, item: str | None = None) -> QuestionBankEntry | None:
        question_key = _norm(question.text)
        option_keys = {_norm(option) for option in question.options}
        candidates: list[tuple[QuestionBankEntry, bool, bool]] = []
        for entry in self.entries:
            if not entry.trusted_for_auto:
                continue
            if _norm(entry.question) != question_key:
                continue
            option_exact = False
            if entry.options:
                entry_options = {_norm(option) for option in entry.options}
                if option_keys and not entry_options.issubset(option_keys):
                    continue
                option_exact = bool(option_keys) and entry_options == option_keys
            if course and entry.course and not _context_matches(entry.course, course):
                continue
            item_matches = not (item and entry.item) or _context_matches(entry.item, item)
            if not item_matches and not (entry.verified and option_exact):
                continue
            candidates.append((entry, option_exact, item_matches))
        candidates.sort(
            key=lambda candidate: (
                candidate[0].source_status == "verified_correct",
                candidate[0].verified,
                candidate[1],
                candidate[2],
                candidate[0].source_status == "ai_suggested_trusted",
                candidate[0].score or 0.0,
                candidate[0].confidence or 0.0,
                candidate[0].updated or "",
            ),
            reverse=True,
        )
        return candidates[0][0] if candidates else None


def dated_question_bank_filename(day: date | None = None) -> str:
    return f"question-bank-{(day or date.today()).strftime('%Y%m%d')}.jsonl"


def find_latest_question_bank_path(root: str | Path = ".") -> Path | None:
    candidates: list[tuple[str, float, str, Path]] = []
    for path in Path(root).glob(QUESTION_BANK_GLOB):
        if not path.is_file():
            continue
        match = QUESTION_BANK_FILENAME_RE.match(path.name)
        if not match:
            continue
        candidates.append((match.group(1), path.stat().st_mtime, path.name, path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][3]


def resolve_question_bank_path(path: str | Path | None) -> Path | None:
    if path is None:
        return find_latest_question_bank_path()
    if str(path).strip().lower() == QUESTION_BANK_LATEST_ALIAS:
        return find_latest_question_bank_path()
    return Path(path)


def entry_from_jsonl_row(row: dict) -> QuestionBankEntry | None:
    question = row.get("question", {})
    answer = row.get("answer", {})
    text = question.get("text")
    answers = answer.get("answers") or answer.get("selected_answers") or []
    if not text or not answers:
        return None
    status = answer.get("status") or row.get("assessment", {}).get("answer_status")
    stage = row.get("quiz_stage") or _stage_from_activity(row.get("activity", {}).get("title"))
    trusted = answer.get("trusted_for_auto")
    if trusted is None:
        trusted = status in {"verified_correct", "ai_suggested_trusted"} or (
            stage == "pretest" and status in {"unverified_selected", "pretest_historical_selected"}
        )
    return QuestionBankEntry(
        question=str(text),
        answers=[str(answer) for answer in answers],
        course=row.get("course", {}).get("title"),
        item=row.get("activity", {}).get("title"),
        options=[str(option) for option in question.get("options") or []],
        verified=status in {"verified_correct", "ai_suggested_trusted"},
        score=_parse_float(answer.get("score") or row.get("assessment", {}).get("score")),
        updated=row.get("created_at") or row.get("exported_at"),
        quiz_stage=stage,
        source_status=status,
        trusted_for_auto=bool(trusted),
        confidence=_parse_float(answer.get("confidence") or row.get("assessment", {}).get("confidence")),
    )


def _stage_from_activity(activity_title: str | None) -> str:
    title = normalize_text(activity_title)
    if "問卷" in title:
        return "survey"
    if "課前" in title:
        return "pretest"
    if "課後" in title:
        return "posttest"
    return "unknown"


def _context_matches(expected: str, actual: str) -> bool:
    expected_norm = _norm(expected)
    actual_norm = _norm(actual)
    if not expected_norm or not actual_norm:
        return True
    if expected_norm in actual_norm or actual_norm in expected_norm:
        return True
    expected_core = _context_core(expected)
    actual_core = _context_core(actual)
    return bool(expected_core and actual_core and (expected_core in actual_core or actual_core in expected_core))


def _context_core(value: str) -> str:
    text = re.split(r"\s*\|\s*", value or "", maxsplit=1)[0]
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    return _norm(text)


def suggest_answer(question: QuizQuestion) -> list[str]:
    positive_tokens = ("以上皆是", "皆是", "正確", "是", "可以", "應")
    negative_tokens = ("以上皆非", "皆非")
    for token in positive_tokens:
        for option in question.options:
            if token in option:
                return [option]
    for option in question.options:
        if not any(token in option for token in negative_tokens):
            return [option]
    return question.options[:1]


def sanitized_question_bank_snippet(
    course_title: str,
    item_title: str,
    questions: list[QuizQuestion],
    selected_answers: dict[str, list[str]],
    score: str | None = None,
    passing_score: str | None = None,
) -> str:
    lines = [
        "### TMS passed quiz",
        f"Course: {course_title}",
        f"Item: {item_title}",
        f"Score: {score or ''}",
        f"PassingScore: {passing_score or ''}",
        "Verified: true",
        "Source: live-passed-quiz",
        f"Updated: {date.today().isoformat()}",
        "",
    ]
    for question in questions:
        key = question.name or question.text
        lines.extend(
            [
                f"Question: {question.text}",
                "Options: " + " | ".join(question.options),
                "Answer: " + " | ".join(selected_answers.get(key, [])),
                "",
            ]
        )
    return "\n".join(lines).strip()


def _split_answers(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*\|\s*|\s*,\s*", value or "") if part.strip()]


def _canonical_key(key: str) -> str:
    mapping = {
        "題目": "question",
        "答案": "answer",
        "課程": "course",
        "項目": "item",
        "選項": "options",
        "已驗證": "verified",
        "分數": "score",
        "更新": "updated",
    }
    key = mapping.get(key, key).lower().replace(" ", "")
    return {
        "question": "question",
        "answer": "answer",
        "answers": "answers",
        "course": "course",
        "item": "item",
        "options": "options",
        "verified": "verified",
        "score": "score",
        "updated": "updated",
    }.get(key, key)


def _norm(value: str | None) -> str:
    return re.sub(r"\W+", "", normalize_text(value).lower())


def _parse_float(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None
