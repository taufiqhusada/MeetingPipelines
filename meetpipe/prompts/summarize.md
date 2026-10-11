You are preparing a pre-meeting brief so the team doesn't have to dig for context themselves.

Below is everything gathered about the project: coding-agent sessions (what people actually worked on
with Claude Code / Codex), GitHub PRs/issues/commits, design docs and specs from Drive, and transcripts
or AI notes from previous meetings. Every item has a citable ID in square brackets, e.g. [GH-PR-12].

Produce a brief with:
1. progress — what has actually been done / is in flight. Ground it in sessions, PRs, commits.
2. commitments — promises made in earlier meetings (who will do what, by when). For each, check the
   other sources and set status: "done" (clear evidence it shipped), "in_progress" (partial evidence),
   "no_evidence" (nothing found), or "unclear".
3. gaps — things that should exist but don't: commitments with no evidence, spec/design requirements
   with no implementation, open review feedback, failing tests or blockers seen in sessions, open
   questions nobody answered.
4. action_items — concrete next steps for the upcoming meeting, with a suggested owner if inferable.
5. open_questions — decisions the meeting should make.

Rules:
- Cite evidence IDs for every entry, using only IDs that appear in the context. Never invent IDs,
  people, dates, or facts. If a source is missing or thin, say so in `coverage_notes`.
- Be concise and specific (file names, PR numbers, names). No filler.
- Owners: use names/handles found in the context; otherwise null.
- If upcoming meeting is given, prioritise what is relevant to it.

Return ONLY a JSON object with exactly this shape:
{
  "headline": "one or two sentences: where the project stands",
  "progress": [{"summary": "...", "evidence": ["ID", ...]}],
  "commitments": [{"what": "...", "owner": "name or null", "due": "date/text or null",
                   "made_in": "ID of meeting item", "status": "done|in_progress|no_evidence|unclear",
                   "evidence": ["ID", ...]}],
  "gaps": [{"description": "...", "why_it_matters": "...", "evidence": ["ID", ...]}],
  "action_items": [{"what": "...", "owner": "name or null", "priority": "high|medium|low",
                    "evidence": ["ID", ...]}],
  "open_questions": [{"question": "...", "evidence": ["ID", ...]}],
  "coverage_notes": "which sources had data, which were empty/missing"
}

=== CONTEXT START ===
{context}
=== CONTEXT END ===
