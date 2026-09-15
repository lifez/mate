---
name: ahoy
description: Recap visible Mate session events since the previous real human message, surface visibly unanswered decisions, and guide the user through them one at a time. Use only when explicitly invoked with /skill:ahoy.
---

# Ahoy

Give a concise session recap without gathering fresh state, except for the first-message fallback below.

1. Inspect the visible conversation before the current invocation. Treat an ordinary user-role message as human-authored unless it is clearly runtime-generated. Exclude messages beginning `MATE EVENT (` or `MATE WATCHER (`, Mate runtime attachments, extension/custom messages, and the `/stow` maintenance prompt beginning `Stow this Mate conversation now.` Never infer human authorship merely from a synthetic user role.
2. Find the most recent real human message before this invocation. The current invocation is outside the recap interval; an earlier `/skill:ahoy` invocation is a real human boundary.
3. If no prior real human message is visible, call `mate_status` once and give a bounded current-state digest under **Captain’s Call**, **Recently Landed**, **Underway**, and **Charted Next**. Mention snapshot limits. Do not read reports, mutate state, acknowledge events, dispatch work, or open the Lavish board.
4. Otherwise, use only visible session history. Do not call tools or read files. Recap concrete outcomes, failures, decisions made, decisions needed, and running work that appeared after the boundary. Preserve full PR URLs. Do not claim current live state beyond the last visible event.
5. Inspect all visible history before this invocation for explicit human decisions that remain unanswered, including ones raised before the recap boundary. A later unrelated human message does not close a decision. Close one only when a later visible response directly resolves, chooses, declines, grants, or denies it. Deduplicate by substance.
6. If compaction hid the exact boundary, say so and summarize only visible evidence. Treat an older decision as open only when both its request and unanswered status remain visible.
7. If the interval has no events but an older open decision exists, report that decision. If neither exists, say in one sentence that nothing happened after the previous human message.
8. After the recap, if decisions remain, present only the one you judge most impactful. Say the ordering is your judgment and include: the decision, why it matters, options, and your recommendation. When the user answers, present the next decision from the existing inventory, one at a time, until none remain.

Do not treat recap, recommendation, or a user answer as task approval, event acknowledgement, completion, cancellation, dispatch authority, scope expansion, push, merge, deploy, or cleanup permission. Existing Mate human-only gates remain unchanged.
