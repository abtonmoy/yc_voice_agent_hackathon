"""Render bench_llm.json into a standalone graph page in the dashboard's design
language (light "paper" theme, single green accent, JetBrains Mono).

Two horizontal bar charts: throughput (tok/s, higher better) and total end-to-end
latency (ms, lower better). "Ours" (the Cloudflare endpoint) is drawn in the accent
colour; the others in ink. Data-driven — re-run bench_llm.py then this to refresh.

    uv run python bench_graph.py            # -> ../dashboard/bench.html
"""

from __future__ import annotations

import html
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "bench_llm.json")
ACC = os.path.join(HERE, "bench_accuracy.json")
CEK = os.path.join(HERE, "cekura_scores.json")  # Cekura LLM-judge scores per backend
OUT = os.path.join(HERE, "..", "dashboard", "bench.html")

# Which row is "ours" — highlighted in the accent colour.
OURS_MARK = "ours"


def _bars(rows, key, *, unit, lower_is_better):
    """One chart's worth of <div class=row> markup, scaled to the max value."""
    vals = [r[key] for r in rows]
    vmax = max(vals) if vals else 1
    best = min(vals) if lower_is_better else max(vals)
    out = []
    for r in rows:
        v = r[key]
        pct = (v / vmax * 100) if vmax else 0
        cls = "bar"
        if OURS_MARK in r["name"].lower():
            cls += " ours"
        if v == best:
            cls += " best"
        out.append(
            f'<div class="row">'
            f'<div class="lbl">{html.escape(r["name"])}</div>'
            f'<div class="track"><div class="{cls}" style="width:{pct:.1f}%"></div></div>'
            f'<div class="val">{v:,.0f}<span class="unit">{unit}</span></div>'
            f"</div>"
        )
    return "\n".join(out)


def _acc_bars(rows, acc):
    """Accuracy chart — transcript-derived rubric score (0-100) per backend, in the
    same row order as the latency charts. When a backend has no real measurement
    (`measured: false`) we render an empty bar labelled "not measured" rather than a
    misleading pass rate. (See bench_accuracy.json / bench_accuracy.py for why the
    old 8/8 numbers were invalid: every Cekura run had zero agent dialogue.)"""
    out = []
    measured_scores = [acc[r["name"]]["score"] for r in rows
                       if acc.get(r["name"]) and acc[r["name"]].get("measured")]
    best = max(measured_scores) if measured_scores else None
    for r in rows:
        a = acc.get(r["name"])
        if not a:
            continue
        cls = "bar"
        if OURS_MARK in r["name"].lower():
            cls += " ours"
        if a.get("measured") and a.get("score") is not None:
            score = a["score"]
            pct = max(0.0, min(100.0, float(score)))
            if best is not None and score == best:
                cls += " best"
            val = f'{score:.0f}<span class="unit">/100</span>'
        else:
            pct = 0.0
            cls += " unmeasured"
            val = '<span class="unit">not measured</span>'
        out.append(
            f'<div class="row">'
            f'<div class="lbl">{html.escape(r["name"])}</div>'
            f'<div class="track"><div class="{cls}" style="width:{pct:.1f}%"></div></div>'
            f'<div class="val">{val}</div>'
            f"</div>"
        )
    return "\n".join(out)


def _cekura_bars(rows, scores, key, *, unit="%"):
    """0-100 bar per backend from cekura_scores.json[name][key], same row order and
    style as the latency charts. Higher is better; "ours" in accent, best in ink."""
    vals = {r["name"]: (scores.get(r["name"], {}) or {}).get(key) for r in rows}
    present = [v for v in vals.values() if isinstance(v, (int, float))]
    best = max(present) if present else None
    out = []
    for r in rows:
        v = vals.get(r["name"])
        cls = "bar"
        if OURS_MARK in r["name"].lower():
            cls += " ours"
        if isinstance(v, (int, float)):
            pct = max(0.0, min(100.0, float(v)))
            if best is not None and v == best:
                cls += " best"
            val = f'{v:.0f}<span class="unit">{unit}</span>'
        else:
            pct = 0.0
            cls += " unmeasured"
            val = '<span class="unit">not measured</span>'
        out.append(
            f'<div class="row"><div class="lbl">{html.escape(r["name"])}</div>'
            f'<div class="track"><div class="{cls}" style="width:{pct:.1f}%"></div></div>'
            f'<div class="val">{val}</div></div>'
        )
    return "\n".join(out)


