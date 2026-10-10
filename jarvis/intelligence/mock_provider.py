"""Deterministic demo provider; it does not perform language-model reasoning."""


class MockProvider:
    name = "mock"
    is_demo = True

    async def generate_reply(self, message: str) -> str:
        return (
            "This is a JARVIS demo response, not an AI-generated answer. "
            f"I received: {message}"
        )
