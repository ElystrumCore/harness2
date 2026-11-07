# Bounded ai-memory integration

The experimental hybrid adapter accepts an optional `MemoryPacket`. It remains off when that argument is absent and performs no memory-service calls. The host can prepare a packet with `AiMemoryReader`, which uses ai-memory 3.1.1's read-only JSON API. Start the separately configured local server with `serve --transport http --enable-api`; use an explicit port and scope.

```python
from harness2.integrations.memory import AiMemoryReader
from harness2.integrations.hybrid import HybridAdapter

packet = AiMemoryReader(
    'http://127.0.0.1:49374', 'synthetic-lab', 'fixture',
    ['notes/synthetic.md'],
).recall('synthetic arithmetic', 'one')
adapter = HybridAdapter(output, 'chuck', executor=executor, memory_packet=packet)
adapter.execute('ledger-train', None, 'one')
```

The host freezes exact page IDs, paths, body digests, workspace, project and run ID. Only three explicitly allowlisted Markdown paths are admitted. Responses and the rendered packet are bounded; wrong scopes, redirects, non-loopback endpoints, duplicate paths, stale run IDs and recognizable credential patterns are refused. Credential-pattern checks are defense in depth, not a complete secret detector. Use synthetic or deliberately approved memory content.

Memory-enabled run directories are created owner-only (`0700`), independently of the caller's umask, enclosing the recalled evidence and worker files.

The packet enters the worker's task text as quoted JSON historical data. It is never appended to system instructions or used to grant tools or declare success. The existing verifier still decides whether the artifact is correct. `memory.json` and its digest accompany the attempt and participate in cache identity; changed packets or tampered evidence refuse reuse. The memory endpoint and any bearer stay outside the worker.

This integration does not install capture hooks, backfill transcripts, start a service, write learning or publish to Cyclone. The reviewed upstream hook-spool privacy issue therefore remains outside the enabled path. For the pilot configure backfill, profiles, automatic learning and automatic forgetting off, and require approval for any later learning experiment. A token may be supplied to the reader in memory by an authorized host caller; the command-line helper deliberately has no token flag.

The source-review lab exercised the actual ai-memory server/API in an isolated network namespace with synthetic pages and a foreign-project control. Two adapter attempts passed with a deterministic worker double. Portable regression tests cover scope, byte limits, quoted delimiters, memory-off, credentials, stale runs, cached evidence and false success. This establishes transport and admission, not a measured improvement in live Claude task quality. The protocol is pinned to the reviewed 3.1.1 shape; review later upstream versions before replacing it. Complete recovery requires both SQLite and wiki backups.
