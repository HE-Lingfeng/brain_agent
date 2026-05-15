from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import json_dumps


DEFAULT_APPROVED_FORUM_LESSONS_DIR = Path(__file__).resolve().parent / "knowledge" / "approved_forum_lessons"
DEFAULT_PROMPT_LESSON_LIMIT = 6
DEFAULT_PROMPT_MAX_CHARS = 6000
MACHINE_READABLE_MARKER = "```json brain_agent_forum_learning_payload"


def approve_forum_lesson(
    report_path: Path,
    *,
    title: str | None = None,
    approve_all: bool = False,
    approved_by: str = "user",
    notes: str = "",
    knowledge_dir: Path | None = None,
) -> dict[str, Any]:
    """Import explicitly approved forum learning into the system knowledge base."""

    payload = load_forum_learning_report(report_path)
    learning = _learning_payload(payload)
    proposals = _proposed_updates(learning)
    approved = _select_proposals(proposals, title=title, approve_all=approve_all)
    if not approved:
        raise ValueError("No proposed_system_updates matched the approval request")

    query = str(payload.get("query") or learning.get("query") or "").strip()
    now = datetime.now(timezone.utc).isoformat()
    lesson_title = title or approved[0].get("title") or "approved forum lesson"
    lesson = {
        "title": lesson_title,
        "approved_at": now,
        "approved_by": approved_by,
        "source_report": str(report_path.expanduser().resolve()),
        "query": query,
        "notes": notes,
        "summary_cn": learning.get("summary_cn", ""),
        "experience_lessons": _string_list(learning.get("experience_lessons")),
        "alpha_research_patterns": _string_list(learning.get("alpha_research_patterns")),
        "pitfalls": _string_list(learning.get("pitfalls")),
        "approved_system_updates": [
            {**item, "approval_status": "approved", "approved_at": now, "approved_by": approved_by}
            for item in approved
            if isinstance(item, dict)
        ],
        "approval_required": False,
    }

    root = (knowledge_dir or DEFAULT_APPROVED_FORUM_LESSONS_DIR).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    slug = _slugify(lesson_title)
    lesson_path = _unique_path(root / f"{slug}.md")
    lesson_path.write_text(render_approved_forum_lesson_markdown(lesson), encoding="utf-8")

    index_path = root / "index.jsonl"
    record = {
        "title": lesson["title"],
        "approved_at": now,
        "approved_by": approved_by,
        "query": query,
        "path": str(lesson_path),
        "source_report": lesson["source_report"],
        "approved_update_count": len(lesson["approved_system_updates"]),
    }
    with index_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    return {"success": True, "lesson_path": str(lesson_path), "index_path": str(index_path), "lesson": lesson}


