# PROMPTS.md

A running log of the prompts, reasoning, and outcomes behind changes to this
project (Rock Paper Scissors — Arduino UNO Q). Newest entries appended at the
bottom.

---

## Session 1 — Add LLM trash-talk after each round

**Date:** 2026-07-22

### User prompt

> Learn about this project — it's the classical rock paper scissors. I would
> like to add an extra step using the LLM brick
> (https://github.com/arduino/app-bricks-py/tree/main/src/arduino/app_bricks/llm).
> After a single game, if the human wins, the Arduino should use the LLM to
> share excuses for why it lost. If it wins, I want to read the Arduino teasing
> the human in a funny way.

An example of the LLM brick usage was provided (chat-with-tools + streaming,
`LargeLanguageModel(max_tokens=512, tools=[...])`, `llm.chat_stream(prompt)`).

The user asked for a plan and clarifying questions before any coding.

### Thinking / investigation notes

- **Explored the project structure:**
  - `app.yaml` — declares the `arduino:video_object_detection` brick, port 5001.
  - `python/main.py` — Flask app. A thread-safe `GameState` runs the countdown,
    snapshots the detected gesture, computes a `winner`
    (`human` / `arduino` / `draw` / `no_detection`), records history, then
    returns to `idle`. The web UI polls `/api/state` every 500 ms.
  - `python/templates/index.html` — polls state; shows detection panel, Arduino
    move, a result banner, and a history table.
  - `python/requirements.txt` — only `flask` (bricks are provided by App Lab, not
    pip-installed — consistent with `video_object_detection` not being listed).

- **Confirmed the LLM brick API** by reading the brick source
  (`local_llm.py`) rather than trusting the example alone:
  ```python
  LargeLanguageModel(
      system_prompt="",      # persona / constraints
      temperature=0.7,
      max_tokens=512,
      timeout=None,
      tools=None,
      model=None,            # falls back to app/brick config
  )
  llm.chat(message) -> str            # blocking, strips <think>…</think>
  llm.chat_stream(message) -> Iterator # streaming
  ```
  Key finding: the persona is passed via `system_prompt`, and `chat()` blocks
  and returns the whole string — good enough for a polling UI.

- **Consulted the `arduino-unoq-applab` skill** for App Lab conventions:
  bricks are declared in `app.yaml`; use graceful `ImportError` fallback so the
  code still runs off-device.

### Clarifying questions asked (and answers)

| Question | Answer chosen |
|----------|---------------|
| How to deliver the excuse/taunt? | **Text on screen only** (no TTS) |
| Which outcomes trigger a comment? | **All outcomes** (win, loss, draw, no-detection) |
| How should the text appear? | **Appear all at once** (blocking `chat()` + "thinking…" indicator) |

### Plan presented

1. `app.yaml` — add `arduino:llm: {}` to bricks.
2. `main.py` — init `LargeLanguageModel` with a cheeky persona (graceful
   fallback); add `llm_comment` / `llm_state` to `GameState`; after a round
   resolves, generate a per-outcome line in the existing round thread.
3. `index.html` — add an "Arduino says…" speech bubble with a thinking
   indicator, then the generated line.

### Changes made

**`app.yaml`**
- Added `arduino:llm: {}` alongside the existing object-detection brick.

**`python/main.py`**
- Initialized `LargeLanguageModel` with a "cheeky, competitive Arduino" persona
  (`system_prompt`, `max_tokens=80`, `temperature=0.9`), wrapped in try/except
  `ImportError` so it no-ops off-device (mirrors the detector pattern).
- `GameState` gained `llm_comment` and `llm_state` fields — reset on each new
  round and in `reset()`, exposed in `to_dict()` as `llmComment` / `llmState`.
- Added `_generate_comment(winner, human_move, arduino_move)`, called after the
  result is recorded (inside the existing round thread):
  - `human` wins → dramatic **excuse** for losing
  - `arduino` wins → playful **taunt**
  - `draw` → witty tie remark
  - `no_detection` → "show your hand properly" nudge
  - Sets `llm_state='thinking'`, calls blocking `chat()`, stores line with
    `llm_state='ready'`; on failure logs and reverts to `idle`.
- Added an `[MODE] LLM: yes/no` line to the startup banner.

**`python/templates/index.html`**
- Added `.llm-bubble` speech-bubble styling (with a CSS tail and animated
  blinking dots for the thinking state).
- Added an "Arduino says" bubble element under the result banner.
- Added `updateLlm(state, comment)` to the poll loop: dots while `thinking`,
  full line when `ready`, hidden otherwise.
- Hidden the bubble immediately on new round (`playRound`) and on `resetGame`.

### Outcome / status

- Code complete and internally consistent.
- **Not runnable on this machine:** no Python installed locally, and the
  `llm` / `video_object_detection` bricks only exist on the UNO Q. Must be
  deployed to the board to test live (watch `[LLM]` lines in the app logs).
- **On-device caveat:** the local LLM takes a few seconds per line, so the
  result screen holds longer while it "thinks" — expected with the blocking
  approach the user chose. The LLM brick also needs a model downloaded locally
  on the board.
