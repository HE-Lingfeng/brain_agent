from __future__ import annotations

import re
import json
import os
import base64
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .credentials import load_credentials


ALPHA_TERMS = {
    "alpha",
    "alphas",
    "brain",
    "simulation",
    "simulate",
    "submit",
    "submission",
    "sharpe",
    "fitness",
    "turnover",
    "decay",
    "delay",
    "truncation",
    "neutralization",
    "pasteurization",
    "nanhandling",
    "universe",
    "operator",
    "operators",
    "datafield",
    "datafields",
    "dataset",
    "datasets",
    "correlation",
    "self-correlation",
    "production-correlation",
}

DEFAULT_DAILY_LEARNING_QUERIES = [
    "high quality alpha turnover fitness",
    "simulation submission self correlation production correlation",
    "datafield coverage universe neutralization decay",
    "operator tips ts_rank group_neutralize trade_when",
]


class ForumService:
    """Thin brain_agent wrapper around the existing BRAIN forum client."""

    def __init__(self, *, secret_path: str | None = None) -> None:
        creds = load_credentials(secret_path=secret_path, require_brain=True, require_llm=False)
        self.email = creds["brain_email"]
        self.password = creds["brain_password"]

    async def glossary(self) -> dict[str, Any]:
        client = _forum_client()
        terms = await client.get_glossary_terms(self.email, self.password)
        return {
            "success": True,
            "terms": terms,
            "total_terms": len(terms),
            "analysis": analyze_glossary_terms(terms),
        }

    async def search(self, query: str, *, max_results: int = 50, locale: str = "zh-cn") -> dict[str, Any]:
        client = _forum_client()
        payload = await client.search_forum_posts(self.email, self.password, query, max_results, locale=locale)
        results = payload.get("results", []) if isinstance(payload, dict) else []
        payload["analysis"] = analyze_search_results(query, results)
        return payload

    async def read(self, post: str, *, include_comments: bool = True) -> dict[str, Any]:
        client = _forum_client()
        payload = await client.read_full_forum_post(self.email, self.password, post, include_comments)
        if isinstance(payload, dict):
            payload["analysis"] = analyze_forum_post(payload)
        return payload

    async def learn(
        self,
        query: str,
        *,
        max_results: int = 10,
        read_top: int = 3,
        locale: str = "zh-cn",
        include_comments: bool = True,
    ) -> dict[str, Any]:
        client = _forum_client()
        search_payload = await client.search_forum_posts(self.email, self.password, query, max_results, locale=locale)
        results = search_payload.get("results", []) if isinstance(search_payload, dict) else []
        ranked = sorted(results, key=_result_score, reverse=True)
        posts = []
        for item in ranked[: max(0, read_top)]:
            link = _canonical_forum_link(str(item.get("link") or ""))
            if not link:
                continue
            item = {**item, "link": link}
            try:
                post_payload = await client.read_full_forum_post(self.email, self.password, link, include_comments)
            except Exception as exc:
                posts.append({"source": item, "error": str(exc)})
                continue
            if isinstance(post_payload, dict):
                post_payload["analysis"] = analyze_forum_post(post_payload)
            posts.append({"source": item, "payload": post_payload})
        local_payload = {
            "success": True,
            "query": query,
            "search": search_payload,
            "read_posts": posts,
            "local_analysis": {
                "search": analyze_search_results(query, results),
                "combined_terms": _keywords(_joined_posts_text(posts), limit=25),
                "brain_terms": _brain_terms(_joined_posts_text(posts)),
            },
        }
        local_payload["llm_learning"] = summarize_forum_learning_with_llm(local_payload)
        local_payload["approval_required"] = True
        local_payload["approval_note"] = (
            "This report only proposes system optimizations. Do not apply any proposal until the user explicitly approves it."
        )
        return local_payload

    async def daily_learn(
        self,
        *,
        queries: list[str] | None = None,
        max_results_per_query: int = 12,
        read_top: int = 5,
        locale: str = "zh-cn",
        include_comments: bool = True,
        history_path: Path | None = None,
        reread_after_days: int = 14,
    ) -> dict[str, Any]:
        client = _forum_client()
        normalized_queries = _normalize_daily_queries(queries)
        history = _read_forum_learning_history(history_path)
        searches = []
        by_link: dict[str, dict[str, Any]] = {}
        for query in normalized_queries:
            search_payload = await client.search_forum_posts(
                self.email,
                self.password,
                query,
                max_results_per_query,
                locale=locale,
            )
            results = search_payload.get("results", []) if isinstance(search_payload, dict) else []
            searches.append(
                {
                    "query": query,
                    "result_count": len(results),
                    "analysis": analyze_search_results(query, results),
                }
            )
            for item in results:
                if not isinstance(item, dict):
                    continue
                link = _canonical_forum_link(str(item.get("link") or "").strip())
                if not link:
                    continue
                existing = by_link.get(link)
                if existing is None or _result_score(item) > _result_score(existing):
                    by_link[link] = {**item, "link": link, "matched_query": query}

        ranked_all = sorted(by_link.values(), key=_result_score, reverse=True)
        ranked, recent_skipped = _prioritize_unread_posts(
            ranked_all,
            history,
            read_top=max(0, read_top),
            reread_after_days=max(0, int(reread_after_days)),
        )
        posts = []
        for item in ranked[: max(0, read_top)]:
            link = _canonical_forum_link(str(item.get("link") or ""))
            item = {**item, "link": link}
            try:
                post_payload = await client.read_full_forum_post(self.email, self.password, link, include_comments)
            except Exception as exc:
                posts.append({"source": item, "error": str(exc)})
                continue
            if isinstance(post_payload, dict):
                post_payload["analysis"] = analyze_forum_post(post_payload)
            posts.append({"source": item, "payload": post_payload})

        text = _joined_posts_text(posts)
        _append_forum_learning_history(history_path, posts)
        local_payload = {
            "success": True,
            "mode": "daily_learn",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query": "daily high-quality BRAIN forum learning",
            "queries": normalized_queries,
            "searches": searches,
            "read_posts": posts,
            "local_analysis": {
                "searched_query_count": len(normalized_queries),
                "unique_result_count": len(by_link),
                "read_post_count": len(posts),
                "history_path": str(history_path.expanduser().resolve()) if history_path else "",
                "reread_after_days": max(0, int(reread_after_days)),
                "recently_read_skipped_count": len(recent_skipped),
                "recently_read_skipped": [
                    {
                        "title": item.get("title", ""),
                        "link": item.get("link", ""),
                        "last_read_at": history.get(str(item.get("link") or ""), {}).get("read_at", ""),
                    }
                    for item in recent_skipped[:10]
                ],
                "top_results": [
                    {
                        "title": item.get("title", ""),
                        "link": item.get("link", ""),
                        "score": _result_score(item),
                        "votes": _intish(item.get("votes")),
                        "comments": _intish(item.get("comments")),
                        "matched_query": item.get("matched_query", ""),
                    }
                    for item in ranked_all[:10]
                ],
                "combined_terms": _keywords(text, limit=25),
                "brain_terms": _brain_terms(text),
            },
        }
        local_payload["llm_learning"] = summarize_forum_learning_with_llm(local_payload)
        local_payload["approval_required"] = True
        local_payload["approval_note"] = (
            "Daily learning report only proposes optimizations. Apply nothing until the user approves a proposal."
        )
        return local_payload


