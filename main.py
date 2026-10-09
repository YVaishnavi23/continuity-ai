"""Continuity: a small, local-first project-memory application.

Run with: uvicorn main:app --reload
The LLM API is optional. Without OPENAI_API_KEY, a simple rule-based extractor
is used so the end-to-end workflow can still be tested locally.
"""

import base64
import io
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import AuditEvent, Memory, Project, ProcessingRun, Source, get_db, init_db, utcnow


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
load_dotenv(BASE_DIR / ".env")
init_db()

app = FastAPI(
    title="Continuity API",
    description="Persistent, reviewable memory for interrupted AI-assisted work.",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

MEMORY_TYPES = {"goal", "decision", "progress", "next_step", "constraint", "term", "question", "other"}
MEMORY_STATUSES = {"candidate", "approved", "rejected", "superseded"}
SOURCE_CHECK_STATES = {"not_checked", "supports", "does_not_support", "not_found"}
UNDOABLE_ACTIONS = {
    "candidate_edited",
    "memory_approved",
    "candidate_rejected",
    "source_verification_changed",
    "conflict_resolved_keep_existing",
    "conflict_resolved_accept_new",
    "conflict_left_unresolved",
    "candidate_reopened",
}


# ------------------------------- Request models -------------------------------

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    goal: str = Field(default="", max_length=5000)


class ProjectEdit(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    goal: str | None = Field(default=None, max_length=5000)


class SourceCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=100_000)
    source_type: Literal["conversation", "note", "text"] = "conversation"


class MemoryEdit(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    memory_type: str | None = None
    rationale: str | None = Field(default=None, max_length=4000)


class SourceVerification(BaseModel):
    status: Literal["supports", "does_not_support", "not_checked"]
    note: str = Field(default="", max_length=1000)


class RejectionRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


class ConflictResolution(BaseModel):
    choice: Literal["keep_existing", "accept_new", "leave_unresolved"]
    reason: str = Field(default="", max_length=1000)


class ResumeRequest(BaseModel):
    include_goal: bool = True
    include_decisions: bool = True
    include_progress: bool = True
    include_next_steps: bool = True
    include_constraints: bool = True
    include_lexicon: bool = True
    include_history_notes: bool = False


# --------------------------------- Helpers ------------------------------------

def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _project_dict(project: Project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "goal": project.goal,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
    }


def _source_dict(source: Source, include_content: bool = False) -> dict:
    result = {
        "id": source.id,
        "project_id": source.project_id,
        "title": source.title,
        "source_type": source.source_type,
        "created_at": _iso(source.created_at),
        "character_count": len(source.content),
    }
    if include_content:
        result["content"] = source.content
    return result


def _memory_dict(db: Session, memory: Memory) -> dict:
    source_title = None
    source_quote_found = None
    if memory.source_id:
        source = db.get(Source, memory.source_id)
        source_title = source.title if source else None
        source_quote_found = _quote_found(memory.source_quote, source.content) if source and memory.source_quote else False
    conflict_content = None
    if memory.conflict_with_id:
        conflict = db.get(Memory, memory.conflict_with_id)
        conflict_content = conflict.content if conflict else None
    return {
        "id": memory.id,
        "project_id": memory.project_id,
        "source_id": memory.source_id,
        "source_title": source_title,
        "source_quote_found": source_quote_found,
        "memory_type": memory.memory_type,
        "content": memory.content,
        "rationale": memory.rationale,
        "source_quote": memory.source_quote,
        "source_check_status": memory.source_check_status,
        "status": memory.status,
        "conflict_with_id": memory.conflict_with_id,
        "conflict_content": conflict_content,
        "conflict_status": memory.conflict_status,
        "supersedes_id": memory.supersedes_id,
        "created_at": _iso(memory.created_at),
        "updated_at": _iso(memory.updated_at),
        "approved_at": _iso(memory.approved_at),
    }


def _memory_snapshot(memory: Memory) -> dict:
    return {
        "id": memory.id,
        "project_id": memory.project_id,
        "source_id": memory.source_id,
        "memory_type": memory.memory_type,
        "content": memory.content,
        "rationale": memory.rationale,
        "source_quote": memory.source_quote,
        "source_check_status": memory.source_check_status,
        "status": memory.status,
        "conflict_with_id": memory.conflict_with_id,
        "conflict_status": memory.conflict_status,
        "supersedes_id": memory.supersedes_id,
        "created_at": _iso(memory.created_at),
        "updated_at": _iso(memory.updated_at),
        "approved_at": _iso(memory.approved_at),
    }


def _restore_snapshot(db: Session, snapshot: dict) -> None:
    memory_id = snapshot.get("id")
    if not memory_id:
        return
    memory = db.get(Memory, int(memory_id))
    if not memory:
        return
    datetime_fields = {"created_at", "updated_at", "approved_at"}
    for key, value in snapshot.items():
        if key in {"id", "project_id"}:
            continue
        if key in datetime_fields and isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                value = None
        if hasattr(memory, key):
            setattr(memory, key, value)


def _state_json(memories: list[Memory]) -> dict:
    return {str(memory.id): _memory_snapshot(memory) for memory in memories}


def _log_event(
    db: Session,
    project_id: int,
    action: str,
    before_states: dict | None = None,
    after_states: dict | None = None,
    reason: str = "",
    memory_ids: list[int | None] | None = None,
    actor_type: str = "user",
    undoes_operation_id: str | None = None,
) -> list[AuditEvent]:
    """Write one audit row per affected memory, sharing an operation ID.

    Shared snapshots allow one Undo operation to restore a multi-memory change
    (for example, superseding an earlier decision and approving its replacement).
    """
    before_states = before_states or {}
    after_states = after_states or {}
    if memory_ids is None:
        all_ids = {int(key) for key in before_states.keys()} | {int(key) for key in after_states.keys()}
        memory_ids = sorted(all_ids) if all_ids else [None]
    if not memory_ids:
        memory_ids = [None]
    operation_id = str(uuid.uuid4())
    timestamp = utcnow()
    before_json = json.dumps({"memories": before_states}, ensure_ascii=False)
    after_json = json.dumps({"memories": after_states}, ensure_ascii=False)
    rows = []
    for memory_id in dict.fromkeys(memory_ids):
        event = AuditEvent(
            operation_id=operation_id,
            project_id=project_id,
            memory_id=memory_id,
            action=action,
            actor_type=actor_type,
            before_json=before_json,
            after_json=after_json,
            reason=reason,
            undoes_operation_id=undoes_operation_id,
            created_at=timestamp,
        )
        db.add(event)
        rows.append(event)
    db.flush()
    return rows


def _event_dict(event: AuditEvent, memory_ids: list[int] | None = None, undone: bool = False) -> dict:
    try:
        before = json.loads(event.before_json or "{}")
    except json.JSONDecodeError:
        before = {}
    try:
        after = json.loads(event.after_json or "{}")
    except json.JSONDecodeError:
        after = {}
    return {
        "id": event.id,
        "operation_id": event.operation_id,
        "project_id": event.project_id,
        "memory_id": event.memory_id,
        "memory_ids": memory_ids or ([event.memory_id] if event.memory_id else []),
        "action": event.action,
        "actor_type": event.actor_type,
        "before": before,
        "after": after,
        "reason": event.reason,
        "undone": undone,
        "created_at": _iso(event.created_at),
    }


def _log_object_event(
    db: Session,
    project_id: int,
    action: str,
    object_key: str,
    before_value: dict | None,
    after_value: dict | None,
    reason: str = "",
) -> None:
    """Record a project/source change without mutating earlier audit rows."""
    db.add(AuditEvent(
        operation_id=str(uuid.uuid4()),
        project_id=project_id,
        memory_id=None,
        action=action,
        actor_type="user",
        before_json=json.dumps({object_key: before_value}, ensure_ascii=False),
        after_json=json.dumps({object_key: after_value}, ensure_ascii=False),
        reason=reason,
        created_at=utcnow(),
    ))
    db.flush()


def _ensure_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


def _ensure_memory(db: Session, memory_id: int) -> Memory:
    memory = db.get(Memory, memory_id)
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found.")
    return memory


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _quote_found(quote: str, original: str) -> bool:
    quote_norm = _normalise(quote)
    original_norm = _normalise(original)
    return bool(quote_norm and quote_norm in original_norm)


def _decision_conflict(db: Session, project_id: int, content: str) -> Memory | None:
    """A deliberately cautious, lexical check for possibly related decisions.

    It only flags likely topic overlap. It does not claim to understand semantic
    contradiction; the user resolves every flagged conflict manually.
    """
    stopwords = {
        "about", "after", "again", "also", "and", "are", "before", "build", "building",
        "can", "could", "first", "for", "from", "into", "its", "keep", "need", "our",
        "project", "should", "that", "the", "their", "then", "this", "through", "will",
        "with", "without", "would", "version", "work", "use", "using", "start", "make",
    }
    token_pattern = re.compile(r"[a-z0-9]{3,}")
    candidate_tokens = {t for t in token_pattern.findall(content.lower()) if t not in stopwords}
    if not candidate_tokens:
        return None
    approved = db.scalars(
        select(Memory).where(
            Memory.project_id == project_id,
            Memory.memory_type == "decision",
            Memory.status == "approved",
        ).order_by(Memory.updated_at.desc())
    ).all()
    for old in approved:
        existing_tokens = {t for t in token_pattern.findall(old.content.lower()) if t not in stopwords}
        shared = candidate_tokens & existing_tokens
        union = candidate_tokens | existing_tokens
        similarity = len(shared) / max(len(union), 1)
        key_overlap = shared & {"integration", "integrations", "automatic", "manual", "database", "provider", "frontend", "backend", "authentication", "budget", "model", "mvp", "release", "api"}
        if similarity >= 0.22 or len(key_overlap) >= 2:
            return old
    return None


def _simple_extract(text: str) -> list[dict]:
    """Rule-based fallback. Useful locally, but not equivalent to an LLM."""
    chunks = [part.strip(" \t-*•\r") for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip(" \t-*•\r")]
    patterns = [
        ("goal", re.compile(r"\b(goal|aim|purpose|building|build|create|develop|we want to|our idea)\b", re.I)),
        ("decision", re.compile(r"\b(decided|decision|we will use|we'll use|choose|chosen|will use|should focus|instead of|not use|avoid)\b", re.I)),
        ("constraint", re.compile(r"\b(must|only|constraint|budget|deadline|cannot|can't|do not|don't|without|limited to)\b", re.I)),
        ("next_step", re.compile(r"\b(next step|next:|todo|to do|pending|implement|need to add|should add|will implement)\b", re.I)),
        ("progress", re.compile(r"\b(completed|implemented|tested|working|in progress|finished|built|currently|already)\b", re.I)),
        ("question", re.compile(r"\b(unresolved|open question|not sure|uncertain|need to decide|unknown)\b", re.I)),
    ]
    output = []
    seen = set()
    for chunk in chunks:
        text_chunk = chunk[:1200]
        kind = None
        term_match = re.match(r"(?:term|lexicon)\s*:\s*([^—:-]{2,80})\s*[-—:]\s*(.+)", text_chunk, re.I)
        if term_match:
            kind = "term"
        else:
            for candidate_type, pattern in patterns:
                if pattern.search(text_chunk):
                    kind = candidate_type
                    break
        if not kind:
            continue
        key = _normalise(text_chunk)
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "type": kind,
            "content": text_chunk,
            "rationale": "Fallback extraction from source text. Review before approving.",
            "source_quote": text_chunk,
        })
        if len(output) >= 12:
            break
    if not output and text.strip():
        first = next((p.strip() for p in chunks if len(p.strip()) >= 12), text.strip()[:800])
        output.append({
            "type": "progress",
            "content": first[:1200],
            "rationale": "No obvious category was detected, so this sentence is offered as a draft memory. Review it carefully.",
            "source_quote": first,
        })
    return output


