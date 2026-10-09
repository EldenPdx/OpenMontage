# Browser production control plane

Status: accepted for Web Studio v1 (#10). Scope: local single user and one execution slot.

The browser submits a brief to the existing Backlot FastAPI application. A durable
PostgreSQL command queue hands the task to a worker. The worker supervises a real
Pi RPC process; Pi reads AGENT_GUIDE.md, selects an existing pipeline, and follows
its manifest, stage director and provider skills through the controlled bridge.
Canonical project files remain visible through the existing Backlot board.

Python owns lifecycle, persistence, validation and execution policy. It does not
choose creative stages or reproduce pipeline orchestration. A Pi acknowledgement,
agent_end or agent_settled is never evidence of a successful video.

| Source of truth | Contents |
| --- | --- |
| PostgreSQL | task/run, configuration fingerprint and non-secret snapshot, commands, leases/fences, gates/decisions, events, budget reservations, model/tool call journal, file-write intents |
| projects/<project_id>/ | existing project marker, canonical checkpoints/artifacts/history, media, render reports and final video |
| .runtime/studio/ | isolated Pi agent configuration and persistent session JSONL, project ownership locks; PostgreSQL keeps exact internal session references |

Pi configuration and sessions stay outside projects because Backlot serves project
media. They are never exposed by API or copied into public events. Secret values,
authorization headers and raw subprocess environments are excluded from snapshots,
ordinary database records, argv and diagnostics. No cache or broker is needed;
future cache additions use Redis and never replace authoritative persistence.

Each task creates its own project, run and exact session. A resumed attempt retains
that session and completed project stages. Changing a provider/model/runtime or
other approved creative scope creates a new approval version. Multiple tasks may
wait for approval while another task uses the execution slot.

PostgreSQL and files have no shared transaction. Before an atomic file replacement,
record the intended path, old/new byte hashes, revision and live fence. Write a
same-directory temporary file, flush it, atomically replace and record the applied
intent. Restart reconciliation compares actual bytes with both hashes: old bytes
allow an intent to be completed, new bytes allow acknowledgement, any third value
is an explicit recovery conflict. Never silently overwrite externally changed files.

A database lease provides ownership and a monotonically increasing fence; an OS
project lock additionally excludes legacy CLI writers and stale processes. Expiry
alone never permits starting a second writer: the previous process must be stopped
and its project lock released. All bridge operations and worker mutations check the
current fence, including before external requests and before atomic file replacement.
Unmanaged historical/CLI projects keep their existing file behavior; writes to a
currently owned Studio project must use the same ownership guard or fail clearly.

The bridge allows structured registry calls and restricted knowledge/artifact
operations. Pi gets no default bash/edit tool, arbitrary HTTP, arbitrary MCP,
untrusted extensions, workflow/script execution or credential access. Allowed local
render commands are chosen by trusted tools, never supplied as shell strings by the
model/browser. Registry discovery is not authorization: selectors resolve only
explicitly allowed trusted providers/profiles. Trusted configuration is authoritative
even if repository .env or the host's Pi profile is polluted.

Pi v1.1.0's before_provider_request hook catches handler exceptions and continues.
Throwing from that hook is therefore not a security boundary. A trusted custom
provider/transport must authorize and reserve each request before actual network
I/O and fail closed, including retries, compaction, summaries and nested calls.
SDK retries and warming are disabled until interception is proven. Host extensions,
MCP and context files are disabled; only the named trusted extension is loaded.

Initial Pi source: https://github.com/earendil-works/pi, tag v1.1.0,
commit abe508e1b89912adde45528136c3221eb69acdd7, Node >=22.19.0.
Real Pi and real PostgreSQL are required for integration; deterministic local model
and media services can avoid paid calls. Production deployment is out of scope.

Shared ownership follows #9: #11 config; #12 database; #13 RPC; #14 trusted
extension/bridge and checkpoint/cost guards; #15 server registration; #16 worker;
#17 approvals; #18 Studio/library UI; #19 board controls; #20 runtime/dependencies;
#21 cross-module tests/CI. Interface changes update #10 and notify dependent owners.
