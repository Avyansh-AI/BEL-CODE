# Local AI Setup Guide (Ollama & Self-Hosted Gateways)

Running local models is **100% free**, requires **no API keys or accounts**, works **offline**, and keeps **100% of your source code on your computer**.

---

## 1. Install Ollama

Download and install Ollama for Windows, macOS, or Linux:

- Official website: <https://ollama.com/download>

Start Ollama (the desktop app starts it automatically; on Linux or CLI run):

```bash
ollama serve
```

Verify Ollama is listening:

```bash
curl http://localhost:11434/api/tags
```

---

## 2. Recommended Coding Models by Hardware

Choose a model that fits your system memory (VRAM on GPU, Unified Memory on Apple Silicon, or System RAM):

| Available RAM / VRAM | Recommended Ollama Model | Command |
| :--- | :--- | :--- |
| **8 GB** | `qwen2.5-coder:7b` or `llama3.2` | `ollama pull qwen2.5-coder:7b` |
| **16 GB** | `qwen2.5-coder:14b` or `deepseek-r1:7b` | `ollama pull qwen2.5-coder:14b` |
| **24 GB+** | `qwen2.5-coder:32b` | `ollama pull qwen2.5-coder:32b` |

*(If you run into out-of-memory errors or slow CPU swapping, step down to a smaller `:7b` or `:3b` quantized tag.)*

---

## 3. Connect Ollama in BEL CODE

1. Open BEL CODE (`http://127.0.0.1:8765`).
2. Click **Use a Local AI Model** on the Welcome screen (or **Connect Your AI -> Local AI (Ollama)**).
3. Leave the API Key blank (**not required**).
4. Keep Base URL as `http://localhost:11434/v1` (or set `OLLAMA_HOST` if Ollama runs on another port or LAN host).
5. Pick your pulled model (for example `qwen2.5-coder:7b`), click **Save & Test Connection**, and then click **Finish & Start Coding**.

If Ollama is not running yet, **Test Connection** gives clear diagnostic instructions to start `ollama serve` and pull a model.

---

## 4. Other Local Servers (LM Studio, vLLM, llama.cpp, FreeLLMAPI)

- **LM Studio / vLLM / `llama.cpp` server:** Use **Connect Your Own API** (`OpenAI-compatible`), enter `http://localhost:<port>/v1` (e.g., `http://localhost:1234/v1` for LM Studio) and your loaded model ID.
- **FreeLLMAPI Local Router (`freellm`):** If you run a local FreeLLMAPI router on `http://localhost:31415/v1` (or legacy port `3001`), configure it in Settings or `FREELLM_API_KEY` inside `.env`.