def load_forum_learning_report(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    text = resolved.read_text(encoding="utf-8")
    stripped = text.strip()
    if stripped.startswith("{"):
        data = json.loads(stripped)
        if not isinstance(data, dict):
            raise ValueError(f"Forum learning report must be a JSON object: {resolved}")
        return data

    fenced = _extract_fenced_json(text, "brain_agent_forum_learning_payload")
    if fenced is not None:
        return fenced

    return _parse_legacy_markdown_report(text)


def render_approved_forum_lesson_markdown(lesson: dict[str, Any]) -> str:
    lines = [
        f"# {lesson.get('title') or 'Approved Forum Lesson'}",
        "",
        "## Metadata",
        f"- approved_at: {lesson.get('approved_at', '')}",
        f"- approved_by: {lesson.get('approved_by', '')}",
        f"- query: {lesson.get('query', '')}",
        f"- source_report: {lesson.get('source_report', '')}",
        "",
        "## Summary",
        str(lesson.get("summary_cn") or ""),
        "",
        "## Experience Lessons",
        *_bullets(lesson.get("experience_lessons")),
        "",
        "## Alpha Research Patterns",
        *_bullets(lesson.get("alpha_research_patterns")),
        "",
        "## Pitfalls",
        *_bullets(lesson.get("pitfalls")),
        "",
        "## Approved System Updates",
    ]
    for item in lesson.get("approved_system_updates", []) or []:
        if not isinstance(item, dict):
            continue
        lines.extend(
            [
                f"- {item.get('title', 'Untitled')} [approved]",
                f"  target: {item.get('target', '')}",
                f"  why: {item.get('why', '')}",
                f"  change: {item.get('change', '')}",
                f"  risk: {item.get('risk', '')}",
            ]
        )
    if lesson.get("notes"):
        lines.extend(["", "## Approval Notes", str(lesson.get("notes"))])
    lines.extend(["", "## Machine Readable", "```json brain_agent_approved_forum_lesson", json_dumps(lesson), "```"])
    return "\n".join(lines).rstrip() + "\n"


def render_approved_lessons_prompt(
    knowledge_dir: Path | None = None,
    *,
    limit: int = DEFAULT_PROMPT_LESSON_LIMIT,
    max_chars: int = DEFAULT_PROMPT_MAX_CHARS,
    query: str = "",
) -> str:
    root = (knowledge_dir or DEFAULT_APPROVED_FORUM_LESSONS_DIR).expanduser().resolve()
    index_path = root / "index.jsonl"
    if not index_path.exists():
        return ""
    records = _read_lesson_index(index_path)
    records = _rank_lesson_records(records, query)[: max(limit * 3, limit)]
    snippets = []
    for record in records[:limit]:
        path = Path(str(record.get("path") or ""))
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        payload = _extract_fenced_json(text, "brain_agent_approved_forum_lesson")
        lesson = payload if isinstance(payload, dict) else {}
        if not lesson:
            continue
        snippets.append(
            {
                "title": lesson.get("title"),
                "query": lesson.get("query"),
                "summary_cn": _truncate(str(lesson.get("summary_cn") or ""), 500),
                "experience_lessons": [_truncate(item, 240) for item in _string_list(lesson.get("experience_lessons"))[:3]],
                "alpha_research_patterns": [
                    _truncate(item, 240) for item in _string_list(lesson.get("alpha_research_patterns"))[:3]
                ],
                "pitfalls": [_truncate(item, 220) for item in _string_list(lesson.get("pitfalls"))[:2]],
                "approved_system_updates": [
                    _compact_update(item) for item in (lesson.get("approved_system_updates", []) or [])[:2] if isinstance(item, dict)
                ],
            }
        )
    if not snippets:
        return ""
    payload = {
        "disclosure_policy": (
            "progressive: compact lesson summaries only; open the source lesson file if more detail is needed"
        ),
        "total_approved_lessons": len(records),
        "included_lessons": snippets,
    }
    text = (
        "## Approved BRAIN Forum Lessons\n"
        "Only the following user-approved forum lessons may be used as system knowledge. "
        "Treat them as soft guidance, not as permission to submit alphas automatically.\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return _budget_text(text, max_chars)


def _read_lesson_index(index_path: Path) -> list[dict[str, Any]]:
    records = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _rank_lesson_records(records: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]+", query or "") if len(term) > 1]
    if not terms:
        return list(reversed(records))

    def score(record: dict[str, Any]) -> tuple[int, str]:
        text = " ".join(str(record.get(k) or "") for k in ("title", "query", "source_report")).lower()
        return (sum(1 for term in terms if term in text), str(record.get("approved_at") or ""))

    return sorted(records, key=score, reverse=True)


def _compact_update(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": _truncate(str(item.get("title") or ""), 160),
        "target": _truncate(str(item.get("target") or ""), 120),
        "change": _truncate(str(item.get("change") or ""), 260),
        "risk": _truncate(str(item.get("risk") or ""), 180),
    }


def _truncate(value: str, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _budget_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    suffix = "\n\n[truncated: approved forum lessons exceeded prompt budget]\n"
    return text[: max(0, max_chars - len(suffix))].rstrip() + suffix


def _learning_payload(payload: dict[str, Any]) -> dict[str, Any]:
    learning = payload.get("llm_learning") if isinstance(payload.get("llm_learning"), dict) else payload
    if not isinstance(learning, dict):
        raise ValueError("Forum learning report has no llm_learning object")
    return learning


def _proposed_updates(learning: dict[str, Any]) -> list[dict[str, Any]]:
    updates = learning.get("proposed_system_updates")
    return [item for item in updates if isinstance(item, dict)] if isinstance(updates, list) else []


def _select_proposals(proposals: list[dict[str, Any]], *, title: str | None, approve_all: bool) -> list[dict[str, Any]]:
    if approve_all:
        return proposals
    if not title:
        raise ValueError("--title is required unless --approve-all is used")
    normalized = _norm_title(title)
    return [item for item in proposals if _norm_title(str(item.get("title") or "")) == normalized]


def _extract_fenced_json(text: str, info_suffix: str) -> dict[str, Any] | None:
    pattern = rf"```json\s+{re.escape(info_suffix)}\s*(.*?)```"
    match = re.search(pattern, text, flags=re.DOTALL)
    if not match:
        return None
    data = json.loads(match.group(1).strip())
    if not isinstance(data, dict):
        raise ValueError("Machine-readable JSON payload must be an object")
    return data


def _parse_legacy_markdown_report(text: str) -> dict[str, Any]:
    learning = {
        "summary_cn": _section_text(text, "Summary"),
        "experience_lessons": _section_bullets(text, "Experience Lessons"),
        "alpha_research_patterns": _section_bullets(text, "Alpha Research Patterns"),
        "pitfalls": _section_bullets(text, "Pitfalls"),
        "proposed_system_updates": _legacy_proposals(text),
        "approval_required": True,
    }
    query_match = re.search(r"^Query:\s*(.+)$", text, flags=re.MULTILINE)
    return {"success": True, "query": query_match.group(1).strip() if query_match else "", "llm_learning": learning}


def _legacy_proposals(text: str) -> list[dict[str, Any]]:
    section = _section_text(text, "Proposed System Updates")
    proposals: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in section.splitlines():
        if line.startswith("- "):
            if current:
                proposals.append(current)
            title = re.sub(r"\s*\[[^\]]+\]\s*$", "", line[2:].strip())
            current = {"title": title, "approval_status": "pending"}
        elif current and line.strip().startswith(("target:", "change:", "risk:", "why:")):
            key, value = line.strip().split(":", 1)
            current[key] = value.strip()
    if current:
        proposals.append(current)
    return proposals


def _section_text(text: str, heading: str) -> str:
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        return ""
    start = match.end()
    next_match = re.search(r"^##\s+", text[start:], flags=re.MULTILINE)
    end = start + next_match.start() if next_match else len(text)
    return text[start:end].strip()


def _section_bullets(text: str, heading: str) -> list[str]:
    section = _section_text(text, heading)
    return [line[2:].strip() for line in section.splitlines() if line.startswith("- ") and line[2:].strip()]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _bullets(value: Any) -> list[str]:
    items = _string_list(value)
    return [f"- {item}" for item in items] if items else ["- None"]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "_", value.strip().lower()).strip("_")
    return slug[:80] or "approved_forum_lesson"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(2, 1000):
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find unique path for {path}")


def _norm_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()
