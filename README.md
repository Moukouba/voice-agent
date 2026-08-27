# Basic Multilingual Logistics Voice Agent (LiveKit Implementation)

A real-time, programmatic voice agent built using the **LiveKit Agents SDK**. This assistant handles customer shipment inquiries, validates tracking IDs in the required `FE-####` format, interacts asynchronously with a mock logistics API, and manages safety guardrails (such as refund escalation).

---

## ? Architecture & Tech Stack

* **Orchestration / WebRTC:** LiveKit Agents Framework (`livekit-agents`)
* **Speech-to-Text (STT):** Deepgram Nova-3 (`deepgram/nova-3-general`)
* **LLM Orchestration:** OpenAI GPT-4o (`openai/gpt-4o`)
* **Text-to-Speech (TTS):** Rime Arcana (`rime/arcana`)
* **Voice Activity Detection (VAD):** Silero VAD (`livekit-agents[silero]`)
* **Turn Detection:** LiveKit Cloud Inference Gateway (`inference.TurnDetector()`)
* **Mock API Integration:** Beeceptor (`httpx` HTTP client)
* **Environment & Package Management:** `uv`

---

## ? Prerequisites

Before running the agent locally, ensure you have the following installed and configured:

1. **Python 3.10+**
2. **`uv` Package Manager** (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
3. A **LiveKit Cloud Account** ([cloud.livekit.io](https://cloud.livekit.io))

---

## ? Environment Setup & Installation

### 1. Platform Credentials

1. Log into your [LiveKit Cloud Console](https://cloud.livekit.io).
2. Navigate to **Settings > API Keys** and generate a new set of credentials.
3. In the project root directory (`/m47-agent/livekit-voice-agent`), create a `.env` file and populate it with your credentials:

```
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your-api-key
LIVEKIT_API_SECRET=your-api-secret
TRACKING_API_URL=https://logistics-m47ai.free.beeceptor.com/track_shipment
```



### 2. Environment Initialization & Dependency Setup

Navigate to your project directory and initialize a bare Python environment using uv:

```
cd livekit-voice-agent
uv init --bare
```

Install the required dependencies:

```
uv add "livekit>=1.1.14" \
       "livekit-agents[silero]>=1.7.0" \
       "livekit-plugins-noise-cancellation>=0.3.0" \
       "python-dotenv>=1.2.3" \
       "httpx>=0.27.0"
```


Technical Dependency Notes:

- httpx: Strictly required (not optional). The track_shipment tool calls the Beeceptor mock API endpoint (https://logistics-m47ai.free.beeceptor.com/track_shipment) directly using httpx.
- livekit-agents[silero]: Required for local Voice Activity Detection (VAD).
- No turn-detector extra needed: This agent uses LiveKit's Cloud Inference Gateway (inference.TurnDetector()) rather than the local MultilingualModel plugin, eliminating the need to download heavy local turn-detection models.

### 3. CLI Installation & Model Download

Install the LiveKit CLI tool:


```
curl -sSL [https://get.livekit.io/cli](https://get.livekit.io/cli) | bash
```


Download the necessary local model files (Silero VAD):

```
uv run -m livekit.agents download-files
```

Running the Agent

Start the development agent server:

```
lk agent dev
```

Once initialized, follow the provided URL output in your CLI terminal and click "Start a Session" to interact with the voice agent in real time.
