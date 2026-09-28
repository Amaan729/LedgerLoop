# Design notes

Decisions and the reasoning behind them. The README covers what the system does.

## Log first, decisions derived

A CRUD design would store "invoice 104233 is paid" and lose how it got there. Here the stored facts are the inputs (the payment arrived, with this memo, from this payer name) and the decision is derived from them. That costs a replay on boot, but it buys three things:

1. **Explaining any decision later.** The audit entry has the full decision body: what the agent saw, which rule fired, and which policy version it ran under.
2. **Proving the explanation is true.** `replay.verify` re-derives the decision from the same inputs and checks the hash.
3. **Testing a policy change against real history** before it ships (`replay.what_if`).

Human resolutions are events too, not side-channel updates. Otherwise replay would diverge from live the first time someone clicked "release".

## Effectively-once from at-least-once

Redis Streams with consumer groups delivers at least once. The engine:

1. reads a batch (XREADGROUP),
2. appends it to the log, skipping event_ids already stored,
3. runs agents on the new events only,
4. writes events, decisions, exceptions, tool recordings, and audit rows in **one** transaction,
5. acks.

A crash before 4 commits nothing, and Redis redelivers. A crash between 4 and 5 means the redelivered batch is all duplicates: a no-op. On restart the consumer reads its own pending list first (`XREADGROUP ... 0`) before asking for new entries.

If the commit throws, in-memory state has already moved ahead of the database, so the engine throws it away and rebuilds from the log. That's crude, but it's always correct, and failures should be rare.

Same `event_id` with different content is a **conflict**, not a duplicate. It's never applied. That's the case where a producer reused an id, and silently picking either version would be wrong.

## What determinism requires

Replay only works if an agent's output depends on nothing but its inputs. The rules:

- **No wall clock.** "Now" is the event's `occurred_at`. The overdue check uses the order's timestamp, not today.
- **No unordered iteration** where order affects the result. Candidate lists are sorted by (due date, id) or (score, id) before anything picks from them. Open invoices per customer live in an insertion-ordered dict.
- **No unrecorded external calls.** Anything that can change between runs (a model, a service, and in principle the parser code itself) goes through `RecordingTools`. In replay mode a missing recording raises; it never silently calls live.
- **Policy is versioned per decision.** Boot and verify look up the version each decision was made under.
- **Ids are derived, not generated.** Decision and exception ids are hashes of (event id, agent), so a replay produces the same ids.

`verify` catches violations: a decision whose hash differs from the stored one means one of these rules was broken, or the agent code changed. The second case is expected after a deploy. That's the "code drift" problem every event-sourced system has, and the answer here is the same as elsewhere: version the logic, or snapshot and accept that old decisions were made by old code.

Caches are allowed if they can't change results. The payer-name cache keeps the top two candidates per name and is keyed on the name block's membership, and the benchmark decisions were compared before and after adding it.

## The LLM parser

The regex handles almost all memos. The LLM is a fallback for the rest, and it's deliberately limited:

- It runs only when the regex finds nothing, so cost and latency only apply to the residue.
- It **proposes** refs. It doesn't decide anything. The cash agent still checks that each ref exists, belongs to the payer, and fits the amount.
- Its output is recorded, so replay doesn't depend on the model answering the same way twice (it won't).

The general pattern: let the model do the fuzzy perception step, keep the decision in deterministic code that checks the model's output, and record the model's answer so replay stays deterministic.

## Cash matching: precision over coverage

Order of attack, cheapest and most certain first:

1. same bank ref as an earlier payment → stop, possible duplicate
2. parse refs from the memo
3. identify the payer by name (prefix-aware for truncated bank feeds)
4. refs resolve to invoices → allocate (exact, short-pay within tolerance, or a verified partial)
5. no usable refs → match on amount: one invoice, a unique combination (bounded subset-sum over the oldest 12), or one invoice short by less than the tolerance

Every tie goes to a person. Two invoices with the same amount? Review. Two combinations that sum to the payment? Review. A partial payment that could also be a typo for a sibling invoice? Review. The benchmark counts wrong auto-decisions separately from auto-resolution for this reason. A system that auto-applies everything somewhere would score 100% auto-resolution and be useless.

## Batch size

Throughput comes from amortizing the transaction and the round trips. At 500 events per batch, p50 batch time is ~110 ms, so an event waits at most about that long before it's durable. Smaller batches cut latency and throughput together. For AR, a few hundred milliseconds doesn't matter, so the default favors throughput.

## Known gaps, roughly in the order I'd fix them

1. **Unapplied cash should offset exposure.** Right now a customer whose payment is stuck in review looks overdue to the risk agent. Credit teams net unapplied cash against AR. That needs a notion of "probably this customer's cash", which is exactly what the cash agent couldn't determine, so it isn't trivial.
2. **Snapshots**, so boot doesn't replay everything.
3. **Partition by customer** to run more than one consumer. Every agent's state is per-customer except duplicate detection and payer identification, which need a shared name index.
4. **Calibrate the simulator** against real remittance data.
