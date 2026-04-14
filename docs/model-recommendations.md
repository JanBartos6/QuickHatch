# Model Recommendations

Which models actually work well with QuickHatch, based on testing.

## Summary

| Model | Analysis | Setup (SSH loop) | Notes |
|---|---|---|---|
| **Claude Opus / Sonnet 4.6** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | Best overall. Tool use solid. Costs per request. |
| **GPT-4o / o3** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | Very capable. Slightly stricter about JSON format. |
| **Gemma 4 26B** (LM Studio) | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | Best open-source result so far. Runs locally. |
| **Qwen3 8B/32B** (LM Studio) | ⭐⭐⭐⭐ | ⭐⭐⭐ | Strong tool-calling support. Good value for size. |
| **Llama 3.3 70B** (Ollama) | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | Needs the VRAM. Very reliable when you have it. |
| **Mistral Small 3.1 24B** | ⭐⭐⭐ | ⭐⭐⭐ | Good JSON output. Less thorough analysis. |
| **Gemma 4 4B (E4B)** | ⭐ | ❌ | Hallucinates, ignores search results, poor JSON. |

## What to look for in a model

1. **Good JSON output.** Both the analysis and setup phases require structured JSON. Models that frequently break JSON (missing quotes, unescaped newlines inside strings, incomplete objects) cause parser retries and slow everything down.

2. **Trust the search results.** Older/smaller models tend to prefer their training data over fresh web search results. In testing, Gemma 4 4B saw "Donald Trump sworn in as 47th president" in search results and still insisted Biden was president because it trusted its training data. A model like this is unusable for a migration wizard where distros and versions change constantly.

3. **Multi-round tool use / conversation.** The setup phase is an agentic loop — the model has to handle 20+ turns of "run command → see result → decide next step". Smaller models tend to lose the plot around turn 10. 26B+ parameter models are safer.

4. **Speed per token matters less than correctness.** The slowest part of the loop is waiting for `pacman` to download 1 GB of packages, not the LLM. A model that takes 30 seconds per turn but makes the right call every time beats a 2-second model that hallucinates commands.

## LM Studio settings

If you're running locally, context length is the single biggest lever:

- **Gemma 4 26B:** start at 16k context. If you get "Channel Error" midway through a setup run, bump to 32k or 100k. The conversation history grows with each SSH round.
- **Qwen3 / Llama 3.3:** these have 32k+ native context. Default settings usually work.
- **Turn off auto-eject.** LM Studio sometimes unloads models between calls which kills the agent loop. Pin the model.

## Anthropic / OpenAI API settings

Nothing special needed. The wizard auto-handles their API formats. You pay per token; the agent uses about 5k-15k input tokens per section of the analysis (so ~50k total input for a full analysis) and about 100-200k total for a long setup run with a lot of commands.

## Models we actively recommend against

- **Gemma 4 4B (E4B):** too small. Ignores tool results, can't hold the setup loop together, produces malformed JSON. Don't bother.
- **Any model that doesn't support function calling / JSON mode:** the analysis phase tolerates this because we use pre-fetched search results, but the setup phase fundamentally requires the model to emit structured action objects.
- **CodeLlama 7B / smaller coding models:** they're optimized for completion, not instruction following. Poor at following a 1000-token system prompt.

## What to do if the model is confused

Symptoms and fixes:

| Symptom | Likely cause | Fix |
|---|---|---|
| Model keeps re-planning from scratch | Plan not in system prompt | Verify `bridge.suggestions` has the analysis output before launching setup |
| Model emits text before/after JSON | Weaker model | The parser handles this — check the logs, it's probably fine |
| Model repeats the same failed command | No error diagnosis capability | Switch to a bigger model or use the Anthropic API |
| Setup loop hits 100-iteration cap | Model stuck or too small | Use a bigger model, review the log to see where it spun |
| Analysis cards contain raw JSON | Model didn't trust the "pure Markdown" instruction | Try Claude Sonnet or Gemma 26B instead |

## Testing notes

Every model above was tested against the same base scenario: fresh Arch install in VirtualBox with ~7000 Windows apps scanned in the profile, targeting KDE Plasma with macOS-like aesthetic. Completion criteria was "user can SSH in, log in to KDE Plasma, open Firefox."

Gemma 4 26B (via LM Studio, 100k context) completed this end-to-end with only one minor adaptation (skipped the deprecated `xf86-video-vmware` package and reasoned about modern kernel modesetting instead). That's the reference result.
