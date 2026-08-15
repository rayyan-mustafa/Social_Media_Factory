# Cursor Build Prompt: Hook Generator Agent (Netflix/True-Crime Style)

Paste this into Cursor to implement:

---

Build a Python module called `hook_generator.py` that generates Netflix-documentary-style opening hooks from a video transcript/script, using an LLM API call with a heavily few-shot-loaded prompt (not just instructions — models copy patterns from examples far better than rules).

## Requirements

### 1. Input
A function `generate_hooks(transcript: str, topic: str, num_variants: int = 3) -> list[dict]` where:
- `transcript`: full script/transcript of the source video
- `topic`: short topic label (e.g. "Anne Boleyn's execution")
- `num_variants`: how many hook options to generate for A/B picking

### 2. The core system prompt (use this exact structure — do not simplify it)

```
You are a documentary hook writer trained in the style of Netflix true-crime and history documentaries (Making a Murderer, The Crown, Bad Blood, Dirty Money).

RULES — violating any of these makes the hook unusable:
1. NEVER start chronologically. Open on the most emotionally charged or shocking moment, even if it happens at the END of the story.
2. NEVER use these banned phrases: "today we discuss," "in this video," "let's explore," "let's talk about," "welcome back," "in this episode."
3. ALWAYS end the hook on an unresolved question or tension — the viewer must feel something is incomplete.
4. Prefer withholding a name/identity over stating it upfront, when it creates curiosity.
5. Use short, punchy sentences. Maximum 4 sentences per hook. No sentence over 20 words.

STRUCTURE TO FOLLOW:
Sentence 1: The most shocking/emotional moment from the story (not the start of the timeline).
Sentence 2: A consequence or stakes statement that raises the emotional temperature.
Sentence 3: A question OR a false-assumption reversal ("Everyone believes X. They're wrong.").
Sentence 4 (optional): A one-line bridge into the story, WITHOUT resolving the tension.

FEW-SHOT EXAMPLES (study the pattern, don't copy content):

Example 1 (execution/betrayal pattern):
"In 1536, she waited in a room in the Tower of London, certain she'd be pardoned. She wasn't. Six years earlier, the same man who signed her death warrant had broken from Rome just to marry her. So what actually changed?"

Example 2 (identity withheld pattern):
"Nobody suspected the quiet lady-in-waiting. That was exactly the point. By the time the court realized what she'd done, it was already too late. This is the story historians almost missed entirely."

Example 3 (false assumption pattern):
"Everyone assumes the king wanted her dead from the start. He didn't. For years, he protected her from the very people who eventually convinced him to sign the order. The real villain wasn't who you think."

Example 4 (countdown/ticking clock pattern):
"She had nineteen days left, and she had no idea. While she planned her future, the men around her were already building a case to end it. History remembers the execution. It rarely asks who built the trap."

Now generate {num_variants} new hooks for the topic: "{topic}"
Base the factual content strictly on this transcript, do not invent facts: {transcript}

Output as JSON list: [{{"hook": "...", "pattern_used": "execution/betrayal | identity_withheld | false_assumption | ticking_clock | other"}}]
```

### 3. Function implementation
- Call your existing LLM API (Claude/GPT — use whatever client is already configured in the pipeline)
- Inject `num_variants`, `topic`, and `transcript` into the prompt above
- Parse the JSON response into a list of dicts
- If JSON parsing fails, retry once with an explicit "output ONLY valid JSON, no markdown fences" instruction appended

### 4. Quality filter (post-generation, rules-based, no extra API call)
Before returning hooks, run each through a simple validator:
- Reject any hook containing banned phrases (case-insensitive match against the banned list)
- Reject any hook where the longest sentence exceeds 25 words
- Reject any hook under 2 sentences or over 5 sentences
- Log rejected hooks with reason to `hook_rejects.jsonl` for later prompt-tuning reference (if your agent keeps failing the same way, this log tells you why)

### 5. Output
```python
[
  {
    "hook": str,
    "pattern_used": str,
    "passed_validation": bool,
    "rejection_reason": str | None
  },
  ...
]
```

### 6. Integration point
Call this right after transcript generation (Stage 1 of the pipeline), before branching into newsletter/ebook/tutorial formats — the hook is specific to the VIDEO/narrative format, so it should feed into the video script and Shorts/clip titling, not the newsletter or ebook (those need their own hook style, which can be a separate future module).

### 7. Test with real data
- Run it once against 2-3 of your actual past video transcripts
- Manually rate the 3 variants each time (1-5) and log ratings to `hook_ratings.jsonl`
- After ~10 logged ratings, review which `pattern_used` types score highest for YOUR specific audience — feed that insight back into reordering which patterns get generated first

---

Use whatever LLM client library is already in your pipeline (Claude API or OpenAI API — match existing setup). Keep temperature relatively high (0.8-0.9) for this specific call since hook generation benefits from creative variance, unlike factual transcript work which should stay lower temperature.