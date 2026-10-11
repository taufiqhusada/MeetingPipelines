You are merging several pre-meeting briefs (from different teammates, repos, or runs) into one
team-level context for the upcoming meeting.

Each brief is JSON with headline, progress, commitments, gaps, action_items, open_questions. Evidence
IDs are prefixed with the brief label, e.g. "alice:GH-PR-12".

Do:
- Merge duplicates (same PR/issue/commitment mentioned by several briefs) into one entry, keeping all evidence.
- Surface cross-person dependencies and conflicts (A waits on B; two people doing the same thing;
  contradicting statuses) under `dependencies_and_conflicts`.
- Keep owners. Never invent facts or IDs; only use evidence IDs present in the input.

Also produce the Slack view the team actually reads, in exactly this shape:
- `team_goal`: ONE sentence: what the team is trying to achieve right now (from specs, milestones, goals).
- `people`: one entry per teammate.
  - A brief labelled with a teammate's worker id (e.g. "taufik") describes that person's own coding
    sessions, so its work belongs to them unless an owner says otherwise. Work in the "shared" brief
    belongs to whoever is named as owner/author.
  - Use these display names, merging aliases into one person: {people}
  - progress = finished since last meeting; wip = in progress now (prefix "Blocked: " if stuck);
    next = their next step before/at the meeting.
  - At most 3 items per bucket; each one short line (max ~15 words), concrete (PR #, file, feature),
    no filler. Leave a bucket empty rather than padding it.
- `meeting_goals`: what this meeting must decide, max 4, each phrased as a decision
  ("Decide who owns brief aggregation", "Pick Fireflies vs Recall"). Include unowned work and conflicts here.

Return ONLY a JSON object with the same shape as the inputs plus these extra keys:
{
  "team_goal": "...",
  "people": [{"name": "Taufiq",
              "progress": [{"text": "...", "evidence": ["label:ID"]}],
              "wip":      [{"text": "...", "evidence": []}],
              "next":     [{"text": "...", "evidence": []}]}],
  "meeting_goals": [{"text": "Decide ...", "evidence": []}],
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
