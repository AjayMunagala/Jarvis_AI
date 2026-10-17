"""Deterministic API-level workflow coverage using the existing app services."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.intelligence.mock_provider import MockProvider
from jarvis.main import create_app
from jarvis.memory.models import MemoryPage, MemoryRecord
from jarvis.persistence.base import ConversationMessage, ConversationNotFound


OWNER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_OWNER_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TOKEN = "e2e-workflow-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
CONVERSATION_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


class InMemoryMemoryRepository:
    def __init__(self):
        self.records: dict[UUID, tuple[UUID, MemoryRecord]] = {}
        self.create_calls = 0

    def create(self, owner_id, content, category=None, source_reference=None):
        self.create_calls += 1
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = MemoryRecord(
            id=UUID(int=self.create_calls), content=content, category=category,
            source_reference=source_reference, created_at=now, updated_at=now,
        )
        self.records[record.id] = (owner_id, record)
        return record

    def create_document(self, owner_id, content, filename):
        for record_owner, record in self.records.values():
            if record_owner == owner_id and record.source_kind == "document" and record.content == content:
                return record, False
        record = self.create(owner_id, content, None, filename).model_copy(update={"source_kind": "document"})
        self.records[record.id] = (owner_id, record)
        return record, True

    def search(self, owner_id, query, *, limit=10, offset=0):
        terms = query.casefold().split()
        matches = [
            record for record_owner, record in self.records.values()
            if record_owner == owner_id
            and all(term in record.content.casefold() for term in terms)
        ]
        selected = matches[offset:offset + limit]
        return MemoryPage(
            items=tuple(selected), total=len(matches), limit=limit, offset=offset,
            has_more=offset + limit < len(matches),
        )

    def search_with_deadline(self, owner_id, query, *, deadline, limit=10, offset=0):
        del deadline
        return self.search(owner_id, query, limit=limit, offset=offset)


class InMemoryConversationRepository:
    def __init__(self):
        self.messages: dict[UUID, list[ConversationMessage]] = {}

    def save_exchange(self, user_content, assistant_content, conversation_id=None):
        conversation_id = conversation_id or CONVERSATION_ID
        if conversation_id != CONVERSATION_ID:
            raise ConversationNotFound()
        messages = self.messages.setdefault(conversation_id, [])
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for role, content in (("user", user_content), ("assistant", assistant_content)):
            messages.append(ConversationMessage(
                id=UUID(int=len(messages) + 1), conversation_id=conversation_id,
                sequence_number=len(messages), role=role, content=content, created_at=now,
            ))
        return conversation_id

    def get_messages(self, conversation_id, *, before_sequence=None, limit=51):
        if conversation_id not in self.messages:
            raise ConversationNotFound()
        messages = self.messages[conversation_id]
        if before_sequence is not None:
            messages = [item for item in messages if item.sequence_number < before_sequence]
        return tuple(messages[:limit])


class RecordingMockProvider(MockProvider):
    def __init__(self):
        self.inputs = []

    async def generate_reply(self, message, *, system_instruction=None, response_model=None):
        self.inputs.append((message, system_instruction))
        return await super().generate_reply(
            message, system_instruction=system_instruction, response_model=response_model
        )


def test_authenticated_document_to_memory_search_chat_and_history_workflow():
    settings = Settings(
        _env_file=None,
        environment="test",
        persistence_enabled=True,
        persistence_database_url=SecretStr("postgresql://local/jarvis-test"),
        local_auth_token=SecretStr(TOKEN),
        owner_id=OWNER_ID,
        memory_retrieval_enabled=True,
        memory_retrieval_limit=5,
    )
    provider = RecordingMockProvider()
    app = create_app(settings=settings, provider=provider)
    assert app.state.orchestration_service.tool_registry.config.tools == ()
    assert app.state.orchestration_service.tool_registry.execution_enabled is False
    memories = InMemoryMemoryRepository()
    conversations = InMemoryConversationRepository()
    app.state.memory_repository = memories
    app.state.conversation_repository = conversations

    # A foreign-owner record intentionally has the same lexical terms.
    foreign = memories.create(
        OTHER_OWNER_ID,
        "The lighthouse plan uses the nebula vault key. FOREIGN_OWNER_MARKER",
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    document = b"The lighthouse plan uses the nebula vault key."

    with TestClient(app, headers=headers) as client:
        imported = client.post(
            "/memories/ingest",
            content=document,
            headers={"X-Document-Filename": r"C:\\private\\lighthouse-notes.md",
                     "Content-Type": "application/octet-stream"},
        )
        assert imported.status_code == 201
        imported_memory_id = UUID(imported.json()["id"])
        stored_owner, stored_record = memories.records[imported_memory_id]
        assert stored_owner == OWNER_ID
        assert stored_record.source_reference == "lighthouse-notes.md"
        assert stored_record.content == document.decode("utf-8")
        assert memories.create_calls == 2  # one foreign fixture record, one import

        search = client.post("/memories/search", json={"query": "lighthouse nebula"})
        assert search.status_code == 200
        assert [item["id"] for item in search.json()["items"]] == [str(imported_memory_id)]
        assert "FOREIGN_OWNER_MARKER" not in search.text
        assert foreign.id != imported_memory_id

        chat = client.post("/chat", json={"message": "lighthouse nebula"})
        assert chat.status_code == 200
        assert chat.json()["memory_context"] == {
            "outcome": "success", "memory_ids": [str(imported_memory_id)]
        }
        assert len(provider.inputs) == 1
        provider_message, provider_instruction = provider.inputs[0]
        assert "The lighthouse plan uses the nebula vault key." in provider_message
        assert "FOREIGN_OWNER_MARKER" not in provider_message
        assert "untrusted reference data" in provider_instruction
        assert chat.json()["conversation_id"] == str(CONVERSATION_ID)

        history = client.get(f"/conversations/{CONVERSATION_ID}/messages")
        assert history.status_code == 200
        history_messages = history.json()["messages"]
        assert [item["sequence_number"] for item in history_messages] == [0, 1]
        assert [item["role"] for item in history_messages] == ["user", "assistant"]
        assert history_messages[0]["content"] == "lighthouse nebula"
        assert history_messages[1]["content"] == chat.json()["reply"]

        # Invalid import fails before repository creation, leaving the valid
        # memory and conversation exchange intact.
        failed_import = client.post(
            "/memories/ingest",
            content=b"should not be stored",
            headers={"X-Document-Filename": "notes.exe",
                     "Content-Type": "application/octet-stream"},
        )

    assert failed_import.status_code == 422
    assert failed_import.json() == {
        "error": "http_error", "detail": "Only .txt and .md documents are supported."
    }
    assert memories.create_calls == 2
    assert set(conversations.messages) == {CONVERSATION_ID}
    assert len(conversations.messages[CONVERSATION_ID]) == 2