def _forum_client() -> Any:
    try:
        from forum_functions import forum_client
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "forum_functions.py is required for forum access. Run this command from the AIWorker repo root."
        ) from exc
    return forum_client


def _normalize_daily_queries(queries: list[str] | None) -> list[str]:
    values = [str(query).strip() for query in (queries or DEFAULT_DAILY_LEARNING_QUERIES)]
    deduped = []
    seen = set()
    for value in values:
        key = value.lower()
        if value and key not in seen:
            deduped.append(value)
            seen.add(key)
    return deduped or list(DEFAULT_DAILY_LEARNING_QUERIES)


def _canonical_forum_link(link: str) -> str:
    text = str(link or "").strip()
    if "/search/click?" not in text:
        return text
    decoded = urllib.parse.unquote(text)
    decoded_blobs = [decoded]
    for blob in re.findall(r"[A-Za-z0-9+/=]{24,}", text):
        try:
            padded = blob + "=" * (-len(blob) % 4)
            decoded_blobs.append(base64.b64decode(padded).decode("utf-8", errors="ignore"))
        except (ValueError, OSError):
            continue
    joined = "\n".join(decoded_blobs)
    matches = re.findall(r"https://support\.worldquantbrain\.com/hc/[^\x00-\x20\"<>]+", joined)
    if not matches:
        return text
    url = next((item for item in matches if "/community/posts/" in item), matches[-1])
    return re.split(r"[\x00-\x1f]", url, maxsplit=1)[0].rstrip()


