from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.schemas.examples import (
    ExamplePaperLibraryResponse,
    ExamplePairUploadResponse,
    ExampleQuestionCreate,
    ExampleQuestionUpdate,
    ExamplePairUploadQuestion,
)
from app.services.example_library import (
    add_question_to_example_paper,
    delete_example_question,
    list_example_papers,
    update_example_question,
)
from app.services.example_upload import MAX_EXAMPLE_QUESTIONS, upload_example_pairs
from app.services.paper_pipeline import MAX_PDF_BYTES

router = APIRouter()


@router.get("", response_model=ExamplePaperLibraryResponse)
async def get_example_papers(db: AsyncSession = Depends(get_db_session)):
    """List stored source papers with all associated questions."""
    return await list_example_papers(db)


@router.post(
    "/papers/{source_paper_id}/questions",
    response_model=ExamplePairUploadQuestion,
    status_code=201,
)
async def add_example_question(
    source_paper_id: UUID,
    payload: ExampleQuestionCreate,
    db: AsyncSession = Depends(get_db_session),
):
    try:
        return await add_question_to_example_paper(
            db,
            source_paper_id,
            question_text=payload.question_text,
            rationale=payload.rationale,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.patch("/questions/{question_id}", response_model=ExamplePairUploadQuestion)
async def edit_example_question(
    question_id: UUID,
    payload: ExampleQuestionUpdate,
    db: AsyncSession = Depends(get_db_session),
):
    try:
        return await update_example_question(
            db,
            question_id,
            question_text=payload.question_text,
            rationale=payload.rationale,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.delete("/questions/{question_id}", status_code=204)
async def remove_example_question(
    question_id: UUID,
    db: AsyncSession = Depends(get_db_session),
):
    try:
        await delete_example_question(db, question_id)
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    return Response(status_code=204)


@router.post("/upload", response_model=ExamplePairUploadResponse, status_code=201)
async def upload_paper_question_pairs(
    source_type: Literal["own_criticism", "openreview_review"] = Form(...),
    questions: list[str] = Form(...),
    rationales: list[str] | None = Form(default=None),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db_session),
):
    if file.content_type not in {"application/pdf", "application/octet-stream"} and not (
        (file.filename or "").lower().endswith(".pdf")
    ):
        raise HTTPException(415, detail="Upload a PDF file.")
    data = await file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, detail="PDF must be 20 MB or smaller.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(415, detail="The uploaded file is not a valid PDF.")
    if len(questions) > MAX_EXAMPLE_QUESTIONS:
        raise HTTPException(
            422, detail=f"Provide no more than {MAX_EXAMPLE_QUESTIONS} questions for this paper."
        )

    try:
        return await upload_example_pairs(
            db,
            filename=file.filename,
            pdf_bytes=data,
            source_type=source_type,
            questions=questions,
            rationales=rationales,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, detail=str(exc)) from exc
