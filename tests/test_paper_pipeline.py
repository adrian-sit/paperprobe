import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import get_db_session
from app.main import app
from app.schemas.extraction import GeneratedQuestion, QuestionGeneration
from app.services import paper_pipeline


def test_standard_api_and_example_library_remain_available():
    paths = app.openapi()['paths']
    assert '/api/v1/papers/{paper_id}/questions' in paths
    assert '/api/v1/papers/{paper_id}/questions/progress' in paths
    assert '/api/v1/examples/upload' in paths
    assert all('agent' not in path for path in paths)
    app.dependency_overrides[get_db_session] = lambda: None
    try:
        async def request():
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                return await client.post(
                    f'/api/v1/papers/{uuid4()}/questions', json={'count': 11}
                )
        response = asyncio.run(request())
    finally:
        app.dependency_overrides.pop(get_db_session, None)
    assert response.status_code == 422


def test_generation_merges_latest_fields_and_persists_validated_questions(monkeypatch):
    paper_id, version_id = uuid4(), uuid4()
    fields = [
        SimpleNamespace(field_type='methods', value={'items': ['A', 'B']}),
        SimpleNamespace(field_type='methods', value={'items': ['B', 'C']}),
    ]
    version = SimpleNamespace(
        id=version_id, is_latest=True, version_key='v2', title='Current title',
        abstract='Current abstract', extracted_fields=fields,
    )
    paper = SimpleNamespace(id=paper_id, title='Old title', versions=[version])
    monkeypatch.setattr(paper_pipeline, '_load', AsyncMock(return_value=paper))
    calls = []

    def generate(title, abstract, extracted_fields, count):
        calls.append((title, abstract, extracted_fields, count))
        return QuestionGeneration(questions=[
            GeneratedQuestion(question=f'Question {i}', focus='evidence', rationale='Grounded')
            for i in range(count)
        ])

    monkeypatch.setattr(paper_pipeline, 'generate_questions_from_context', generate)
    saved = []

    async def refresh(question):
        question.id = uuid4()
        question.created_at = datetime.now(UTC)

    db = SimpleNamespace(add=saved.append, commit=AsyncMock(), refresh=AsyncMock(side_effect=refresh))
    result = asyncio.run(paper_pipeline.generate_and_store_questions(db, paper_id, 3))
    assert calls == [('Current title', 'Current abstract', {'methods': {'items': ['A', 'B', 'C']}}, 3)]
    assert len(result.questions) == 3
    assert all(q.paper_version_id == version_id and q.source == 'gemini' and q.status == 'draft' for q in saved)
    db.commit.assert_awaited_once()


def test_missing_paper_does_not_generate_or_commit(monkeypatch):
    monkeypatch.setattr(paper_pipeline, '_load', AsyncMock(return_value=None))
    generate = AsyncMock()
    monkeypatch.setattr(paper_pipeline, 'generate_questions_from_context', generate)
    db = SimpleNamespace(commit=AsyncMock())
    with pytest.raises(ValueError, match='Paper not found'):
        asyncio.run(paper_pipeline.generate_and_store_questions(db, uuid4(), 3))
    generate.assert_not_called()
    db.commit.assert_not_awaited()