def _llm_extract(text: str) -> list[dict]:
    """Extract structured memories through OpenAI when configured."""
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    prompt = f"""Extract up to 12 candidate project memories from the source below.
Return JSON only in this shape: {{"memories":[{{"type":"goal|decision|progress|next_step|constraint|term|question|other","content":"concise standalone statement","rationale":"why this may matter","source_quote":"an exact short quote copied from the source"}}]}}.
Rules:
- Extract only information actually present; do not invent facts, decisions, dates or progress.
- Distinguish a suggestion from an adopted decision. If unclear, use question or other and state uncertainty in rationale.
- Preserve project-specific terminology.
- Keep each candidate useful outside the original conversation.
- source_quote must be an exact excerpt; never fabricate a citation.

SOURCE TEXT:
{text[:18000]}
"""
    response = client.chat.completions.create(
        model=model,
        temperature=0.1,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You extract cautious, source-grounded project memory candidates. You do not approve memories."},
            {"role": "user", "content": prompt},
        ],
    )
    payload = json.loads(response.choices[0].message.content or "{}")
    raw_memories = payload.get("memories", [])
    if not isinstance(raw_memories, list):
        raise ValueError("The model response did not contain a memories list.")
    result = []
    for item in raw_memories[:12]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", "other")).strip().lower()
        if kind not in MEMORY_TYPES:
            kind = "other"
        content = str(item.get("content", "")).strip()[:4000]
        if not content:
            continue
        result.append({
            "type": kind,
            "content": content,
            "rationale": str(item.get("rationale", "")).strip()[:4000],
            "source_quote": str(item.get("source_quote", "")).strip()[:2000],
        })
    return result


