# Optional governed hybrid evaluation lab

This extension runs the native Harness2 propose/probe/compose method against isolated Chuck and OpenCode workers. It adds a direct Anthropic text proposer, deterministic artifact checks and one host-only provider broker. It is an opt-in experiment, not a production runtime registration or automatic learned-prompt release.

## Prerequisites and pins

Linux with working unprivileged bubblewrap namespaces, Python 3.12 or 3.13 for Harness2, and separate prepared worker checkouts:

- Chuck: `b5c0ef2bb5f21061e32b4422454095b3adb56746`, clean, with its own installed Python environment.
- agent-lineage: `03db5f7f8de333b2ac1b6320873fe24db501a483`, clean.
- OpenCode: `23fb5e0516c99ac04a1aa46c193efda2e1b9bb24` (1.14.18), Bun 1.3.11. Apply exactly `third_party/patches/opencode-compat.diff` then `third_party/patches/hybrid-opencode-events.diff`; install its frozen lock with lifecycle scripts disabled. Unknown changes refuse.

Do not point the v1 worker at OpenCode v2. Do not modify serving checkouts. Missing namespace support refuses execution; there is no host fallback. Install this fork in its own environment (`pip install -e .`); the controller uses Python's standard library. The worker environments carry their own dependencies. The Anthropic SDK is needed by Chuck, not the host broker.

## Configuration and zero-call checks

Create a private experiment root outside the Git checkout. Copy `harness2/integrations/runtime.example.json` to `runtime.json` inside it and replace all five paths with existing absolute paths. No credential belongs in that JSON. Runtime/source validation happens before credentials are loaded.

```
python -m harness2.integrations.pilot --plan
python -m unittest discover -s tests -v
```

The default test suite makes no external API calls. Five actual namespace/native-worker tests are explicit gates; run them on a prepared host before paid work:

```
HARNESS2_NATIVE_TESTS=1 HARNESS2_NATIVE_RUNTIME=/absolute/lab/runtime.json \
  python -m unittest discover -s tests -v
```

`HARNESS2_TEST_EVIDENCE_DIR` optionally retains synthetic native test outcomes in an existing directory. No test loads a real key. CI runs the host-independent suite and reports native tests as skipped rather than pretending their prerequisites exist.

## Paid pilot

Use an already approved credential in `ANTHROPIC_API_KEY`, or explicitly name an existing credential file with `--env-file`. Never put a credential in arguments or runtime configuration. No credential file is searched implicitly.

```
python -m harness2.integrations.pilot --live \
  --lab-root /absolute/lab --output /absolute/lab/run-001
```

The entire local `run-*` series shares a $5 ceiling, including every prior settled call. Each session is capped at $0.25; provider timeout is 60 seconds, worker timeout 300 seconds, and calls are serialized. Admission requires worst-case session headroom; each actual request has its own durable pre-send reservation. Unknown transmission/usage and prior false-success markers block later runs. Existing run directories are never overwritten or automatically resumed.

Only `claude-haiku-5-5` and bounded Messages API text/tool-history shapes are admitted. The real key stays in the host broker. Workers receive temporary tokens over a mounted Unix socket and have no external network route. Shared memory and learning are off. Candidate files are restricted to `systemprompt.md` and `tool_descriptions/tool_guidance.md`, at most 12,000 characters/64KiB. Candidates cannot install code, plugins, skills or subagents or widen policy.

The pilot executes four fixed baselines, four native Harness2 solver rollouts with three API improvement stages, freezes the result, then runs one held-out case. At k=1 selection is trivial and requires no API call. The text proposer is a documented method variant, not the upstream agentic-proposer benchmark. A KEEP_BASE result and empty library are valid; they do not establish an improvement.

## Evidence and limits

Each output retains `experiment.json`, append-only gateway receipts, complete bounded worker events, typed outcomes, artifact checks, `method.json`, `frozen.json` and `report.json`. Successful delivery, successful process and correct artifact are distinct. Permission refusals, unresolved tool failures, nonzero exits and missing terminal events refuse verification. Ordinary read failures can recover only through later matching tool/path success.

The broker receipt total is the authoritative experimental cost estimate. Upstream Harness2 method dollar ledgers currently omit solver rollout dollars; they must not be mistaken for a complete experiment total. Published API rates are estimates, not invoice reconciliation or proof that promotional credit applied. No Console balance is inferred.

An initial local pilot verified 9/9 synthetic worker attempts after repairing signed-history replay, recovered-read classification and OpenCode event draining. Across its four retained integration revisions it recorded 59 provider calls and an estimated $0.014177595. All prompts remained base and the library empty. These tiny arithmetic cases prove transport/method behavior, not general coding quality or security against another process running as the same Unix user. Raw local run/credential artifacts are not included in the fork.
