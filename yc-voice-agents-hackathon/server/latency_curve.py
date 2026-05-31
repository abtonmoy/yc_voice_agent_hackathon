"""Latency curve + component waterfall, parsed from the deployed agent's logs
(Pipecat metrics + Pipecat Cloud observability). No tunnel needed.

Usage:
  pc cloud agent logs flower-bot -n 2000 | uv run --no-project python latency_curve.py
"""
import re
import sys

text = sys.stdin.read()

# Per-turn voice-to-voice (user stops -> bot's first speech).
v2v = [float(x) * 1000 for x in re.findall(r"first_bot_speech_latency[^\n]*\n?[^\n]*?latency=([0-9.]+)s", text)]
if not v2v:
    v2v = [float(x) * 1000 for x in re.findall(r"latency=([0-9.]+)s", text)]

# Component breakdown (per turn).
llm_ttfb = [float(x) * 1000 for x in re.findall(r"AnthropicLLMService#0: TTFB ([0-9.]+)s", text)]
tts_agg = [float(x) * 1000 for x in re.findall(r"text aggregation ([0-9.]+)s", text)]
tts_ttfb = [float(x) * 1000 for x in re.findall(r"GradiumTTSService#0: TTFB ([0-9.]+)s", text)]


def stats(xs):
    return (int(sum(xs) / len(xs)), int(min(xs)), int(max(xs))) if xs else (0, 0, 0)


print("=== VOICE-TO-VOICE LATENCY CURVE (per agent turn, ms) ===")
for n, ms in enumerate(v2v, 1):
    bar = "#" * max(1, int(ms) // 80)
    print(f"  turn {n:>2}: {int(ms):>5} ms  {bar}")
a, mn, mx = stats(v2v)
print(f"  V2V: avg {a} ms | min {int(mn)} | max {int(mx)} | turns {len(v2v)}\n")

print("=== COMPONENT WATERFALL (avg ms across turns) ===")
for label, xs in (("LLM TTFB (Claude)", llm_ttfb),
                  ("TTS text-aggregation", tts_agg),
                  ("TTS TTFB (Gradium)", tts_ttfb)):
    a, mn, mx = stats(xs)
    bar = "#" * max(1, a // 30)
    print(f"  {label:<22} avg {a:>4} ms (min {int(mn)}, max {int(mx)})  {bar}")
