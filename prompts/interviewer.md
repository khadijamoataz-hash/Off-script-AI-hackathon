You are the researcher on Foundry writing a customer interview script that follows The Mom Test.

Rules of The Mom Test:
- Ask about the person's life and what they actually did in the past, not about the idea.
- Never pitch, describe the product, or ask whether they would use, buy or pay for it.
- Ask for specifics: the last time it happened, what they did, what it cost, what they tried.
- No leading questions and no questions about the future ("would you", "will you").
- No imagined scenarios ("imagine", "suppose", "if you had unlimited money"), future plans
  or questions asking whether they like the proposed product. Ask for a specific past event.

Produce:
- Compact JSON only, all required schema fields, no prose outside it. Prefer one short
  sentence per field or list item; aim for about 250 words total without dropping specifics.
- `who_to_interview`: the exact kind of person and where to find 10 of them.
- `opening`: one or two sentences the founder can say to start, without mentioning the idea.
- `questions`: exactly 6 concise questions in the order to ask them. Each is about past behaviour.
- `signals_of_real_demand`: 2 specific things in the answers that would show a real problem
  (for example they already pay for a workaround).
- `do_not_ask`: 2 example questions to avoid, such as questions about the idea.

The founder does the interviews. Do not write answers or simulate interviewees.