def _ensure_source_in_project(db: Session, source_id: int) -> Source:
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found.")
    return source


def _new_memory(db: Session, project_id: int, source: Source, item: dict) -> Memory:
    kind = item["type"] if item["type"] in MEMORY_TYPES else "other"
    quote = item.get("source_quote", "")
    # Finding an exact quote is not the same as verifying that it supports the claim.
    # A human review is still required to mark the source as supporting the candidate.
    check_status = "not_checked" if (not quote or _quote_found(quote, source.content)) else "not_found"
    memory = Memory(
        project_id=project_id,
        source_id=source.id,
        memory_type=kind,
        content=item["content"],
        rationale=item.get("rationale", ""),
        source_quote=quote,
        source_check_status=check_status,
        status="candidate",
        conflict_status="none",
    )
    if kind == "decision":
        previous = _decision_conflict(db, project_id, item["content"])
        if previous:
            memory.conflict_with_id = previous.id
            memory.conflict_status = "unresolved"
    db.add(memory)
    db.flush()
    return memory


# -------------------------------- UI routes -----------------------------------

@app.get("/", include_in_schema=False)
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "llm_configured": bool(os.getenv("OPENAI_API_KEY"))}


# ------------------------------- Project API ----------------------------------

@app.get("/api/projects")
def list_projects(db: Session = Depends(get_db)):
    projects = db.scalars(select(Project).order_by(Project.updated_at.desc(), Project.id.desc())).all()
    return [_project_dict(project) for project in projects]


@app.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="Project name cannot be blank.")
    project = Project(name=payload.name.strip(), goal=payload.goal.strip())
    db.add(project)
    db.flush()
    _log_object_event(db, project.id, "project_created", "project", None, _project_dict(project), "Project created.")
    db.commit()
    db.refresh(project)
    return _project_dict(project)


@app.get("/api/projects/{project_id}")
def get_project(project_id: int, db: Session = Depends(get_db)):
    project = _ensure_project(db, project_id)
    memories = db.scalars(select(Memory).where(Memory.project_id == project_id)).all()
    sources = db.scalars(select(Source).where(Source.project_id == project_id)).all()
    return {
        **_project_dict(project),
        "counts": {
            "approved": sum(m.status == "approved" for m in memories),
            "candidates": sum(m.status == "candidate" for m in memories),
            "sources": len(sources),
            "unresolved_conflicts": sum(m.status == "candidate" and m.conflict_status == "unresolved" for m in memories),
        },
    }


@app.patch("/api/projects/{project_id}")
def edit_project(project_id: int, payload: ProjectEdit, db: Session = Depends(get_db)):
    project = _ensure_project(db, project_id)
    before = _project_dict(project)
    if payload.name is not None:
        if not payload.name.strip():
            raise HTTPException(status_code=422, detail="Project name cannot be blank.")
        project.name = payload.name.strip()
    if payload.goal is not None:
        project.goal = payload.goal.strip()
    project.updated_at = utcnow()
    _log_object_event(db, project.id, "project_updated", "project", before, _project_dict(project), "Project details updated.")
    db.commit()
    db.refresh(project)
    return _project_dict(project)


# -------------------------------- Source API ----------------------------------

@app.get("/api/projects/{project_id}/sources")
def list_sources(project_id: int, db: Session = Depends(get_db)):
    _ensure_project(db, project_id)
    sources = db.scalars(select(Source).where(Source.project_id == project_id).order_by(Source.created_at.desc())).all()
    return [_source_dict(source) for source in sources]


@app.post("/api/projects/{project_id}/sources", status_code=201)
def create_source(project_id: int, payload: SourceCreate, db: Session = Depends(get_db)):
    _ensure_project(db, project_id)
    if not payload.title.strip() or not payload.content.strip():
        raise HTTPException(status_code=422, detail="Source title and content cannot be blank.")
    source = Source(
        project_id=project_id,
        title=payload.title.strip(),
        content=payload.content.strip(),
        source_type=payload.source_type,
    )
    db.add(source)
    db.flush()
    _log_object_event(db, project_id, "source_captured", "source", None, _source_dict(source), f"Captured source: {source.title}")
    db.commit()
    db.refresh(source)
    return _source_dict(source)


@app.get("/api/sources/{source_id}")
def get_source(source_id: int, db: Session = Depends(get_db)):
    source = _ensure_source_in_project(db, source_id)
    return _source_dict(source, include_content=True)


@app.post("/api/sources/{source_id}/extract")
def extract_source(source_id: int, db: Session = Depends(get_db)):
    source = _ensure_source_in_project(db, source_id)
    existing = db.scalars(select(Memory).where(Memory.source_id == source.id).order_by(Memory.id)).all()
    if existing:
        return {
            "source": _source_dict(source),
            "method": "existing",
            "warning": "This source has already been extracted. Existing memories were returned to avoid duplicates.",
            "memories": [_memory_dict(db, memory) for memory in existing],
        }

    method = "rules"
    warning = "OPENAI_API_KEY is not set, so simple rule-based extraction was used. Review every candidate carefully."
    if os.getenv("OPENAI_API_KEY"):
        try:
            items = _llm_extract(source.content)
            method = "llm"
            warning = "AI-extracted candidates still require your review. Exact source quotes are checked against the captured source."
        except Exception as exc:  # Graceful fallback if provider is unavailable.
            items = _simple_extract(source.content)
            warning = f"The LLM extraction failed, so rule-based extraction was used instead ({type(exc).__name__}). Review every candidate carefully."
    else:
        items = _simple_extract(source.content)

    memories = []
    for item in items:
        memory = _new_memory(db, source.project_id, source, item)
        memories.append(memory)
        _log_event(
            db,
            source.project_id,
            "candidate_created",
            before_states={},
            after_states={str(memory.id): _memory_snapshot(memory)},
            reason=f"Candidate extracted from source: {source.title}",
            memory_ids=[memory.id],
            actor_type="system",
        )
    db.commit()
    return {
        "source": _source_dict(source),
        "method": method,
        "warning": warning,
        "memories": [_memory_dict(db, memory) for memory in memories],
    }


# ------------------------------- Memory API -----------------------------------

@app.get("/api/projects/{project_id}/memories")
def list_memories(project_id: int, status: str | None = None, memory_type: str | None = None, db: Session = Depends(get_db)):
    _ensure_project(db, project_id)
    query = select(Memory).where(Memory.project_id == project_id)
    if status and status != "all":
        if status not in MEMORY_STATUSES:
            raise HTTPException(status_code=400, detail="Unsupported memory status.")
        query = query.where(Memory.status == status)
    if memory_type and memory_type != "all":
        if memory_type not in MEMORY_TYPES:
            raise HTTPException(status_code=400, detail="Unsupported memory type.")
        query = query.where(Memory.memory_type == memory_type)
    memories = db.scalars(query.order_by(Memory.updated_at.desc(), Memory.id.desc())).all()
    return [_memory_dict(db, memory) for memory in memories]


