#!/usr/bin/env python3
"""
Fit scorer — applies scoring.json's 4-dimension rubric to unscored items
via the Anthropic API. Judgment comes from the model; arithmetic happens here.

Behaviour:
  * no ANTHROPIC_API_KEY -> prints a notice and exits 0 (collection still works)
  * scores items where needs_scoring is true, up to scoring_batch_cap per run
  * temperature 0; strict JSON contract; one retry; then the item is flagged
    needs_review (never silently guessed) and listed in data/new_items.md
  * RESCORE_LEGACY=true -> also scores items that carry only a legacy score
    (the acceptance test: compare new ranking against legacy hand scores)
"""

import json, os, re, sys
from pathlib import Path

DATA = Path("data")
JSON_F = DATA / "opportunities.json"
NEW_F = DATA / "new_items.md"

def main():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("scorer: ANTHROPIC_API_KEY not set — skipping (add it as a repo secret to enable)")
        return

    import anthropic
    client = anthropic.Anthropic(api_key=key)

    rubric = json.loads(Path("scoring.json").read_text())
    cfg = json.loads(Path("sources.json").read_text())
    cap = int(cfg.get("auto_modules", {}).get("scoring_batch_cap", 25))
    weights = {d["key"]: d["weight"] for d in rubric["dimensions"]}
    dim_keys = list(weights)

    blob = json.loads(JSON_F.read_text())
    rescore = os.environ.get("RESCORE_LEGACY", "").lower() == "true"
    targets = [it for it in blob["items"]
               if it.get("needs_scoring") or (rescore and it.get("score") is None
                                              and (it.get("legacy") or {}).get("score") is not None)]
    targets = targets[:cap]
    if not targets:
        print("scorer: nothing to score")
        return

    anchors = "\n".join(
        f'- "{a["title"]}" -> {json.dumps(a["target"])} ({a["why"]})'
        for a in rubric["anchors"])

    flagged, scored = [], 0
    for it in targets:
        item_desc = json.dumps({k: it.get(k) for k in
                    ("title","issuer","funder","country","source","deadline","value","notes")},
                    ensure_ascii=False)
        prompt = (
            f"You score funding/tender opportunities for this organization:\n{rubric['organization']}\n\n"
            f"Score each dimension 0-100:\n"
            + "\n".join(f"- {d['key']} (weight {d['weight']}): {d['question']}" for d in rubric["dimensions"])
            + f"\n\nEligibility flag: {rubric['eligible_flag_rules']}\n\n"
            f"Calibration anchors (title -> target dimension scores):\n{anchors}\n\n"
            f"Opportunity to score:\n{item_desc}\n\n"
            f"{rubric['output_contract']}")

        result = None
        for attempt in (1, 2):
            try:
                msg = client.messages.create(
                    model="claude-sonnet-4-6", max_tokens=500, temperature=0,
                    messages=[{"role": "user", "content": prompt}])
                text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
                text = re.sub(r"```(json)?|```", "", text).strip()
                cand = json.loads(text)
                assert all(isinstance(cand.get(k), int) and 0 <= cand[k] <= 100 for k in dim_keys)
                assert cand.get("eligible") in ("Y", "N", "?")
                assert all(isinstance(cand["rationales"].get(k), str) for k in dim_keys)
                result = cand
                break
            except Exception as ex:
                print(f"[score] attempt {attempt} failed for '{it['title'][:60]}': {ex}", file=sys.stderr)

        if result is None:
            it["needs_review"] = True
            it["needs_scoring"] = False
            flagged.append(it["title"][:120])
            continue

        it["dims"] = {k: result[k] for k in dim_keys}
        it["score"] = round(sum(result[k] * weights[k] for k in dim_keys), 1)  # arithmetic in code
        it["eligible"] = result["eligible"]
        it["rationales"] = result["rationales"]
        it["needs_scoring"] = False
        it.pop("needs_review", None)
        scored += 1

    JSON_F.write_text(json.dumps(blob, indent=1))

    if flagged:
        note = "\n\n## Scoring flags — needs human review\n" + "\n".join(f"- {t}" for t in flagged)
        NEW_F.write_text((NEW_F.read_text() if NEW_F.exists() else "") + note)

    print(f"scorer: {scored} scored · {len(flagged)} flagged · "
          f"{sum(1 for i in blob['items'] if i.get('needs_scoring'))} remaining")

if __name__ == "__main__":
    main()
