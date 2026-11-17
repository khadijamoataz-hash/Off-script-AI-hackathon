# The committed demo run, step by step

This page walks through the run whose files are in `outputs/` and whose log is `decision_log.json`. Every file there was written by the code during that run on 17 November 2026 (times below are UTC, from the log); nothing was edited by hand. Project `PRJ-479A6665`, model `qwen3.5:9b` on a laptop, cost $0.00.

**In one line:** the brief was screened, researched with fetched sources, challenged three times by the critic, given a demand target before the test, and stopped when real demand came in at 0 of 55.

## 1. Brief and screening (12:37)

The founder typed: "digital ai academy to help cs students gets into job market". The intake agent confirmed it is a digital product (not physical goods, finance, health or legal advice) and the guardrails cleared the brief. Log: `intake / screen_brief`.

## 2. Discover, step 1: opportunities (12:37 to 12:57)

File: [`outputs/phase1_step1_opportunities.md`](../outputs/phase1_step1_opportunities.md)

- The researcher sharpened the idea and rated it against two alternatives on the same criteria.
- Claims are a table: each one is **Sourced** only if the page was fetched in the run and the quote is found in it, otherwise `ASSUMPTION`. Pages that refused automated readers are listed as skipped, not hidden.
- The critic gave the strongest case, the risks, kill criteria and the cheapest test that could disprove the idea, and recommended **REPEAT** with medium confidence: the problem is real, but there is no evidence anyone would pay.
- The founder chose repeat at step 1 (12:45), continued (12:50), then chose repeat again at the end of Discover (12:52) and continued once more (12:57). The critic recommended REPEAT in all three research rounds. Each system recommendation is logged with `approved_by: null` and each founder choice with `approved_by: "founder"`.

## 3. Discover, step 2: interview script (12:57)

File: [`outputs/phase1_step2_interview_script.md`](../outputs/phase1_step2_interview_script.md)

A Mom Test script: questions about what people did in the past, and a list of questions not to ask (no pitching, no "would you buy").

The historical script also contains a hypothetical question that the validator at the time missed. Current schema checks reject that pattern and request a repaired script before exporting it. The committed demo output is preserved as generated, not corrected by hand; these checks have been verified with scripted tests, not a new local-model demo run.

## 4. Demand target fixed before the test (12:59)

Before Validate started, the founder fixed the target: **55 waitlist sign-ups from people in the target market**. It is written to the log (`set_evidence_target`, `fix_demand_target`) and cannot be lowered afterwards.

File: [`outputs/phase1_gate_decision.md`](../outputs/phase1_gate_decision.md)

## 5. Validate: Lean Canvas, landing page, invite post (13:00 to 13:07)

Files: [`outputs/phase2_step1_lean_canvas.md`](../outputs/phase2_step1_lean_canvas.md), [`outputs/phase2_step2_landing_page.html`](../outputs/phase2_step2_landing_page.html)

- Each canvas box is a claim: 1 of 9 is backed by a fetched source, 8 are labelled `ASSUMPTION`. The assumptions are what the demand test has to check.
- The landing page is rendered by code: it has an empty slot `[Real customer quote - collect after launch]` and a `[YOUR-FORM-URL]` placeholder, and no testimonials, counters, prices or countdowns.
- The page and an invite post went to the approval queue. The founder approved both on the approval screen. Approval exported the exact text as copy-ready files, each with the content hash and a single-use approval token: [`phase2_step2_approved_deploy_act-0c629491.txt`](../outputs/phase2_step2_approved_deploy_act-0c629491.txt), [`phase2_step2_approved_post_act-2940e2ac.txt`](../outputs/phase2_step2_approved_post_act-2940e2ac.txt). Foundry posted nothing.

## 6. Real results and the STOP recommendation (13:07 to 13:08)

File: [`outputs/phase2_gate_decision.md`](../outputs/phase2_gate_decision.md)

- The founder recorded the real result: **0** sign-ups, confirming these are real people only. (The results line in that file reads "0 0" because the founder typed 0 in the "of what" field; that field now requires a word.)
- 0 is below half of 55, so the system recommended **STOP** and stated the numbers. Continue was not available: a project whose evidence gate has failed cannot be advanced.
- The founder chose stop. Log: `validate_gate`, outcome `stop`, `approved_by: "founder"`.

## 7. Delivery test (MOCKED)

File: [`outputs/payment_test_log.md`](../outputs/payment_test_log.md)

A simulated order checks that delivery happens once per order id and that a refund revokes access. It is labelled `MOCKED` and states that no payment took place. Foundry has no payment code.

## What the run did not reach

Commercial setup and Launch open only after Validate passes, so this run has no Phase 4 or Phase 5 drafts. Both steps are built and covered by tests (`tests/test_workflow_discover.py`, `tests/test_app_gates.py`). Build and Operate are `STUBBED`.

## Checking it yourself

```bash
python scripts/verify_decision_log.py     # 46 entries, required keys and the hash chain
```

Each entry in `decision_log.json` carries `prev_hash` and `entry_hash`, so a changed or removed entry breaks the chain.
