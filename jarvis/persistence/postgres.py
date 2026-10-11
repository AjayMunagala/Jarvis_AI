"""Optional PostgreSQL repository using parameterized psycopg operations."""

from datetime import datetime, timezone
from typing import Callable
from uuid import UUID, uuid4

from jarvis.persistence.base import ConversationMessage, MessageRole, PersistenceError


class PostgresConversationRepository:
    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: int = 3,
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

    def _open_connection(self):
        return self._connect(
            self._database_url,
            connect_timeout=self._connect_timeout_seconds,
        )

    def create_conversation(self) -> UUID:
        conversation_id = uuid4()
        try:
            with self._open_connection() as connection:
                connection.execute(
                    "INSERT INTO jarvis_app.conversations (id) VALUES (%s)",
                    (conversation_id,),
                )
        except Exception as exc:
            raise PersistenceError() from exc
        return conversation_id

    def append_message(
        self, conversation_id: UUID, role: MessageRole, content: str
    ) -> ConversationMessage:
        message_id = uuid4()
        try:
            message = ConversationMessage(
                id=message_id,
                conversation_id=conversation_id,
                role=role,
                content=content,
                created_at=datetime.now(timezone.utc),
            )
            with self._open_connection() as connection:
                conversation = connection.execute(
                    "SELECT id FROM jarvis_app.conversations WHERE id = %s FOR UPDATE",
                    (conversation_id,),
                ).fetchone()
                if conversation is None:
                    raise PersistenceError()
                sequence_number = connection.execute(
                    """SELECT COALESCE(MAX(sequence_number), -1) + 1
                       FROM jarvis_app.messages WHERE conversation_id = %s""",
                    (conversation_id,),
                ).fetchone()[0]
                row = connection.execute(
                    """INSERT INTO jarvis_app.messages
                       (id, conversation_id, sequence_number, role, content)
                       VALUES (%s, %s, %s, %s, %s)
                       RETURNING id, conversation_id, role, content, created_at""",
                    (message.id, message.conversation_id, sequence_number, message.role, message.content),
                ).fetchone()
            return ConversationMessage(
                id=row[0], conversation_id=row[1], role=row[2], content=row[3], created_at=row[4]
            )
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError() from exc

    def get_messages(self, conversation_id: UUID) -> tuple[ConversationMessage, ...]:
        try:
            with self._open_connection() as connection:
                rows = connection.execute(
                    """SELECT id, conversation_id, role, content, created_at
                       FROM jarvis_app.messages WHERE conversation_id = %s
                       ORDER BY sequence_number, id""",
                    (conversation_id,),
                ).fetchall()
            return tuple(
                ConversationMessage(
                    id=row[0], conversation_id=row[1], role=row[2], content=row[3], created_at=row[4]
                )
                for row in rows
            )
        except Exception as exc:
            raise PersistenceError() from exc

    def save_exchange(self, user_content: str, assistant_content: str) -> UUID:
        """Store the conversation and both messages in one transaction."""
        conversation_id = uuid4()
        user_message_id, assistant_message_id = uuid4(), uuid4()
        try:
            # psycopg connection context commits on success and rolls back on exception.
            with self._open_connection() as connection:
                connection.execute(
                    "INSERT INTO jarvis_app.conversations (id) VALUES (%s)", (conversation_id,)
                )
                connection.execute(
                    """INSERT INTO jarvis_app.messages
                       (id, conversation_id, sequence_number, role, content)
                       VALUES (%s, %s, 0, 'user', %s), (%s, %s, 1, 'assistant', %s)""",
                    (user_message_id, conversation_id, user_content,
                     assistant_message_id, conversation_id, assistant_content),
                )
        except Exception as exc:
            raise PersistenceError() from exc
        return conversation_id
