"""Deterministic demo provider; it does not perform language-model reasoning."""


class MockProvider:
    name = "mock"
    is_demo = True

    async def generate_reply(self, message: str, *, system_instruction: str | None = None) -> str:
        # The mock stays deterministic; it accepts the contract without implying
        # that it follows model instructions or performs model reasoning.
        del system_instruction
        return (
            "This is a JARVIS demo response, not an AI-generated answer. "
            f"I received: {message}"
        )