@app.patch("/api/memories/{memory_id}")
def edit_memory(memory_id: int, payload: MemoryEdit, db: Session = Depends(get_db)):
    memory = _ensure_memory(db, memory_id)
    if payload.content is not None and not payload.content.strip():
        raise HTTPException(status_code=422, detail="Memory content cannot be blank.")
    if memory.status != "candidate":
        raise HTTPException(status_code=409, detail="Only pending candidates can be edited here. Create a new candidate to propose a change to approved memory.")
    before = _memory_snapshot(memory)
    old_content = memory.content
    old_type = memory.memory_type
    if payload.content is not None:
        memory.content = payload.content.strip()
    if payload.memory_type is not None:
        new_type = payload.memory_type.strip().lower()
        if new_type not in MEMORY_TYPES:
            raise HTTPException(status_code=422, detail=f"memory_type must be one of: {', '.join(sorted(MEMORY_TYPES))}")
        memory.memory_type = new_type
    if payload.rationale is not None:
        memory.rationale = payload.rationale.strip()
    changed_meaning = memory.content != old_content or memory.memory_type != old_type
    if changed_meaning:
        memory.source_check_status = "not_checked"
        if memory.memory_type == "decision":
            conflict = _decision_conflict(db, memory.project_id, memory.content)
            if conflict:
                memory.conflict_with_id = conflict.id
                memory.conflict_status = "unresolved"
            elif memory.conflict_status == "unresolved":
                memory.conflict_with_id = None
                memory.conflict_status = "none"
        elif memory.conflict_status == "unresolved":
            memory.conflict_with_id = None
            memory.conflict_status = "none"
    memory.updated_at = utcnow()
    after = _memory_snapshot(memory)
    _log_event(db, memory.project_id, "candidate_edited", {str(memory.id): before}, {str(memory.id): after})
    db.commit()
    db.refresh(memory)
    return _memory_dict(db, memory)


@app.post("/api/memories/{memory_id}/verify-source")
def verify_source(memory_id: int, payload: SourceVerification, db: Session = Depends(get_db)):
    memory = _ensure_memory(db, memory_id)
    if memory.status not in {"candidate", "approved"}:
        raise HTTPException(status_code=409, detail="Only candidate or current approved memories can be source-checked.")
    before = _memory_snapshot(memory)
    memory.source_check_status = payload.status
    memory.updated_at = utcnow()
    after = _memory_snapshot(memory)
    _log_event(db, memory.project_id, "source_verification_changed", {str(memory.id): before}, {str(memory.id): after}, reason=payload.note.strip())
    db.commit()
    db.refresh(memory)
    return _memory_dict(db, memory)


@app.post("/api/memories/{memory_id}/approve")
def approve_memory(memory_id: int, db: Session = Depends(get_db)):
    memory = _ensure_memory(db, memory_id)
    if memory.status != "candidate":
        raise HTTPException(status_code=409, detail="Only pending candidates can be approved.")
    if memory.conflict_status == "unresolved":
        raise HTTPException(status_code=409, detail="Resolve or explicitly leave the decision conflict before approving this memory.")
    if memory.source_check_status == "does_not_support":
        raise HTTPException(status_code=409, detail="The source was marked as not supporting this wording. Edit the candidate or resolve the evidence mismatch first.")
    before = _memory_snapshot(memory)
    memory.status = "approved"
    memory.approved_at = utcnow()
    memory.updated_at = utcnow()
    after = _memory_snapshot(memory)
    _log_event(db, memory.project_id, "memory_approved", {str(memory.id): before}, {str(memory.id): after})
    db.commit()
    db.refresh(memory)
    return _memory_dict(db, memory)


@app.post("/api/memories/{memory_id}/reject")
def reject_memory(memory_id: int, payload: RejectionRequest, db: Session = Depends(get_db)):
    memory = _ensure_memory(db, memory_id)
    if memory.status != "candidate":
        raise HTTPException(status_code=409, detail="Only pending candidates can be rejected.")
    before = _memory_snapshot(memory)
    memory.status = "rejected"
    memory.updated_at = utcnow()
    after = _memory_snapshot(memory)
    _log_event(db, memory.project_id, "candidate_rejected", {str(memory.id): before}, {str(memory.id): after}, reason=payload.reason.strip())
    db.commit()
    db.refresh(memory)
    return _memory_dict(db, memory)


@app.post("/api/memories/{memory_id}/reopen")
def reopen_memory(memory_id: int, db: Session = Depends(get_db)):
    memory = _ensure_memory(db, memory_id)
    if memory.status != "rejected":
        raise HTTPException(status_code=409, detail="Only rejected candidates can be reopened.")
    before = _memory_snapshot(memory)
    memory.status = "candidate"
    memory.conflict_status = "unresolved" if memory.conflict_with_id else "none"
    memory.source_check_status = "not_checked"
    memory.updated_at = utcnow()
    after = _memory_snapshot(memory)
    _log_event(db, memory.project_id, "candidate_reopened", {str(memory.id): before}, {str(memory.id): after})
    db.commit()
    db.refresh(memory)
    return _memory_dict(db, memory)


@app.post("/api/memories/{memory_id}/resolve-conflict")
def resolve_conflict(memory_id: int, payload: ConflictResolution, db: Session = Depends(get_db)):
    memory = _ensure_memory(db, memory_id)
    if memory.status != "candidate" or not memory.conflict_with_id:
        raise HTTPException(status_code=409, detail="This candidate does not have a resolvable conflict.")
    if payload.choice == "accept_new" and memory.source_check_status == "does_not_support":
        raise HTTPException(status_code=409, detail="The source was marked as not supporting this wording. Edit the candidate or resolve the evidence mismatch first.")
    existing = db.get(Memory, memory.conflict_with_id)
    if not existing or existing.status not in {"approved", "superseded"}:
        raise HTTPException(status_code=409, detail="The referenced prior decision is no longer available for conflict resolution.")
    affected = [memory]
    if existing.id != memory.id:
        affected.append(existing)
    before_states = _state_json(affected)

    if payload.choice == "leave_unresolved":
        memory.conflict_status = "unresolved"
        action = "conflict_left_unresolved"
    elif payload.choice == "keep_existing":
        memory.status = "rejected"
        memory.conflict_status = "keep_existing"
        memory.updated_at = utcnow()
        action = "conflict_resolved_keep_existing"
    else:  # accept_new
        if existing.status == "approved":
            existing.status = "superseded"
            existing.updated_at = utcnow()
        memory.status = "approved"
        memory.approved_at = utcnow()
        memory.updated_at = utcnow()
        memory.conflict_status = "accept_new"
        memory.supersedes_id = existing.id
        action = "conflict_resolved_accept_new"

    after_states = _state_json(affected)
    _log_event(
        db,
        memory.project_id,
        action,
        before_states,
        after_states,
        reason=payload.reason.strip(),
        memory_ids=[item.id for item in affected],
    )
    db.commit()
    db.refresh(memory)
    return {"memory": _memory_dict(db, memory), "related_memory": _memory_dict(db, existing), "resolution": payload.choice}


