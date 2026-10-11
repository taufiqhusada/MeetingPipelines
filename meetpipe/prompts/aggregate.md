You are merging several pre-meeting briefs (from different teammates, repos, or runs) into one
team-level context for the upcoming meeting.

Each brief is JSON with headline, progress, commitments, gaps, action_items, open_questions. Evidence
IDs are prefixed with the brief label, e.g. "alice:GH-PR-12".

Do:
- Merge duplicates (same PR/issue/commitment mentioned by several briefs) into one entry, keeping all evidence.
- Surface cross-person dependencies and conflicts (A waits on B; two people doing the same thing;
  contradicting statuses) under `dependencies_and_conflicts`.
- Keep owners. Never invent facts or IDs; only use evidence IDs present in the input.

Return ONLY a JSON object with the same shape as the inputs plus one extra key:
{
  "headline": "...",
  "progress": [{"summary": "...", "evidence": ["label:ID"]}],
  "commitments": [{"what": "...", "owner": "...", "due": "...", "made_in": "label:ID", "status": "...", "evidence": []}],
  "gaps": [{"description": "...", "why_it_matters": "...", "evidence": []}],
  "action_items": [{"what": "...", "owner": "...", "priority": "high|medium|low", "evidence": []}],
  "open_questions": [{"question": "...", "evidence": []}],
  "dependencies_and_conflicts": [{"description": "...", "evidence": []}],
  "coverage_notes": "..."
}

=== BRIEFS START ===
{briefs}
=== BRIEFS END ===