def _read_forum_learning_history(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for line in resolved.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        link = str(record.get("link") or "").strip()
        if link:
            records[link] = record
    return records


def _prioritize_unread_posts(
    ranked: list[dict[str, Any]],
    history: dict[str, dict[str, Any]],
    *,
    read_top: int,
    reread_after_days: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if read_top <= 0 or not history:
        return ranked, []
    cutoff = datetime.now(timezone.utc) - timedelta(days=reread_after_days)
    fresh = []
    recent = []
    for item in ranked:
        link = str(item.get("link") or "")
        record = history.get(link)
        if not record:
            fresh.append(item)
            continue
        read_at = _parse_history_time(record.get("read_at"))
        if read_at is None or read_at <= cutoff:
            fresh.append(item)
        else:
            recent.append(item)
    selected = fresh[:read_top]
    if len(selected) < read_top:
        selected.extend(recent[: read_top - len(selected)])
    selected_links = {str(item.get("link") or "") for item in selected}
    remaining = [item for item in ranked if str(item.get("link") or "") not in selected_links]
    return selected + remaining, recent


def _append_forum_learning_history(path: Path | None, posts: list[dict[str, Any]]) -> None:
    if path is None or not posts:
        return
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with resolved.open("a", encoding="utf-8") as f:
        for item in posts:
            if not isinstance(item, dict):
                continue
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            post = payload.get("post") if isinstance(payload.get("post"), dict) else {}
            comments = payload.get("comments") if isinstance(payload.get("comments"), list) else []
            link = str(source.get("link") or "").strip()
            if not link:
                continue
            body = str(post.get("body") or "")
            comment_text = "\n".join(str(c.get("body") or "") for c in comments if isinstance(c, dict))
            record = {
                "read_at": now,
                "link": link,
                "title": source.get("title") or post.get("title") or "",
                "votes": _intish(source.get("votes")),
                "comments": len(comments),
                "content_sha256": hashlib.sha256(f"{body}\n{comment_text}".encode("utf-8")).hexdigest(),
            }
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _parse_history_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def analyze_search_results(query: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(results, key=_result_score, reverse=True)
    top_results = []
    for item in ranked[:5]:
        top_results.append(
            {
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "score": _result_score(item),
                "votes": _intish(item.get("votes")),
                "comments": _intish(item.get("comments")),
                "date": item.get("date", ""),
                "snippet": item.get("snippet", ""),
            }
        )
    return {
        "query": query,
        "result_count": len(results),
        "top_results": top_results,
        "common_terms": _keywords(" ".join(_result_text(item) for item in results), limit=15),
    }


def analyze_forum_post(payload: dict[str, Any]) -> dict[str, Any]:
    post = payload.get("post") if isinstance(payload.get("post"), dict) else {}
    comments = payload.get("comments") if isinstance(payload.get("comments"), list) else []
    body = str(post.get("body") or "")
    comment_texts = [str(item.get("body") or "") for item in comments if isinstance(item, dict)]
    full_text = "\n".join([body, *comment_texts])
    return {
        "title": post.get("title", ""),
        "author": post.get("author", ""),
        "comment_count": len(comments),
        "key_terms": _keywords(full_text, limit=20),
        "brain_terms": _brain_terms(full_text),
        "operator_mentions": _function_mentions(full_text),
        "datafield_like_mentions": _datafield_mentions(full_text),
        "main_points": _important_sentences(body, limit=5),
        "comment_points": _important_sentences("\n".join(comment_texts), limit=5),
        "actionable_takeaways": _takeaways(full_text),
    }


def analyze_glossary_terms(terms: list[dict[str, Any]]) -> dict[str, Any]:
    joined = " ".join(f"{item.get('term', '')} {item.get('definition', '')}" for item in terms)
    return {
        "term_count": len(terms),
        "common_terms": _keywords(joined, limit=20),
        "brain_terms": _brain_terms(joined),
    }


def summarize_forum_learning_with_llm(payload: dict[str, Any]) -> dict[str, Any]:
    creds = load_credentials(require_brain=False, require_llm=True)
    llm_payload = _build_learning_llm_payload(payload, creds)
    try:
        data = _post_chat_completion(creds, llm_payload)
        content = data["choices"][0]["message"]["content"]
        parsed = _parse_json_object(content)
    except (
        KeyError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
        requests.exceptions.RequestException,
        TimeoutError,
        OSError,
    ) as exc:
        raise RuntimeError(f"LLM forum learning failed: {exc}") from exc
    parsed.setdefault("approval_required", True)
    parsed.setdefault("proposed_system_updates", [])
    return parsed


def _post_chat_completion(creds: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    url = creds["llm_base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {creds['llm_api_key']}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        if response.status_code >= 300:
            raise RuntimeError(f"LLM HTTP {response.status_code}: {response.text[:1000]}")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("LLM response must be a JSON object")
        return data
    except requests.exceptions.SSLError:
        req = urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("LLM response must be a JSON object")
        return data


def _build_learning_llm_payload(payload: dict[str, Any], creds: dict[str, str]) -> dict[str, Any]:
    compact_posts = []
    for item in payload.get("read_posts", [])[:8]:
        if not isinstance(item, dict):
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        post_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        post = post_payload.get("post") if isinstance(post_payload.get("post"), dict) else {}
        comments = post_payload.get("comments") if isinstance(post_payload.get("comments"), list) else []
        compact_posts.append(
            {
                "source_title": source.get("title", ""),
                "source_link": source.get("link", ""),
                "votes": source.get("votes", 0),
                "comment_count": len(comments),
                "post_title": post.get("title", ""),
                "post_body": _truncate(str(post.get("body") or ""), 5000),
                "top_comments": [_truncate(str(c.get("body") or ""), 1200) for c in comments[:12] if isinstance(c, dict)],
                "local_analysis": post_payload.get("analysis", {}),
                "read_error": item.get("error"),
            }
        )
    system = (
        "You are a senior WorldQuant BRAIN research engineer. Learn from forum posts and summarize practical experience. "
        "Return ONLY one JSON object in Chinese. Do not claim a system change was applied. "
        "Any optimization must be a proposal requiring explicit user approval."
    )
    user = {
        "task": "从论坛帖子中总结别人的 BRAIN alpha 挖掘经验，并提出可审批的系统优化建议。",
        "query": payload.get("query"),
        "local_analysis": payload.get("local_analysis"),
        "posts": compact_posts,
        "required_json_schema": {
            "summary_cn": "整体中文总结",
            "experience_lessons": ["可复用经验，带依据"],
            "alpha_research_patterns": ["可迁移到 alpha 挖掘的模式"],
            "pitfalls": ["常见坑和失败原因"],
            "proposed_system_updates": [
                {
                    "title": "建议标题",
                    "target": "可能修改的模块或流程",
                    "why": "来自帖子经验的理由",
                    "change": "具体优化方案",
                    "risk": "潜在风险",
                    "approval_status": "pending",
                }
            ],
            "approval_required": True,
            "questions_for_user": ["需要用户确认的问题"],
        },
    }
    return {
        "model": os.environ.get("LLM_MODEL") or os.environ.get("MOONSHOT_MODEL") or creds.get("llm_model", "kimi-k2.5"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
        "temperature": 1,
    }


def render_markdown(payload: dict[str, Any], *, title: str = "BRAIN Forum Result") -> str:
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    lines = [f"# {title}", ""]
    if "llm_learning" in payload:
        learning = payload.get("llm_learning") if isinstance(payload.get("llm_learning"), dict) else {}
        lines.extend(
            [
                f"Query: {payload.get('query', '')}",
                "",
                "## Summary",
                str(learning.get("summary_cn") or "No summary."),
                "",
                "## Experience Lessons",
                *_bullet_lines(learning.get("experience_lessons", [])),
                "",
                "## Alpha Research Patterns",
                *_bullet_lines(learning.get("alpha_research_patterns", [])),
                "",
                "## Pitfalls",
                *_bullet_lines(learning.get("pitfalls", [])),
                "",
                "## Proposed System Updates",
            ]
        )
        for item in learning.get("proposed_system_updates", []) or []:
            if not isinstance(item, dict):
                continue
            lines.extend(
                [
                    f"- {item.get('title', 'Untitled')} [{item.get('approval_status', 'pending')}]",
                    f"  target: {item.get('target', '')}",
                    f"  change: {item.get('change', '')}",
                    f"  risk: {item.get('risk', '')}",
                ]
            )
        lines.extend(
            [
                "",
                "## Approval Gate",
                "- No system optimization has been applied.",
                "- Reply with the proposal title or exact changes you approve before implementation.",
                "",
                "## Machine Readable",
                "```json brain_agent_forum_learning_payload",
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
        )
    elif "post" in payload:
        post = payload.get("post") or {}
        lines.extend(
            [
                f"## {post.get('title', 'Untitled')}",
                "",
                f"- Author: {post.get('author', 'Unknown')}",
                f"- Comments: {analysis.get('comment_count', payload.get('total_comments', 0))}",
                "",
                "## Main Points",
                *_bullet_lines(analysis.get("main_points", [])),
                "",
                "## Comment Points",
                *_bullet_lines(analysis.get("comment_points", [])),
                "",
                "## Actionable Takeaways",
                *_bullet_lines(analysis.get("actionable_takeaways", [])),
                "",
                "## Terms",
                f"- BRAIN terms: {', '.join(analysis.get('brain_terms', [])) or 'None'}",
                f"- Operators: {', '.join(analysis.get('operator_mentions', [])) or 'None'}",
                f"- Datafields: {', '.join(analysis.get('datafield_like_mentions', [])) or 'None'}",
            ]
        )
    elif "results" in payload:
        lines.extend(["## Top Results"])
        for item in analysis.get("top_results", []):
            lines.append(f"- [{item.get('title', 'Untitled')}]({item.get('link', '')})")
            meta = f"  votes={item.get('votes', 0)}, comments={item.get('comments', 0)}, score={item.get('score', 0)}"
            lines.append(meta)
        lines.extend(["", "## Common Terms", ", ".join(analysis.get("common_terms", [])) or "None"])
    elif "terms" in payload:
        lines.extend(["## Glossary", f"Terms: {analysis.get('term_count', payload.get('total_terms', 0))}", ""])
        for item in payload.get("terms", [])[:50]:
            lines.append(f"- {item.get('term', '')}: {item.get('definition', '')}")
    else:
        lines.append("No recognized forum payload.")
    return "\n".join(lines).rstrip() + "\n"


def _result_text(item: dict[str, Any]) -> str:
    return " ".join(str(item.get(key) or "") for key in ("title", "snippet", "author", "date"))


def _result_score(item: dict[str, Any]) -> int:
    return _intish(item.get("votes")) * 3 + _intish(item.get("comments"))


def _intish(value: Any) -> int:
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else 0


def _keywords(text: str, *, limit: int) -> list[str]:
    tokens = [token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text)]
    stop = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "you",
        "your",
        "are",
        "was",
        "will",
        "have",
        "has",
        "not",
        "can",
        "but",
        "about",
        "https",
        "support",
        "worldquantbrain",
    }
    counts = Counter(token for token in tokens if token not in stop)
    return [word for word, _count in counts.most_common(limit)]


def _brain_terms(text: str) -> list[str]:
    lower = text.lower()
    return sorted(term for term in ALPHA_TERMS if term in lower)


def _function_mentions(text: str) -> list[str]:
    names = set(re.findall(r"\b([a-z][a-z0-9_]{2,})\s*\(", text))
    return sorted(name for name in names if name not in {"http", "https"})[:30]


def _datafield_mentions(text: str) -> list[str]:
    names = set(re.findall(r"\b(?:[a-z]{2,}\d+_[a-z0-9_]+|[a-z]{2,}\d+[a-z0-9_]*)\b", text.lower()))
    return sorted(names)[:30]


def _important_sentences(text: str, *, limit: int) -> list[str]:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+|\n+", text) if len(s.strip()) >= 20]
    scored = sorted(sentences, key=_sentence_score, reverse=True)
    return [sentence[:500] for sentence in scored[:limit]]


def _sentence_score(sentence: str) -> int:
    lower = sentence.lower()
    score = sum(3 for term in ALPHA_TERMS if term in lower)
    score += len(re.findall(r"\b[a-z][a-z0-9_]{2,}\s*\(", lower))
    score += min(len(sentence) // 120, 3)
    return score


def _takeaways(text: str) -> list[str]:
    points = []
    for sentence in _important_sentences(text, limit=20):
        lower = sentence.lower()
        if any(word in lower for word in ("should", "try", "use", "avoid", "need", "建议", "可以", "不要", "使用", "避免")):
            points.append(sentence)
        if len(points) >= 6:
            break
    return points


def _joined_posts_text(posts: list[dict[str, Any]]) -> str:
    chunks = []
    for item in posts:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        post = payload.get("post") if isinstance(payload.get("post"), dict) else {}
        chunks.append(str(post.get("body") or ""))
        comments = payload.get("comments") if isinstance(payload.get("comments"), list) else []
        chunks.extend(str(c.get("body") or "") for c in comments if isinstance(c, dict))
    return "\n".join(chunks)


def _truncate(text: str, max_chars: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 20)].rstrip() + " ...[truncated]"


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")
    return parsed


def _bullet_lines(values: Any) -> list[str]:
    if not values:
        return ["- None"]
    return [f"- {value}" for value in values]
