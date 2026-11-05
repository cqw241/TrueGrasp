"""Transcribe images; student confirmation is required before grading."""

from pydantic import BaseModel, Field

from app.contracts.llm import ChatMessage, ImagePart, ImageURL, LLMClient, TextPart


class Transcription(BaseModel):
    latex: str
    doubts: list[str] = Field(description="Uncertain symbols or regions needing confirmation")
    requires_confirmation: bool = True


async def transcribe_answer(llm: LLMClient, images: list[str]) -> Transcription:
    if not images:
        raise ValueError("At least one image is required")
    result = await llm.generate(
        [
            ChatMessage(
                role="user",
                content=[
                    TextPart(
                        text=(
                            "Transcribe the student's answer into LaTeX. "
                            "Do not solve or correct it. "
                            "List all uncertain symbols or regions in doubts. "
                            "Student confirmation is required before grading."
                        )
                    ),
                    *(ImagePart(image_url=ImageURL(url=url)) for url in images),
                ],
            )
        ],
        Transcription,
    )
    result.requires_confirmation = True
    return result