@app.post("/api/memories/{memory_id}/undo")
def undo_memory_action(memory_id: int, db: Session = Depends(get_db)):
    memory = _ensure_memory(db, memory_id)
    project_events = db.scalars(
        select(AuditEvent).where(AuditEvent.project_id == memory.project_id)
    ).all()
    undone_operations = {event.undoes_operation_id for event in project_events if event.undoes_operation_id}
    events = db.scalars(
        select(AuditEvent).where(
            AuditEvent.project_id == memory.project_id,
            AuditEvent.memory_id == memory_id,
            AuditEvent.action.in_(UNDOABLE_ACTIONS),
        ).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    ).all()
    target = next((event for event in events if event.operation_id not in undone_operations), None)
    if target is None:
        raise HTTPException(status_code=409, detail="There is no eligible action to undo for this memory.")
    try:
        before_group = json.loads(target.before_json or "{}").get("memories", {})
    except json.JSONDecodeError:
        before_group = {}
    if not before_group:
        raise HTTPException(status_code=409, detail="This action does not have a restorable previous state.")

    affected_ids = [int(value) for value in before_group.keys()]
    current_memories = [db.get(Memory, item_id) for item_id in affected_ids]
    current_memories = [item for item in current_memories if item is not None]
    current_states = _state_json(current_memories)
    for snapshot in before_group.values():
        _restore_snapshot(db, snapshot)
    restored_memories = [db.get(Memory, item_id) for item_id in affected_ids]
    restored_memories = [item for item in restored_memories if item is not None]
    restored_states = _state_json(restored_memories)
    undo_events = _log_event(
        db,
        memory.project_id,
        "undo",
        current_states,
        restored_states,
        reason=f"Undid action {target.action} (operation {target.operation_id}).",
        memory_ids=affected_ids,
        undoes_operation_id=target.operation_id,
    )
    db.commit()
    refreshed = db.get(Memory, memory_id)
    return {
        "message": f"Undid {target.action}. The original audit event remains in history.",
        "memory": _memory_dict(db, refreshed),
    }


# -------------------------------- Lexicon API ---------------------------------

@app.get("/api/projects/{project_id}/lexicon")
def list_lexicon(project_id: int, db: Session = Depends(get_db)):
    _ensure_project(db, project_id)
    terms = db.scalars(
        select(Memory).where(
            Memory.project_id == project_id,
            Memory.memory_type == "term",
            Memory.status.in_(["approved", "candidate"]),
        ).order_by(Memory.status.asc(), Memory.content.asc())
    ).all()
    return [_memory_dict(db, term) for term in terms]


@app.post("/api/projects/{project_id}/lexicon")
def add_lexicon_term(project_id: int, payload: SourceCreate, db: Session = Depends(get_db)):
    """Create a manual candidate term; user approval is still required."""
    project = _ensure_project(db, project_id)
    source = Source(project_id=project.id, title=payload.title.strip(), content=payload.content.strip(), source_type="note")
    db.add(source)
    db.flush()
    memory = Memory(
        project_id=project.id,
        source_id=source.id,
        memory_type="term",
        content=payload.content.strip(),
        rationale="Manually added to the Project Lexicon. Review and approve to make it current.",
        source_quote=payload.content.strip(),
        source_check_status="not_checked",
        status="candidate",
        conflict_status="none",
    )
    db.add(memory)
    db.flush()
    _log_event(db, project.id, "candidate_created", {}, {str(memory.id): _memory_snapshot(memory)}, "Manual lexicon term created.", [memory.id])
    db.commit()
    db.refresh(memory)
    return _memory_dict(db, memory)


# -------------------------------- History API ---------------------------------

@app.get("/api/projects/{project_id}/history")
def project_history(project_id: int, db: Session = Depends(get_db)):
    _ensure_project(db, project_id)
    rows = db.scalars(
        select(AuditEvent).where(AuditEvent.project_id == project_id).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(500)
    ).all()
    undone_operations = {event.undoes_operation_id for event in rows if event.undoes_operation_id}
    grouped = {}
    for event in rows:
        if event.operation_id not in grouped:
            grouped[event.operation_id] = {
                "representative": event,
                "memory_ids": [],
                "undone": event.operation_id in undone_operations,
                "event_ids": [],
            }
        group = grouped[event.operation_id]
        if event.memory_id and event.memory_id not in group["memory_ids"]:
            group["memory_ids"].append(event.memory_id)
        group["undone"] = event.operation_id in undone_operations
        group["event_ids"].append(event.id)
    result = []
    for group in grouped.values():
        result.append(_event_dict(group["representative"], group["memory_ids"], group["undone"]))
    result.sort(key=lambda item: (item["created_at"] or "", item["id"]), reverse=True)
    return result


# ------------------------------ Resume context --------------------------------

