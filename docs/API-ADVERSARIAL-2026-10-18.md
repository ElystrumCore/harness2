# Claude API adversarial verification — 2026-10-18

The owner requested direct Claude API testing after the initial integration merge. One bounded `claude-haiku-5-5` call generated 12 synthetic JSON fixtures: six request bodies, four OpenCode event streams and two candidate-file maps. Only protocol rules and synthetic examples were sent. Model-generated JSON was inspected as data, not executed as tests or shell code; the rejection expectations were reviewed before becoming regressions.

Eight cases were already rejected. Four exposed concrete gaps:

| Fixture | Gap | Repair |
|---|---|---|
| `req-tool-use-in-user` | user messages could carry tool-use history | tool blocks are now role-specific |
| `req-tool-result-no-id` | results without a tool-use ID were admitted | require a nonempty identity and typed error flag |
| `oc-tool-after-terminal` | an earlier terminal event could validate later unfinished activity | exactly one terminal finish must be the final event |
| `oc-unknown-event-part` | unknown event types were ignored | validate known event types and their part/state fields |

Adjacent checks cover non-string text, malformed tool identities/inputs, nested tool history inside results, untyped discriminators/roles and path shapes. Invalid bodies refuse before transmission. Signed assistant thinking history remains supported unchanged. Native fake-provider tests exercise the real Chuck/OpenCode clients, including their history replay.

A further regression binds the complete task/grading definition into completed-attempt identity. Changing an expected result can no longer reuse a verification recorded under the old contract.

The historical pre-repair acceptance flags are retained in `tests/fixtures/hybrid/claude-adversarial-cases.json`; tests assert that every reviewed case is refused by the repaired implementation. The adversarial request cost an estimated $0.0010015 and produced a settled provider receipt. This is usage accounting, not Console billing reconciliation or proof that a promotion applied.

The initial integration's separate fresh review had already repaired first-journal directory durability, missing optional SDK metadata, helper/source provenance, and overly broad failed-write recovery. The API-generated cases extend that gate; they do not establish a complete security proof or general agent quality gain. Shared memory, learned-prompt activation and production services remain outside this evaluation.
