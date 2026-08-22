import logging
import re
import httpx
from typing import AsyncIterable
from dataclasses import dataclass
from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    ModelSettings,
    RoomOutputOptions,
    cli,
    stt,
    inference,
    llm,
)
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from livekit import rtc

from system_prompt import SYSTEM_PROMPT

logger = logging.getLogger("multilingual-logistics-agent")
load_dotenv()

# Default configuration constants
DEFAULT_LANGUAGE = "eng"
DEFAULT_TTS_MODEL = "arcana"
DEFAULT_VOICE = "seraphina"

TRACKING_API_URL = "https://logistics-m47ai.free.beeceptor.com/track_shipment"


@dataclass
class LanguageConfig:
    """Configuration for TTS settings per language."""
    lang: str
    model: str = DEFAULT_TTS_MODEL


@llm.function_tool
async def track_shipment(order_id: str) -> str:
    """
    Track a shipment by its Order ID.
    The Order ID format is 'FE-' followed by exactly 4 digits (0-9).
    """
    # Defensive Engineering: case-insensitive validation before calling API
    if not re.match(r'^fe-\d{4}$', order_id, re.IGNORECASE):
        return "Invalid Order ID format. Please provide an ID in the format FE-XXXX (e.g., FE-2026)."

    # Normalize to uppercase to ensure exact match with FE-2026
    normalized_id = order_id.upper()

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                    TRACKING_API_URL,
                    json={"order_id": order_id},
            )
            
            # Log the raw response for debugging
            logger.info(f"API Response for {normalized_id}: {response.status_code} - {response.text}")
            
            if response.status_code == 200:
                # Integration Logic: parse the response to extract status, location, and ETA
                data = response.json()
                status = data.get("status", "Unknown")
                location = data.get("location", "Unknown")
                eta = data.get("eta", "Unknown")
                return f"Shipment found. Status: {status}, Location: {location}, ETA: {eta}."
            else:
                # Unhappy path: 404 or other non-200 status
                return "Order not found. Please double-check the ID and try again."
    except Exception as e:
        logger.error(f"API Error tracking shipment {normalized_id}: {e}")
        return "An error occurred while contacting the logistics service. Please try again later."


class MultilingualLogisticsAgent(Agent):
    """A multilingual voice agent for logistics tracking."""
    
    # TTS config per language.
    LANGUAGE_CONFIGS = {
        "eng": LanguageConfig(lang="eng"),
        "fra": LanguageConfig(lang="fra"),
        "spa": LanguageConfig(lang="spa"),
    }

    LANGUAGE_DISPLAY_NAMES = {
        "eng": "English",
        "fra": "French",
        "spa": "Spanish",
    }

    STT_TO_RIME = {
        "en": "eng",
        "fr": "fra",
        "es": "spa",
    }

    SUPPORTED_LANGUAGES = list(LANGUAGE_CONFIGS.keys())

    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT, tools=[track_shipment])
        self._current_language = DEFAULT_LANGUAGE
        self._room: rtc.Room | None = None

    async def stt_node(
        self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings
    ) -> AsyncIterable[stt.SpeechEvent]:
        """
        Override STT node to detect language and update TTS configuration dynamically.
        """
        default_stt = super().stt_node(audio, model_settings)

        async for event in default_stt:
            if self._is_transcript_event(event):
                await self._handle_language_detection(event)
            yield event

    def _is_transcript_event(self, event: stt.SpeechEvent) -> bool:
        """Check if event is a transcript event with language information."""
        return (
            event.type
            in [
                stt.SpeechEventType.INTERIM_TRANSCRIPT,
                stt.SpeechEventType.FINAL_TRANSCRIPT,
            ]
            and event.alternatives
        )

    async def _handle_language_detection(self, event: stt.SpeechEvent) -> None:
        """Update TTS from STT-detected language and sync to frontend."""
        detected_language = event.alternatives[0].language
        if not detected_language:
            return
        effective_language = self._update_tts_for_language(detected_language)
        if effective_language != self._current_language:
            self._current_language = effective_language
            await self._publish_language_update(effective_language)

    def _update_tts_for_language(self, language: str) -> str:
        """Update TTS configuration based on detected language."""
        base = language.split("-")[0].lower() if language else ""
        rime_lang = self.STT_TO_RIME.get(base, base) if base else DEFAULT_LANGUAGE
        effective_lang = rime_lang if rime_lang in self.LANGUAGE_CONFIGS else DEFAULT_LANGUAGE
        config = self.LANGUAGE_CONFIGS.get(effective_lang, self.LANGUAGE_CONFIGS[DEFAULT_LANGUAGE])
        logger.info(f"Updating TTS: detected={language} -> rime={effective_lang}")
        
        # Include voice=DEFAULT_VOICE to prevent the TTS from randomly 
        # changing voices when the language option is updated.
        self.session.tts.update_options(
            model=f"rime/{config.model}",
            language=config.lang,
            voice=DEFAULT_VOICE, 
        )
        return effective_lang

    async def _publish_language_update(self, language_code: str) -> None:
        """Sync current language to the frontend via participant attributes."""
        if not self._room:
            return
        try:
            display_name = self.LANGUAGE_DISPLAY_NAMES.get(language_code, "English")
            await self._room.local_participant.set_attributes({"current_language": display_name})
        except Exception as e:
            logger.warning("Failed to publish language update: %s", e)

    async def on_enter(self) -> None:
        """Called when the agent session starts. Generate initial greeting."""
        await self._publish_language_update(self._current_language)
        self.session.generate_reply(
            instructions="Greet the user and introduce yourself as a professional logistics assistant. Ask how you can help them today."
        )


def prewarm(proc: JobProcess) -> None:
    """Preload VAD model for faster startup."""
    proc.userdata["vad"] = silero.VAD.load()


server = AgentServer()
server.setup_fnc = prewarm


@server.rtc_session(agent_name="multilingual-logistics-agent")
async def entrypoint(ctx: JobContext) -> None:
    """Main entry point for the multilingual logistics agent worker."""
    ctx.log_context_fields = {"room": ctx.room.name}

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=inference.STT(model="deepgram/nova-3-general", language="multi"),
        llm=inference.LLM(model="openai/gpt-4o"),
        tts=inference.TTS(
            model=f"rime/{DEFAULT_TTS_MODEL}", voice=DEFAULT_VOICE, language=DEFAULT_LANGUAGE
        ),
        turn_detection=MultilingualModel(),
    )

    async def log_usage() -> None:
        """Log usage summary on shutdown."""
        for usage in session.usage.model_usage:
            logger.info(f"Usage: {usage.provider}/{usage.model}: {usage}")

    ctx.add_shutdown_callback(log_usage)

    agent = MultilingualLogisticsAgent()
    agent._room = ctx.room
    await session.start(
        agent=agent,
        room=ctx.room,
        room_output_options=RoomOutputOptions(transcription_enabled=True),
    )


if __name__ == "__main__":
    cli.run_app(server)