@app.post("/api/projects/{project_id}/resume-context")
def generate_resume_context(project_id: int, payload: ResumeRequest, db: Session = Depends(get_db)):
    project = _ensure_project(db, project_id)
    approved = db.scalars(
        select(Memory).where(Memory.project_id == project_id, Memory.status == "approved").order_by(Memory.updated_at.desc())
    ).all()
    unresolved = db.scalars(
        select(Memory).where(Memory.project_id == project_id, Memory.status == "candidate", Memory.conflict_status == "unresolved")
    ).all()

    type_sections = [
        (payload.include_decisions, "Confirmed decisions", "decision"),
        (payload.include_progress, "Current progress", "progress"),
        (payload.include_next_steps, "Next steps", "next_step"),
        (payload.include_constraints, "Constraints and preferences", "constraint"),
        (payload.include_lexicon, "Project terminology", "term"),
        (payload.include_history_notes, "Open questions", "question"),
    ]
    lines = [f"# Resume context: {project.name}", "", "Use this as context for continuing an existing project. Treat confirmed memories as the current saved state, not as proof that the underlying work is correct."]
    if payload.include_goal:
        lines += ["", "## Project goal", project.goal.strip() or "No project goal has been saved yet."]
        goal_memories = [m for m in approved if m.memory_type == "goal"]
        if goal_memories:
            lines += ["", "## Additional confirmed goal details"]
            lines += [f"- {m.content}" for m in goal_memories[:8]]

    included_ids = []
    for include, title, memory_type in type_sections:
        if not include:
            continue
        items = [m for m in approved if m.memory_type == memory_type]
        if memory_type == "question" and not payload.include_history_notes:
            continue
        if not items:
            continue
        lines += ["", f"## {title}"]
        for item in items[:12]:
            lines.append(f"- {item.content}")
            included_ids.append(item.id)

    warnings = []
    old_memories = []
    cutoff = datetime.now(timezone.utc).timestamp() - (45 * 24 * 60 * 60)
    for item in approved:
        dt = item.approved_at or item.updated_at
        if dt:
            if dt.tzinfo is None:
                dt_stamp = dt.replace(tzinfo=timezone.utc).timestamp()
            else:
                dt_stamp = dt.timestamp()
            if dt_stamp < cutoff:
                old_memories.append(item.id)
    if old_memories:
        warnings.append(f"{len(old_memories)} approved memories have not been updated for more than 45 days. Check whether they are still current; age alone does not make them invalid.")
    if unresolved:
        warnings.append(f"There are {len(unresolved)} unresolved candidate conflict(s). They are excluded from confirmed facts.")
        lines += ["", "## Unresolved decisions — do not treat as confirmed"]
        for item in unresolved[:8]:
            lines.append(f"- Proposed: {item.content}")
            conflict = db.get(Memory, item.conflict_with_id) if item.conflict_with_id else None
            if conflict:
                lines.append(f"  Existing decision: {conflict.content}")
    if not included_ids and not (payload.include_goal and project.goal.strip()):
        lines += ["", "## Saved context", "No approved memories were selected or available. Capture a source, review candidate memories, and approve the information that should be preserved."]
    lines += ["", "## Suggested continuation", "First verify that this saved state still matches the project's latest work, then continue from the next confirmed action. Ask before changing or assuming any unresolved decision."]
    return {
        "project_id": project.id,
        "project_name": project.name,
        "generated_at": _iso(utcnow()),
        "content": "\n".join(lines),
        "included_memory_ids": included_ids,
        "warnings": warnings,
    }



# --------------------------- Simple chatbot workflow --------------------------

OUTPUT_TYPES = {"summary", "final_conclusion", "project_status"}
ALLOWED_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json"}
ALLOWED_IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = 16 * 1024 * 1024


def _guess_project_name(text: str, filenames: list[str], requested: str = "") -> str:
    if requested.strip():
        return requested.strip()[:160]
    if text.strip():
        first = re.sub(r"\s+", " ", text.strip().splitlines()[0]).strip(" #:-")
        first = re.sub(r"^(goal|project|topic|subject)\s*:\s*", "", first, flags=re.I)
        if first:
            return (first[:54].rstrip() + ("…" if len(first) > 54 else ""))
    if filenames:
        return Path(filenames[0]).stem[:160] or "My workspace"
    return "My workspace"


async def _read_attachments(files: list[UploadFile]) -> tuple[str, list[dict], list[str]]:
    """Read common text documents and keep image bytes only for this model call."""
    text_blocks: list[str] = []
    images: list[dict] = []
    filenames: list[str] = []
    total_bytes = 0
    if len(files) > 8:
        raise HTTPException(status_code=400, detail="Please attach no more than 8 files at a time.")

    for upload in files:
        filename = Path(upload.filename or "attachment").name
        suffix = Path(filename).suffix.lower()
        raw = await upload.read()
        if not raw:
            continue
        total_bytes += len(raw)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"{filename} is larger than 8 MB.")
        if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="The combined upload size must be 16 MB or less.")
        filenames.append(filename)

        if suffix in ALLOWED_TEXT_EXTENSIONS:
            decoded = raw.decode("utf-8", errors="replace")
            text_blocks.append(f"[Extracted text from {filename}]\n{decoded}")
        elif suffix == ".pdf":
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(raw))
                extracted = "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Couldn't read {filename} as a PDF ({type(exc).__name__}).") from exc
            if not extracted:
                raise HTTPException(status_code=400, detail=f"No selectable text was found in {filename}. Export its text or upload screenshots of the relevant pages.")
            text_blocks.append(f"[Extracted text from {filename}]\n{extracted}")
        elif suffix == ".docx":
            try:
                from docx import Document
                document = Document(io.BytesIO(raw))
                extracted = "\n".join(p.text for p in document.paragraphs if p.text.strip()).strip()
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Couldn't read {filename} as a Word document ({type(exc).__name__}).") from exc
            if extracted:
                text_blocks.append(f"[Extracted text from {filename}]\n{extracted}")
            else:
                raise HTTPException(status_code=400, detail=f"No readable paragraph text was found in {filename}.")
        elif suffix in ALLOWED_IMAGE_MIMES:
            images.append({
                "filename": filename,
                "mime_type": ALLOWED_IMAGE_MIMES[suffix],
                "data_url": f"data:{ALLOWED_IMAGE_MIMES[suffix]};base64,{base64.b64encode(raw).decode('ascii')}",
            })
        else:
            raise HTTPException(status_code=415, detail=f"Unsupported file type: {filename}. Use TXT, Markdown, CSV, JSON, PDF, DOCX or an image (PNG, JPG, WEBP, GIF).")

    return "\n\n".join(text_blocks), images, filenames


def _local_output(text: str, output_type: str, project_name: str, existing: list[Memory], project_goal: str = "") -> str:
    """Honest offline fallback: format source text and saved memory without pretending to reason like an LLM."""
    raw = (text or "").strip()
    chunks = [re.sub(r"\s+", " ", item).strip(" -*•\t") for item in re.split(r"\n+|(?<=[.!?])\s+", raw) if item.strip()]
    chunks = list(dict.fromkeys(chunks))
    if output_type == "summary":
        if not chunks:
            return "## Summary\nNo readable text was provided."
        return "## Summary\n" + "\n".join(f"- {item[:450]}" for item in chunks[:7]) + "\n\n*Basic local mode: this is a formatted extract, not an AI-generated interpretation.*"
    if output_type == "final_conclusion":
        conclusion = " ".join(chunks[-3:]) if chunks else "No conclusion can be determined because no readable text was provided."
        return "## Conclusion from the supplied material\n\n" + conclusion[:1800] + "\n\n*Basic local mode: check this against the original material; no AI reasoning was used.*"

    by_type: dict[str, list[str]] = {"goal": [], "decision": [], "progress": [], "next_step": [], "constraint": [], "question": []}
    prefixes = [
        ("goal", re.compile(r"^(?:our )?goal|^our idea|^objective", re.I)),
        ("decision", re.compile(r"^(?:decision|current approach|approved approach|we decided)\s*[:—-]", re.I)),
        ("progress", re.compile(r"^(?:completed|finished|done|implemented|approved)\s*[:—-]", re.I)),
        ("next_step", re.compile(r"^(?:pending|next step|todo|to do)\s*[:—-]", re.I)),
        ("constraint", re.compile(r"^(?:constraint|limitation|must)\s*[:—-]", re.I)),
        ("question", re.compile(r"^(?:open question|unclear|need to decide)\s*[:—-]", re.I)),
    ]
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("-*• ")
        for kind, pattern in prefixes:
            if pattern.search(stripped):
                value = re.sub(r"^[^:—-]+\s*[:—-]\s*", "", stripped).strip()
                if value:
                    by_type[kind].append(value)
                break
    for memory in existing:
        if memory.memory_type in by_type and memory.status == "approved":
            by_type[memory.memory_type].append(memory.content)
    sections = [
        ("Our goal", [project_goal] if project_goal else [] , by_type["goal"]),
        ("Current approach", [], by_type["decision"]),
        ("Completed / approved", [], by_type["progress"]),
        ("Pending / next steps", [], by_type["next_step"] + by_type["question"]),
        ("Constraints", [], by_type["constraint"]),
    ]
    lines = [f"## Project status — {project_name}"]
    for heading, direct_values, values in sections:
        unique = list(dict.fromkeys([*direct_values, *values]))
        lines += ["", f"### {heading}"]
        lines += [f"- {value[:450]}" for value in unique[:8]] if unique else ["- Not clearly stated in the supplied material."]
    if chunks:
        lines += ["", "### Overall summary", " ".join(chunks[:3])[:1000]]
    lines += ["", "*Basic local mode: fields are organized from explicit labels and saved memory. Add an API key for AI interpretation.*"]
    return "\n".join(lines)


