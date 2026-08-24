import logging
import os
import re
import time
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
    AgentStateChangedEvent,
    MetricsCollectedEvent,
    metrics,
)
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from livekit import rtc

from system_prompt import SYSTEM_PROMPT

os.environ["TRANSFORMERS_VERBOSITY"] = "error"

logger = logging.getLogger("multilingual-logistics-agent")
load_dotenv()

# Default configuration constants
DEFAULT_LANGUAGE = "eng"
DEFAULT_TTS_MODEL = "arcana"
DEFAULT_VOICE = "seraphina"

TRACKING_API_URL = os.getenv("TRACKING_API_URL")

# Strict order-id format: 'FE-' followed by exactly 4 digits, nothing else.
ORDER_ID_PATTERN = re.compile(r"FE-\d{4}", re.IGNORECASE)

# A single AsyncClient is created once and reused across calls (connection pooling)
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Lazily create and reuse a single AsyncClient for the process lifetime."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    return _http_client


async def close_http_client() -> None:
    """Close the shared client on worker shutdown."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


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
    cleaned = order_id.strip()

    # Defensive Engineering: validate independently of whatever the LLM normalized upstream. 
    if not ORDER_ID_PATTERN.fullmatch(cleaned):
        return "Invalid Order ID format. Please provide an ID in the format FE-XXXX (e.g., FE-2026)."

    # Normalize to uppercase and actually USE it for the request. 
    normalized_id = cleaned.upper()

    client = get_http_client()
    try:
        response = await client.post(
            TRACKING_API_URL,
            json={"order_id": normalized_id},
        )

        logger.info(f"API response for {normalized_id}: {response.status_code} - {response.text}")

        if response.status_code == 200:
            data = response.json()
            status = data.get("status", "Unknown")
            location = data.get("location", "Unknown")
            eta = data.get("eta", "Unknown")
            return f"Shipment found. Status: {status}, Location: {location}, ETA: {eta}."

        if response.status_code == 404:
            return "Order not found. Please double-check the ID and try again."

        # Any other non-200 (500, 429, etc.) is a system-side problem, not a missing shipment. 
        logger.warning(f"Unexpected status tracking {normalized_id}: {response.status_code}")
        return "SYSTEM_ERROR: The tracking service returned an unexpected response. Please try again or offer a human agent."

    except httpx.TimeoutException:
        logger.error(f"Timeout tracking shipment {normalized_id}")
        return "SYSTEM_ERROR: The tracking service timed out. Please try again or offer a human agent."
    except Exception as e:
        logger.error(f"API error tracking shipment {normalized_id}: {e}")
        return "SYSTEM_ERROR: An error occurred while contacting the logistics service. Please try again or offer a human agent."


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
        # Restrict to FINAL_TRANSCRIPT to prevent async queue bottlenecks
        return (
            event.type == stt.SpeechEventType.FINAL_TRANSCRIPT
            and event.alternatives
        )

    async def _handle_language_detection(self, event: stt.SpeechEvent) -> None:
        """Update TTS from STT-detected language and sync to frontend."""
        detected_language = event.alternatives[0].language
        if not detected_language:
            return

        base_lang = detected_language.split("-")[0].lower()

        # Guard clause: ignore any language not strictly in our supported mapping (en, fr, es). 
        if base_lang not in self.STT_TO_RIME:
            logger.info(
                f"Unsupported language detected ({detected_language}); "
                f"TTS remains at {self._current_language}"
            )
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

    # --- Metrics Tracking Logic ---
    usage_collector = metrics.UsageCollector()
    last_eou_metrics: metrics.EOUMetrics | None = None

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        nonlocal last_eou_metrics
        # Capture EOU metrics for TTFA calculation
        if ev.metrics.type == "eou_metrics":
            last_eou_metrics = ev.metrics

        # Log each metric as it arrives and add to usage collector
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    @session.on("agent_state_changed")
    def _on_agent_state_changed(ev: AgentStateChangedEvent):
        if ev.new_state == "speaking":
            if last_eou_metrics:
                # Calculate time since user finished speaking
                elapsed = time.time() - last_eou_metrics.timestamp
                logger.info(f"Time to first audio: {elapsed:.3f}s")

    async def log_usage() -> None:
        """Log usage summary on shutdown."""
        summary = usage_collector.get_summary()
        logger.info("Usage summary: %s", summary)
        for usage in session.usage.model_usage:
            logger.info(f"Usage: {usage.provider}/{usage.model}: {usage}")

    ctx.add_shutdown_callback(log_usage)
    ctx.add_shutdown_callback(close_http_client)
    # -------------------------------

    agent = MultilingualLogisticsAgent()
    agent._room = ctx.room
    await session.start(
        agent=agent,
        room=ctx.room,
        room_output_options=RoomOutputOptions(transcription_enabled=True),
    )


if __name__ == "__main__":
    cli.run_app(server)