def main() -> None:
    with open(SRC) as f:
        data = json.load(f)
    rows = [r for r in data if r.get("ok")]
    if not rows:
        raise SystemExit("no successful backends in bench_llm.json")

    cek = {}
    if os.path.exists(CEK):
        cek = json.load(open(CEK))

    # Accuracy + safety: Cekura LLM-judge (0-100). Throughput/e2e from the latency bench.
    accuracy = _cekura_bars(rows, cek, "score")
    gates = _cekura_bars(rows, cek, "gate_score")
    n_note = next((f'{c.get("n_measured")}/{c.get("n_total")} scenarios scored'
                   for c in cek.values() if c.get("n_total")), "")
    acc_hint = (f"Cekura LLM judge vs each scenario's expected outcome &middot; "
                f"{n_note} &middot; higher is better")
    tput = _bars(rows, "tok_s_median", unit=" tok/s", lower_is_better=False)
    e2e = _bars(rows, "total_ms_median", unit=" ms", lower_is_better=True)
    runs = rows[0].get("runs", "?")

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM backend benchmark</title>
<style>
  :root {{
    --paper:#fcfcfc; --ink:#141414; --ink-dim:#6b6b6b; --ink-faint:#e6e6e6;
    --accent:#1a7f37; --accent-soft:rgba(26,127,55,.14);
    --mono:ui-monospace,"JetBrains Mono","SF Mono",monospace;
  }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; background:var(--paper); color:var(--ink);
         font-family:var(--mono); -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:760px; margin:0 auto; padding:48px 28px 64px }}
  h1 {{ font-size:19px; font-weight:600; margin:0 0 4px; letter-spacing:-.2px }}
  .sub {{ color:var(--ink-dim); font-size:12.5px; margin:0 0 36px; line-height:1.5 }}
  .chart {{ margin:0 0 40px }}
  .chart h2 {{ font-size:13px; font-weight:600; margin:0 0 2px }}
  .chart .hint {{ color:var(--ink-dim); font-size:11px; margin:0 0 16px }}
  .row {{ display:flex; align-items:center; gap:14px; margin:9px 0 }}
  .lbl {{ flex:0 0 200px; font-size:12px; color:var(--ink); text-align:right }}
  .track {{ flex:1 1 auto; height:22px; background:var(--ink-faint);
            border-radius:3px; overflow:hidden }}
  .bar {{ height:100%; background:var(--ink-dim); border-radius:3px;
          transition:width .5s ease }}
  .bar.ours {{ background:var(--accent) }}
  .bar.unmeasured {{ background:repeating-linear-gradient(45deg,
      var(--ink-faint),var(--ink-faint) 6px,#dcdcdc 6px,#dcdcdc 12px) }}
  .bar.best {{ box-shadow:none }}
  .bar:not(.ours).best {{ background:var(--ink) }}
  .val {{ flex:0 0 110px; font-size:12.5px; font-variant-numeric:tabular-nums;
          text-align:left; font-weight:600 }}
  .unit {{ color:var(--ink-dim); font-weight:400; font-size:11px }}
  .legend {{ display:flex; gap:20px; margin-top:30px; font-size:11px; color:var(--ink-dim) }}
  .legend i {{ display:inline-block; width:11px; height:11px; border-radius:2px;
               margin-right:6px; vertical-align:-1px }}
  .legend .ours {{ background:var(--accent) }}
  .legend .other {{ background:var(--ink-dim) }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>LLM backend benchmark</h1>
    <p class="sub">Identical test case (one triage briefing turn, max&nbsp;160&nbsp;tokens,
       streamed) &middot; median of {runs} timed runs each.<br>
       Cloudflare/QUIC endpoint is <b>ours</b>; AWS&nbsp;ALB is the provider's.</p>

    <div class="chart">
      <h2>Accuracy</h2>
      <p class="hint">{acc_hint}</p>
      {accuracy}
    </div>

    <div class="chart">
      <h2>Safety gates</h2>
      <p class="hint">decline / vague-approval / overclaim &middot; never apply a fix without a clear verbal yes &middot; higher is better</p>
      {gates}
    </div>

    <div class="chart">
      <h2>Throughput</h2>
      <p class="hint">tokens / second &middot; higher is better</p>
      {tput}
    </div>

    <div class="chart">
      <h2>Total end-to-end latency</h2>
      <p class="hint">request &rarr; full 160-token response &middot; lower is better</p>
      {e2e}
    </div>

    <div class="legend">
      <span><i class="ours"></i>ours (Cloudflare)</span>
      <span><i class="other"></i>other backends</span>
    </div>
  </div>
</body>
</html>
"""
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {os.path.normpath(OUT)} ({len(rows)} backends)")


if __name__ == "__main__":
    main()
