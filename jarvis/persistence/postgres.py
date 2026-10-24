"""Optional PostgreSQL repository using parameterized psycopg operations."""

from datetime import datetime, timezone
from time import monotonic
from typing import Callable
from uuid import UUID, uuid4

from jarvis.models.schemas import (
    CONVERSATION_LIST_DEFAULT_LIMIT,
    CONVERSATION_LIST_MAX_LIMIT,
    CONVERSATION_LIST_MAX_OFFSET,
    CONVERSATION_PREVIEW_MAX_LENGTH,
    ConversationListResponse,
    ConversationSummary,
)
from jarvis.persistence.base import (
    ConversationMessage, ConversationNotFound, MessageRole, PersistenceError,
)
from jarvis.persistence.operation_timeout import (
    bounded_connect_timeout,
    execute_with_deadline,
    operation_deadline,
)


class PostgresConversationRepository:
    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: int = 3,
        operation_timeout_seconds: int = 5,
        connect: Callable | None = None,
    ) -> None:
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL persistence requires the optional 'persistence' dependency."
                ) from exc
            connect = psycopg.connect
        self._database_url = database_url
        self._connect = connect
        self._connect_timeout_seconds = connect_timeout_seconds
        self._operation_timeout_seconds = operation_timeout_seconds

    def _open_connection(self, deadline: float):
        return self._connect(
            self._database_url,
            connect_timeout=bounded_connect_timeout(
                self._connect_timeout_seconds, deadline, clock=monotonic
            ),
        )

    def create_conversation(self) -> UUID:
        deadline = operation_deadline(self._operation_timeout_seconds, clock=monotonic)
        conversation_id = uuid4()
        try:
            with self._open_connection(deadline) as connection:
                execute_with_deadline(
                    connection, deadline,
                    "INSERT INTO jarvis_app.conversations (id) VALUES (%s)",
                    (conversation_id,),
                    clock=monotonic,
                )
        except Exception as exc:
            raise PersistenceError() from exc
        return conversation_id

    def append_message(
        self, conversation_id: UUID, role: MessageRole, content: str
    ) -> ConversationMessage:
        deadline = operation_deadline(self._operation_timeout_seconds, clock=monotonic)
        message_id = uuid4()
        try:
            message = ConversationMessage(
                id=message_id,
                conversation_id=conversation_id,
                sequence_number=0,
                role=role,
                content=content,
                created_at=datetime.now(timezone.utc),
            )
            with self._open_connection(deadline) as connection:
                conversation = execute_with_deadline(
                    connection, deadline,
                    "SELECT id FROM jarvis_app.conversations WHERE id = %s FOR UPDATE",
                    (conversation_id,),
                    clock=monotonic,
                ).fetchone()
                if conversation is None:
                    raise PersistenceError()
                sequence_number = execute_with_deadline(
                    connection, deadline,
                    """SELECT COALESCE(MAX(sequence_number), -1) + 1
                       FROM jarvis_app.messages WHERE conversation_id = %s""",
                    (conversation_id,),
                    clock=monotonic,
                ).fetchone()[0]
                row = execute_with_deadline(
                    connection, deadline,
                    """INSERT INTO jarvis_app.messages
                       (id, conversation_id, sequence_number, role, content)
                       VALUES (%s, %s, %s, %s, %s)
                       RETURNING id, conversation_id, sequence_number, role, content, created_at""",
                    (message.id, message.conversation_id, sequence_number, message.role, message.content),
                    clock=monotonic,
                ).fetchone()
            return ConversationMessage(
                id=row[0], conversation_id=row[1], sequence_number=row[2], role=row[3],
                content=row[4], created_at=row[5]
            )
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError() from exc

    def get_recent_messages(
        self, conversation_id: UUID, *, limit: int, deadline: float | None = None
    ) -> tuple[ConversationMessage, ...]:
        """Return the newest ``limit`` messages in chronological order for chat context.

        ``deadline`` is the caller's monotonic request deadline; the repository
        budget never extends it.
        """
        return self.get_messages(conversation_id, limit=limit, _outer_deadline=deadline)

    def get_messages(
        self, conversation_id: UUID, *, before_sequence: int | None = None, limit: int = 51,
        _outer_deadline: float | None = None,
    ) -> tuple[ConversationMessage, ...]:
        deadline = operation_deadline(
            self._operation_timeout_seconds, outer_deadline=_outer_deadline, clock=monotonic
        )
        try:
            with self._open_connection(deadline) as connection:
                exists = execute_with_deadline(
                    connection, deadline,
                    "SELECT id FROM jarvis_app.conversations WHERE id = %s", (conversation_id,),
                    clock=monotonic,
                ).fetchone()
                if exists is None:
                    raise ConversationNotFound()
                if before_sequence is None:
                    page_filter = "conversation_id = %s"
                    page_parameters = (conversation_id, limit)
                else:
                    page_filter = "conversation_id = %s AND sequence_number < %s"
                    page_parameters = (conversation_id, before_sequence, limit)
                rows = execute_with_deadline(
                    connection, deadline,
                    f"""SELECT id, conversation_id, sequence_number, role, content, created_at
                       FROM jarvis_app.messages
                       WHERE {page_filter}
                       ORDER BY sequence_number DESC, id DESC LIMIT %s""",
                    page_parameters,
                    clock=monotonic,
                ).fetchall()
            messages = tuple(
                ConversationMessage(
                    id=row[0], conversation_id=row[1], sequence_number=row[2], role=row[3],
                    content=row[4], created_at=row[5]
                )
                for row in reversed(rows)
            )
            return messages
        except ConversationNotFound:
            raise
        except Exception as exc:
            raise PersistenceError() from exc

    def list_conversations(
        self,
        *,
        limit: int = CONVERSATION_LIST_DEFAULT_LIMIT,
        offset: int = 0,
    ) -> ConversationListResponse:
        """Return a deterministic, bounded page without exposing full message text."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= CONVERSATION_LIST_MAX_LIMIT
        ):
            raise ValueError(
                f"limit must be between 1 and {CONVERSATION_LIST_MAX_LIMIT}."
            )
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= CONVERSATION_LIST_MAX_OFFSET
        ):
            raise ValueError(
                f"offset must be between 0 and {CONVERSATION_LIST_MAX_OFFSET}."
            )

        deadline = operation_deadline(self._operation_timeout_seconds, clock=monotonic)
        query = """SELECT c.id, c.created_at,
                          COALESCE(activity.last_activity_at, c.created_at) AS last_activity_at,
                          first_user.preview
                   FROM jarvis_app.conversations AS c
                   LEFT JOIN (
                       SELECT conversation_id, MAX(created_at) AS last_activity_at
                       FROM jarvis_app.messages
                       GROUP BY conversation_id
                   ) AS activity ON activity.conversation_id = c.id
                   LEFT JOIN LATERAL (
                       SELECT LEFT(m.content, 121) AS preview
                       FROM jarvis_app.messages AS m
                       WHERE m.conversation_id = c.id AND m.role = 'user'
                       ORDER BY m.sequence_number ASC, m.id ASC
                       LIMIT 1
                   ) AS first_user ON TRUE
                   ORDER BY last_activity_at DESC, c.id DESC
                   LIMIT %s OFFSET %s"""
        try:
            with self._open_connection(deadline) as connection:
                rows = execute_with_deadline(
                    connection,
                    deadline,
                    query,
                    (limit + 1, offset),
                    clock=monotonic,
                ).fetchall()

            has_more = len(rows) > limit
            items = []
            for row in rows[:limit]:
                preview = row[3]
                if preview is not None and len(preview) > CONVERSATION_PREVIEW_MAX_LENGTH:
                    preview = preview[: CONVERSATION_PREVIEW_MAX_LENGTH - 1] + "…"
                items.append(
                    ConversationSummary(
                        conversation_id=row[0],
                        created_at=row[1],
                        last_activity_at=row[2],
                        preview=preview,
                    )
                )
            return ConversationListResponse(
                items=items, limit=limit, offset=offset, has_more=has_more
            )
        except Exception as exc:
            raise PersistenceError() from exc

    def save_exchange(
        self, user_content: str, assistant_content: str, conversation_id: UUID | None = None
    ) -> UUID:
        """Store a new conversation or append an exchange atomically."""
        deadline = operation_deadline(self._operation_timeout_seconds, clock=monotonic)
        is_new = conversation_id is None
        conversation_id = conversation_id or uuid4()
        user_message_id, assistant_message_id = uuid4(), uuid4()
        try:
            # psycopg connection context commits on success and rolls back on exception.
            with self._open_connection(deadline) as connection:
                if is_new:
                    execute_with_deadline(
                        connection, deadline,
                        "INSERT INTO jarvis_app.conversations (id) VALUES (%s)",
                        (conversation_id,),
                        clock=monotonic,
                    )
                    first_sequence = 0
                else:
                    parent = execute_with_deadline(
                        connection, deadline,
                        "SELECT id FROM jarvis_app.conversations WHERE id = %s FOR UPDATE",
                        (conversation_id,),
                        clock=monotonic,
                    ).fetchone()
                    if parent is None:
                        raise ConversationNotFound()
                    first_sequence = execute_with_deadline(
                        connection, deadline,
                        """SELECT COALESCE(MAX(sequence_number), -1) + 1
                           FROM jarvis_app.messages WHERE conversation_id = %s""",
                        (conversation_id,),
                        clock=monotonic,
                    ).fetchone()[0]
                execute_with_deadline(
                    connection, deadline,
                    """INSERT INTO jarvis_app.messages
                       (id, conversation_id, sequence_number, role, content)
                       VALUES (%s, %s, %s, 'user', %s), (%s, %s, %s, 'assistant', %s)""",
                    (user_message_id, conversation_id, first_sequence, user_content,
                     assistant_message_id, conversation_id, first_sequence + 1, assistant_content),
                    clock=monotonic,
                )
        except ConversationNotFound:
            raise
        except Exception as exc:
            raise PersistenceError() from exc
        return conversation_id
