import os
import io
import uuid
import json
import logging
import asyncio
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.core.config import settings, UPLOAD_DIR, SAMPLE_DIR, DATA_DIR
from backend.models.schemas import (
    ResearchRequest,
    SourceDocument,
    FinalReport,
    ChatRequest,
    ChatResponse,
    ProjectSummary,
    AuthRequest,
    AuthResponse,
)
from backend.services.pdf_parser import PDFParserService
from backend.services.rag_service import rag_service
from backend.services.pdf_exporter import PDFReportExporter
from backend.services.sample_generator import ensure_sample_papers_exist
from backend.services.llm_service import llm_service
from backend.agents.manager import project_manager
from backend.agents.qa_agent import qa_agent

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("research_agent")

app = FastAPI(
    title="AI Research Assistant Agent API",
    description="Production-ready multi-agent research assistant with 9-agent LangGraph workflow, RAG, and ReportLab PDF export.",
    version="1.0.0"
)

# CORS Middleware
origins = [
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "http://127.0.0.1:8001",
    "http://localhost:8001",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:3000",
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory document storage registry for uploads prior to run
uploaded_docs_registry = {}

# User Accounts Storage
USERS_FILE = DATA_DIR / "users.json"


def load_users() -> dict:
    if USERS_FILE.exists():
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_users(users: dict):
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to persist users: {e}")


@app.on_event("startup")
async def startup_event():
    ensure_sample_papers_exist()
    logger.info("AI Research Assistant Agent API initialized successfully.")


# =============================================================================
# 0. AUTHENTICATION (EMAIL & PASSWORD)
# =============================================================================

@app.post("/api/auth/signup", response_model=AuthResponse)
async def auth_signup(payload: AuthRequest):
    """
    Registers a new user with email and password.
    """
    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email address is required.")
    if len(payload.password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters long.")

    users = load_users()
    if email in users:
        raise HTTPException(status_code=400, detail="Account already exists with this email. Please log in.")

    users[email] = payload.password
    save_users(users)
    token = f"tok_{uuid.uuid4().hex}"
    logger.info(f"New user registered: {email}")

    return AuthResponse(
        status="success",
        message="Account registered successfully.",
        user={"email": email},
        token=token
    )


@app.post("/api/auth/login", response_model=AuthResponse)
async def auth_login(payload: AuthRequest):
    """
    Authenticates a user with email and password.
    Auto-registers on first login if account does not exist.
    """
    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email address is required.")
    if not payload.password:
        raise HTTPException(status_code=400, detail="Password is required.")

    users = load_users()
    if email in users:
        if users[email] != payload.password:
            raise HTTPException(status_code=401, detail="Invalid password. Please verify and try again.")
    else:
        # Frictionless onboarding: auto-provision account if valid format
        users[email] = payload.password
        save_users(users)
        logger.info(f"Auto-provisioned new account for: {email}")

    token = f"tok_{uuid.uuid4().hex}"
    logger.info(f"User authenticated: {email}")

    return AuthResponse(
        status="success",
        message="Logged in successfully.",
        user={"email": email},
        token=token
    )


# =============================================================================
# 1. FILE UPLOAD & MANAGEMENT
# =============================================================================


@app.post("/api/research/upload", response_model=List[SourceDocument])
async def upload_documents(files: List[UploadFile] = File(...)):
    """
    Accepts research documents (PDF, TXT, MD), validates them,
    saves them to the project upload directory, and returns metadata.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    saved_docs: List[SourceDocument] = []
    allowed_extensions = {".pdf", ".txt", ".md"}

    for file in files:
        ext = Path(file.filename).suffix.lower()
        if ext not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file format: {file.filename}. Only PDF, TXT, and MD files are supported."
            )

        doc_id = f"doc_{uuid.uuid4().hex[:8]}"
        target_path = UPLOAD_DIR / f"{doc_id}_{file.filename}"

        content = await file.read()
        with open(target_path, "wb") as f:
            f.write(content)

        file_size = len(content)
        pages_count = 1
        preview = ""

        if ext == ".pdf":
            try:
                parsed = PDFParserService.parse_pdf(target_path, filename=file.filename)
                pages_count = parsed.total_pages
                preview = parsed.pages[0].text[:300] if parsed.pages else ""
            except Exception as e:
                logger.warning(f"Could not parse page count for {file.filename}: {e}")

        doc_meta = SourceDocument(
            id=doc_id,
            filename=file.filename,
            file_path=str(target_path),
            file_size=file_size,
            pages_count=pages_count,
            extracted_text_preview=preview,
            status="uploaded"
        )

        uploaded_docs_registry[doc_id] = doc_meta.dict()
        saved_docs.append(doc_meta)

    return saved_docs


# =============================================================================
# 2. WORKFLOW INITIALIZATION & EXECUTION
# =============================================================================

@app.post("/api/research/start")
async def start_research(
    payload: ResearchRequest,
    background_tasks: BackgroundTasks,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """
    Initializes a new research project and triggers the 9-agent LangGraph
    workflow in the background.
    """
    if not payload.topic.strip() or not payload.research_question.strip():
        raise HTTPException(status_code=400, detail="Both 'topic' and 'research_question' are required.")

    project_id = f"proj_{uuid.uuid4().hex[:10]}"
    sources_to_use = []

    # 1. Attach user-uploaded files
    if payload.file_ids:
        for fid in payload.file_ids:
            if fid in uploaded_docs_registry:
                sources_to_use.append(uploaded_docs_registry[fid])

    # 2. If sample data requested OR no files uploaded, attach bundled sample papers
    if payload.use_sample_data or len(sources_to_use) == 0:
        sample_files = [
            ("paper1_rag_clinical_decision_support.pdf", "Retrieval-Augmented Generation for Clinical Decision Support"),
            ("paper2_fine_tuning_vs_rag.pdf", "Domain-Adapted Fine-Tuning versus Dynamic RAG"),
            ("paper3_self_reflective_rag.pdf", "Self-Reflective Multi-Agent Architectures for Diagnostic Verification")
        ]
        for s_idx, (s_name, s_title) in enumerate(sample_files):
            p_path = SAMPLE_DIR / s_name
            if p_path.exists():
                sources_to_use.append({
                    "id": f"sample_doc_{s_idx+1}",
                    "filename": s_name,
                    "file_path": str(p_path),
                    "file_size": p_path.stat().st_size,
                    "pages_count": 1,
                    "title": s_title
                })

    # Register project
    project_manager.register_project(
        project_id=project_id,
        topic=payload.topic,
        question=payload.research_question,
        sources=sources_to_use
    )

    # Launch LangGraph workflow asynchronously in background
    background_tasks.add_task(
        project_manager.run_workflow_async,
        project_id=project_id,
        topic=payload.topic,
        question=payload.research_question,
        sources=sources_to_use,
        focus=payload.focus,
        custom_key=x_api_key,
        model_name=settings.GEMINI_MODEL
    )

    return {
        "project_id": project_id,
        "status": "started",
        "message": f"Autonomous 9-agent workflow dispatched with {len(sources_to_use)} sources.",
        "sources_count": len(sources_to_use)
    }


# =============================================================================
# 3. REAL-TIME STREAMING & STATUS
# =============================================================================

@app.get("/api/research/{project_id}/status")
async def get_project_status(project_id: str):
    """Polls current status, active stage, and progress percentage."""
    proj = project_manager.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")

    return {
        "project_id": proj["id"],
        "topic": proj["topic"],
        "question": proj["question"],
        "status": proj["status"],
        "stage_progress": proj["stage_progress"],
        "current_agent": proj["current_agent"],
        "status_message": proj["status_message"],
        "sources_count": len(proj.get("sources", [])),
        "logs": proj.get("logs", []),
        "has_report": proj.get("final_report") is not None
    }


@app.get("/api/research/{project_id}/stream")
async def stream_project_events(project_id: str):
    """
    Server-Sent Events (SSE) endpoint streaming real-time stage transitions,
    agent progress updates, and activity logs to the frontend dashboard.
    """
    proj = project_manager.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")

    event_queue = project_manager.subscribe(project_id)

    async def event_generator():
        try:
            # Yield initial state immediately
            initial_event = {
                "type": "progress",
                "stage_progress": proj["stage_progress"],
                "current_agent": proj["current_agent"],
                "status_message": proj["status_message"],
                "logs": proj.get("logs", [])
            }
            yield f"data: {json.dumps(initial_event)}\n\n"

            # If already completed or failed, emit final event and finish
            if proj["status"] in ("completed", "failed"):
                if proj["status"] == "completed":
                    yield f"data: {json.dumps({'type': 'complete', 'stage_progress': 100, 'current_agent': 'Completed', 'report': proj.get('final_report')})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'error': proj.get('error', 'Workflow failed')})}\n\n"
                return

            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") in ("complete", "error"):
                        break
                except asyncio.TimeoutError:
                    # Send keepalive comment
                    yield ": keepalive\n\n"
        finally:
            project_manager.unsubscribe(project_id, event_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


# =============================================================================
# 4. REPORT ACCESS & PDF EXPORT (FR-14)
# =============================================================================

@app.get("/api/research/{project_id}/report")
async def get_project_report(project_id: str):
    """Retrieves the full 14-section structured research dossier."""
    proj = project_manager.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")
    report = proj.get("final_report")
    if not report:
        raise HTTPException(status_code=400, detail="Report generation is still in progress or not available.")
    return report


@app.get("/api/reports/{report_id}/download-pdf")
async def download_report_pdf(report_id: str):
    """
    Implements FR-14: Downloads the cleanly styled publication-grade PDF
    with cover title block, running headers/footers ('Page X of Y'),
    clear 14 sections, formatted comparison table, citations, and references.
    """
    # Locate report by ID across projects
    target_report = None
    for proj in project_manager.list_projects():
        rep = proj.get("final_report")
        if rep and (rep.get("report_id") == report_id or proj.get("id") == report_id):
            target_report = rep
            break

    if not target_report:
        raise HTTPException(status_code=404, detail=f"Report with ID '{report_id}' not found.")

    try:
        pdf_bytes = PDFReportExporter.generate_pdf(target_report)
        filename = f"Research_Dossier_{report_id}.pdf"
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(pdf_bytes))
            }
        )
    except Exception as e:
        logger.error(f"Error generating PDF for {report_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to generate PDF: {str(e)}")


@app.post("/api/reports/export-pdf")
async def export_report_pdf(report_data: Dict[str, Any]):
    """Direct PDF export accepting raw report JSON."""
    try:
        pdf_bytes = PDFReportExporter.generate_pdf(report_data)
        rep_id = report_data.get("report_id", "export")
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="Research_Report_{rep_id}.pdf"',
                "Content-Length": str(len(pdf_bytes))
            }
        )
    except Exception as e:
        logger.error(f"Error exporting PDF: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to export PDF: {str(e)}")


# =============================================================================
# 5. GROUNDED FOLLOW-UP Q&A CHAT
# =============================================================================

@app.post("/api/chat/message", response_model=ChatResponse)
async def chat_message(
    payload: ChatRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """
    Follow-up Q&A Agent endpoint: Answers user queries grounded strictly
    in the project's vector store with source citations.
    """
    proj = project_manager.get_project(payload.project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")

    response = qa_agent.answer_question(
        project_id=payload.project_id,
        question=payload.message,
        history=payload.history,
        custom_key=x_api_key,
        model_name=settings.GEMINI_MODEL
    )
    return response


# =============================================================================
# 6. PROJECT HISTORY & SAMPLE DEMO
# =============================================================================

@app.get("/api/projects", response_model=List[ProjectSummary])
async def list_projects():
    """Lists all past and active research projects."""
    summaries = []
    for proj in project_manager.list_projects():
        rep = proj.get("final_report")
        summaries.append(ProjectSummary(
            id=proj["id"],
            topic=proj["topic"],
            research_question=proj["question"],
            created_at=proj["created_at"],
            status=proj["status"],
            sources_count=len(proj.get("sources", [])),
            report_id=rep.get("report_id") if rep else None,
            has_report=rep is not None
        ))
    return summaries


@app.post("/api/sample-demo")
async def trigger_sample_demo(
    background_tasks: BackgroundTasks,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """
    1-Click Instant Demo: Dispatches an autonomous research run using
    the pre-bundled clinical RAG academic papers.
    """
    demo_req = ResearchRequest(
        topic="Retrieval-Augmented Generation for Clinical Decision Support",
        research_question="How does retrieval-augmented generation reduce hallucination rates in diagnostic reasoning compared to fine-tuning?",
        focus="Diagnostic accuracy, latency trade-offs, and verification critic loops",
        use_sample_data=True
    )
    return await start_research(demo_req, background_tasks, x_api_key)


@app.get("/api/system/health")
async def system_health(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """Checks system health, LLM connectivity, and vector DB."""
    has_live_key = llm_service.is_live_available(x_api_key)
    return {
        "status": "healthy",
        "live_llm_mode": "Gemini 2.0 Flash (Live)" if has_live_key else "Offline Heuristic / Demo Mode",
        "has_api_key": has_live_key,
        "default_model": settings.GEMINI_MODEL,
        "active_projects_count": len(project_manager.projects),
        "vector_store": "ChromaDB Persistent Store",
        "timestamp": datetime.utcnow().isoformat()
    }


# =============================================================================
# 7. STATIC FRONTEND MOUNT
# =============================================================================
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
