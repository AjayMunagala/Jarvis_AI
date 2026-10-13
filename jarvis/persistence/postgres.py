"""Optional PostgreSQL repository using parameterized psycopg operations."""

from datetime import datetime, timezone
from time import monotonic
from typing import Callable
from uuid import UUID, uuid4

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

    def get_messages(
        self, conversation_id: UUID, *, before_sequence: int | None = None, limit: int = 51
    ) -> tuple[ConversationMessage, ...]:
        deadline = operation_deadline(self._operation_timeout_seconds, clock=monotonic)
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