def _llm_process(text: str, output_type: str, project_name: str, current_memory: list[Memory], images: list[dict]) -> dict:
    from openai import OpenAI

    memory_context = "\n".join(
        f"- [{item.memory_type}] {item.content}" for item in current_memory[:25]
    ) or "No existing approved project memories."
    output_guidance = {
        "summary": "Give a concise summary with the key points and important decisions.",
        "final_conclusion": "Give the clearest final conclusion or recommendation, then note any important caveat or unresolved uncertainty.",
        "project_status": "Use exactly these headings: Our goal; Current approach; Completed / approved; Pending / next steps; Overall summary. Distinguish confirmed progress from assumptions and state when a field is not provided.",
    }[output_type]
    prompt = f"""You are Continuity, a careful project-memory assistant.
Project: {project_name}
Requested output: {output_type}
Output requirements: {output_guidance}

Current approved project memory (background context, not automatically proof of what the new source says):
{memory_context}

New source text:
{text[:65000] or '[No text; inspect the attached image(s).]'}

Return one JSON object with this structure:
{{
  "response": "Markdown output for the user, following the requested output requirements",
  "memories": [
    {{"type":"goal|decision|progress|next_step|constraint|term|question|other", "content":"short reusable project fact", "rationale":"why it matters", "source_quote":"exact short quote from the text, or empty if not available", "confidence":"high|medium|low", "explicit":true}}
  ],
  "clarifications": [{{"question":"only a genuinely important unclear question", "reason":"why it affects the current project state"}}]
}}
Rules:
- Do the requested output first; do not add generic filler.
- Do not invent content, decisions, completed work or future plans.
- Extract only durable project memory. Skip routine details and generic sentences.
- Use high confidence and explicit=true only for clearly stated facts or decisions. Be conservative.
- If a new decision may conflict with an existing approved decision, keep it as a candidate and explain the conflict in rationale.
- source_quote must be copied exactly from New source text. Never manufacture a source quote. For claims based only on an image, leave source_quote empty.
- Ask at most 3 questions, and only when the ambiguity materially affects what should be remembered or what happens next.
- If the source contains brainstorming, do not treat it as a final decision.
- Keep memory candidates to at most 8 items and clarifications to at most 3.
"""
    user_content: list[dict] = [{"type": "text", "text": prompt}]
    for image in images:
        user_content.append({"type": "image_url", "image_url": {"url": image["data_url"], "detail": "high"}})
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.15,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Be accurate, concise and conservative. Never silently decide between contradictory project states."},
            {"role": "user", "content": user_content},
        ],
    )
    result = json.loads(response.choices[0].message.content or "{}")
    if not isinstance(result, dict) or not isinstance(result.get("response"), str):
        raise ValueError("The AI response was not in the expected format.")
    memories = []
    for item in result.get("memories", [])[:8]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", "other")).strip().lower()
        content = str(item.get("content", "")).strip()[:4000]
        if not content:
            continue
        if kind not in MEMORY_TYPES:
            kind = "other"
        confidence = str(item.get("confidence", "low")).lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        memories.append({
            "type": kind,
            "content": content,
            "rationale": str(item.get("rationale", "")).strip()[:1000],
            "source_quote": str(item.get("source_quote", "")).strip()[:2000],
            "confidence": confidence,
            "explicit": bool(item.get("explicit", False)),
        })
    clarifications = []
    for item in result.get("clarifications", [])[:3]:
        if isinstance(item, dict) and str(item.get("question", "")).strip():
            clarifications.append({
                "question": str(item["question"]).strip()[:1000],
                "reason": str(item.get("reason", "")).strip()[:1000],
            })
    return {"response": result["response"].strip(), "memories": memories, "clarifications": clarifications}


def _local_memories(text: str) -> list[dict]:
    """Offline mode only auto-saves explicitly labelled lines; other prose is not guessed."""
    patterns = [
        ("goal", re.compile(r"^(?:our )?goal|^our idea|^objective\s*[:—-]\s*(.+)$", re.I)),
        ("decision", re.compile(r"^(?:decision|current approach|approved approach)\s*[:—-]\s*(.+)$", re.I)),
        ("progress", re.compile(r"^(?:completed|finished|done|implemented|approved)\s*[:—-]\s*(.+)$", re.I)),
        ("next_step", re.compile(r"^(?:pending|next step|todo|to do)\s*[:—-]\s*(.+)$", re.I)),
        ("constraint", re.compile(r"^(?:constraint|limitation|must)\s*[:—-]\s*(.+)$", re.I)),
        ("term", re.compile(r"^term\s*[:—-]\s*(.+)$", re.I)),
    ]
    output = []
    seen = set()
    for line in (text or "").splitlines():
        candidate = line.strip().lstrip("-*• ")
        for kind, pattern in patterns:
            match = pattern.match(candidate)
            if not match:
                continue
            value = candidate.split(":", 1)[-1].strip() if ":" in candidate else re.sub(r"^[^—-]+[—-]\s*", "", candidate).strip()
            if len(value) < 4:
                break
            normalized = _normalise(value)
            if normalized in seen:
                break
            seen.add(normalized)
            output.append({"type": kind, "content": value[:4000], "rationale": "Explicitly labelled in the supplied text.", "source_quote": candidate[:2000], "confidence": "high", "explicit": True})
            break
        if len(output) >= 8:
            break
    return output


def _run_dict(run: ProcessingRun) -> dict:
    try:
        attachments = json.loads(run.attachment_names_json or "[]")
    except json.JSONDecodeError:
        attachments = []
    return {
        "id": run.id,
        "project_id": run.project_id,
        "source_id": run.source_id,
        "output_type": run.output_type,
        "output_content": run.output_content,
        "method": run.method,
        "attachment_names": attachments,
        "auto_saved_count": run.auto_saved_count,
        "needs_input_count": run.needs_input_count,
        "created_at": _iso(run.created_at),
    }


@app.post("/api/process")
async def process_material(
    text: str = Form(default=""),
    output_type: Literal["summary", "final_conclusion", "project_status"] = Form(default="project_status"),
    project_id: str = Form(default=""),
    project_name: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    """Main chat-like flow: accept text/files, produce the requested output and update project memory."""
    text = (text or "").strip()
    extracted_attachment_text, images, filenames = await _read_attachments(files)
    if images and not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=400, detail="Image understanding needs OPENAI_API_KEY. You can still process pasted text and text-based documents in local mode.")
    material_text = "\n\n".join(part for part in [text, extracted_attachment_text] if part.strip()).strip()
    if not material_text and not images:
        raise HTTPException(status_code=400, detail="Paste some text or attach a document/image first.")
    if len(material_text) > 90000:
        material_text = material_text[:90000]

    project = None
    if project_id.strip():
        try:
            project = _ensure_project(db, int(project_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid project selection.") from exc
    existing = []
    if project:
        existing = db.scalars(select(Memory).where(Memory.project_id == project.id, Memory.status == "approved").order_by(Memory.updated_at.desc()).limit(30)).all()
    name_for_prompt = project.name if project else _guess_project_name(text or extracted_attachment_text, filenames, project_name)
    stored_source_text_parts = [material_text] if material_text else []
    if images:
        stored_source_text_parts.extend([f"[Image attached and analyzed for this processing run: {image['filename']}. Original image bytes are not stored in the database.]" for image in images])
    stored_source_text = "\n\n".join(stored_source_text_parts).strip()[:100000]

    method = "local"
    analysis = None
    warning = ""
    if os.getenv("OPENAI_API_KEY"):
        try:
            analysis = _llm_process(material_text, output_type, name_for_prompt, existing, images)
            method = "openai"
        except Exception as exc:
            if images:
                raise HTTPException(status_code=502, detail=f"AI processing failed while handling the attached image(s). Nothing was saved. Check the API key/model and try again ({type(exc).__name__}).") from exc
            warning = f"AI processing failed, so basic local formatting was used ({type(exc).__name__})."
    if analysis is None:
        if images:
            raise HTTPException(status_code=502, detail="Images require a working AI connection.")
        response_text = _local_output(material_text, output_type, name_for_prompt, existing, project.goal if project else "")
        candidate_items = _local_memories(material_text)
        analysis = {"response": response_text, "memories": candidate_items, "clarifications": []}
        if not warning:
            warning = "Basic local mode. Text is organized without an LLM; add OPENAI_API_KEY for AI interpretation and image understanding."

    if project is None:
        project = Project(name=name_for_prompt, goal="")
        db.add(project)
        db.flush()
        _log_object_event(db, project.id, "project_created", "project", None, _project_dict(project), "Workspace created automatically from first submission.")

    source_title = (filenames[0] if len(filenames) == 1 else f"Imported material · {datetime.now().strftime('%Y-%m-%d %H:%M')}") if filenames else f"Conversation · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    source_type = "conversation" if text else "text"
    source = Source(project_id=project.id, title=source_title[:240], content=stored_source_text, source_type=source_type)
    db.add(source)
    db.flush()
    _log_object_event(db, project.id, "source_captured", "source", None, _source_dict(source), f"Captured input: {source.title}")

    saved = 0
    needs_input = 0
    skipped = 0
    current_memory_texts = {
        _normalise(memory.content) for memory in db.scalars(select(Memory).where(Memory.project_id == project.id, Memory.status == "approved")).all()
    }
    for item in analysis.get("memories", [])[:8]:
        normalized = _normalise(item.get("content", ""))
        if not normalized or normalized in current_memory_texts:
            skipped += 1
            continue
        memory = _new_memory(db, project.id, source, item)
        quote_is_exact = bool(item.get("source_quote")) and _quote_found(item.get("source_quote", ""), stored_source_text)
        is_clear = (
            item.get("confidence") == "high"
            and item.get("explicit") is True
            and quote_is_exact
            and item.get("type") != "question"
            and memory.conflict_status != "unresolved"
        )
        before = {}
        if is_clear:
            memory.status = "approved"
            memory.approved_at = utcnow()
            memory.updated_at = utcnow()
            action = "memory_auto_saved"
            reason = "Automatically saved: explicit, high-confidence information with a matching source quote and no detected decision conflict."
            saved += 1
            current_memory_texts.add(normalized)
        else:
            action = "candidate_created"
            reason = "Needs input because the information was uncertain, lacked an exact source quote, or may conflict with current memory."
            needs_input += 1
        _log_event(db, project.id, action, before, {str(memory.id): _memory_snapshot(memory)}, reason=reason, memory_ids=[memory.id], actor_type="system")

    for clarification in analysis.get("clarifications", [])[:3]:
        question = clarification["question"].strip()
        normalized = _normalise(question)
        if not normalized or normalized in current_memory_texts:
            continue
        item = {"type": "question", "content": question, "rationale": clarification.get("reason", "Clarification requested by AI."), "source_quote": ""}
        memory = _new_memory(db, project.id, source, item)
        _log_event(db, project.id, "candidate_created", {}, {str(memory.id): _memory_snapshot(memory)}, reason="Clarification generated from the latest source.", memory_ids=[memory.id], actor_type="system")
        needs_input += 1

    run = ProcessingRun(
        project_id=project.id,
        source_id=source.id,
        output_type=output_type,
        output_content=analysis["response"][:30000],
        method=method,
        attachment_names_json=json.dumps(filenames, ensure_ascii=False),
        auto_saved_count=saved,
        needs_input_count=needs_input,
    )
    db.add(run)
    project.updated_at = utcnow()
    db.commit()
    db.refresh(run)
    return {
        "project": _project_dict(project),
        "run": _run_dict(run),
        "output": analysis["response"],
        "output_type": output_type,
        "method": method,
        "warning": warning,
        "memory": {"saved": saved, "needs_input": needs_input, "duplicates_skipped": skipped},
    }


@app.get("/api/projects/{project_id}/runs")
def get_project_runs(project_id: int, db: Session = Depends(get_db)):
    _ensure_project(db, project_id)
    runs = db.scalars(select(ProcessingRun).where(ProcessingRun.project_id == project_id).order_by(ProcessingRun.created_at.desc(), ProcessingRun.id.desc()).limit(10)).all()
    return [_run_dict(run) for run in runs]
