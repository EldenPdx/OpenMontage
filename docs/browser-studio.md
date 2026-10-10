# Browser Studio: local development runtime

Browser Studio uses the existing Backlot application, a PostgreSQL control plane,
and an official Pi child process managed by the worker. This guide covers the
base development runtime. It does not perform production deployment. Existing
CLI commands and the read-only Backlot server continue to work without PostgreSQL.

## Install the base runtime

Use Python 3.10+ and Node 22.19.0+. Docker is needed for the supplied local
PostgreSQL service; an existing PostgreSQL service can also be used.

```sh
make install-studio
make studio-pi
make studio-db
make studio-check
```

For browser integration tests, run `make install-studio-dev` and install the
Playwright Chromium browser with `.venv/bin/python -m playwright install chromium`.
These commands install development dependencies only and do not call model or
media providers.

`compose.studio.yaml` and `Dockerfile.studio` are separate from any existing
local Docker files. `make studio-db` starts only PostgreSQL and creates private,
ignored files under `.runtime/studio/`:

- `postgres.password`: generated development database password, mode 0600.
- `database-local.env`: host `STUDIO_DATABASE_URL` for `127.0.0.1:55432`.
- `database.env`: container `STUDIO_DATABASE_URL` for the `postgres` service.

Load the local backend environment without placing its password in command-line
arguments:

```sh
set -a
. .runtime/studio/database-local.env
set +a
```

The database is named `openmontage_studio`, with user `openmontage`. The port binds
to localhost. `make studio-check` verifies a real connection with `SELECT 1`;
business migrations are a separate application operation. Do not change the
password file after PostgreSQL initialization without updating the database user.

## Official Pi source and build

The deployment source is [earendil-works/pi](https://github.com/earendil-works/pi).
The initial baseline is tag `v1.1.0`, commit
`abe508e1b89912adde45528136c3221eb69acdd7`.
`pi-runtime/source.lock.json` records the official release source archive SHA-256,
its upstream `package-lock.json` SHA-256, package identity, minimum Node version,
and build command. It does not resolve floating `main` or npm `latest`.

The official release source archive includes the generated model catalog and
native prebuilds that a Git checkout omits. The installer verifies both archive
and dependency lock before `npm ci --ignore-scripts` and `npm run build:offline`.
The latter uses release model data instead of refreshing remote catalogs.
The source and installed metadata live in ignored `.runtime/pi/`; no third-party
runtime binaries are committed. Stop workers before rerunning the installer;
each installation builds a clean verified source tree before replacing the
managed executable, without deleting session or project data.

The managed executable is:

```text
.runtime/pi/source/packages/coding-agent/dist/bundle/cli.js
```

Check it with `.venv/bin/python pi-runtime/check.py`. This launches the genuine
Pi CLI in an isolated temporary working/config/session directory, verifies
version 1.1.0 and real `get_state` / `get_commands` responses, and closes stdin
for orderly shutdown. No provider credentials or model requests are needed.

`pi-runtime/run.sh` is a standalone credential-free RPC diagnostic launcher. It
keeps its persistent agent/session/home directories under `.runtime/studio/pi/`,
starts with an empty environment apart from managed paths, and disables ambient
extensions, MCP, skills, prompts, context files, built-in tools, and startup
network operations. The worker uses the same official executable with its own
per-run config/session directory and explicit trusted bridge extension. Pi has
no built-in HTTP task service. The worker owns deadlines, process groups,
signal handling, and bounded termination after closing stdin.

## Profiles and secrets

The backend default profile is xvan: `https://xvan.ai/v1`, `openai-responses`,
`gpt-5.6-sol`, credential reference `NEW_API_KEY`. Custom provider, base URL,
protocol, model, model capabilities, pricing, and request limits are configured
through backend profiles; the default provider is not a restriction.
The Pi agent profile is separate from the existing `newapi_llm` text tool.
The configuration module generates managed Pi settings/models files and a
minimal process environment. Supply real provider credentials to the backend
only; never include them in browser requests, repository files, image layers,
CLI arguments, or public logs.

RPC readiness does not certify a paid gateway. A real xvan compatibility smoke
requires separate authorization for a paid request. Default validation uses a
controlled local model endpoint and does not silently swap protocol or model.

## Development container and persistence

The optional runtime container is built from the same locked official source:

```sh
docker compose -f compose.studio.yaml --profile runtime build runtime
docker compose -f compose.studio.yaml --profile runtime run --rm runtime
```

The image copies application code explicitly; it does not copy `.env`, the
working tree's `.runtime`, or the user's home directory. No Docker socket is
mounted. The container runs as an unprivileged user, with a separate
`studio-state` volume for managed state. It is a development image, not an
instruction to deploy production services.

PostgreSQL data lives in the `openmontage-studio_postgres-data` named volume.
Stopping or recreating the service preserves it:

```sh
docker compose -f compose.studio.yaml stop postgres
docker compose -f compose.studio.yaml up -d --wait postgres
```

Back up the database with `pg_dump`; keep the file truth and Pi sessions alongside
it when backing up the complete application:

```sh
mkdir -p .runtime/studio/backups
umask 077
docker compose -f compose.studio.yaml exec -T postgres pg_dump -U openmontage -d openmontage_studio > .runtime/studio/backups/studio.sql
```

Copy the private backup to storage outside the checkout. Do not use `docker compose down -v` unless
you intentionally want to delete persisted development data. Restore into an
empty development database using `psql` before starting workers. The existing
`projects/` directory remains the canonical home of checkpoints and media;
PostgreSQL stores the control records and exact file references.

## Start the complete local application

After loading the private database environment above, migrate once:

```sh
.venv/bin/python -m production migrate
```

Enable Studio through trusted backend configuration or `STUDIO_ENABLED=1`. Start
the API and the worker in separate terminals with the same environment:

```sh
STUDIO_ENABLED=1 .venv/bin/python -m uvicorn backlot.server:app --host 127.0.0.1 --port 4751
STUDIO_ENABLED=1 .venv/bin/python -m production worker
```

Open `http://127.0.0.1:4751/studio`. Enter a description, select an available
profile, duration, aspect ratio and budget. Submission commits a queued task;
it does not claim a video exists. The worker owns the real Pi process, so closing
the browser does not stop execution. Studio needs PostgreSQL, the installed Pi,
and configured backend credentials; its readiness response explains missing
dependencies. The original Backlot library and CLI remain usable independently.

The initial Studio tool boundary supports fixed FFmpeg media operations and
configured New API image/video models. It runs on POSIX hosts, including the
development Linux container and macOS. Arbitrary shell, generated programs,
HTML compositions, MCP and host extensions are disabled. Existing standalone
Remotion and HyperFrames workflows remain available through the CLI. The browser
server is for local single-user use; public access requires a separate identity
and authorization design.

Pi configuration is independent of the existing `newapi_llm` tool. Configure the
New API media profiles through the existing [gateway guide](../skills/core/newapi.md)
and the Pi profile through [Pi configuration](pi-configuration.md). The defaults
select Images2.5-Flare and dreamina-seedance-2-5-260628; narration defaults off.
Profiles and media gateway settings are frozen for each run. A configuration
change requires explicit reconciliation or a new task, rather than silently
changing provider or protocol.
The agent's catalog includes the selected image/video deployment profiles, with
their declared parameters, defaults and limits. It does not expose credentials
or advertise other models as alternatives to the frozen selection.

## Review, costs, cancellation and recovery

Backlot displays the exact current proposal, script, scene plan and assets before
their manifest-defined gates can advance. Approve confirms that revision and
scope. Request revision stores feedback in the exact Pi session and invalidates
the old decision. Refreshing or repeating a click does not create another task
or continuation. A stale page receives a conflict and reloads the current gate.

Unknown model/media prices create a separate cost gate before any paid request.
The gate identifies the provider/model and estimated reservation. Quoted actions
above the configured single-action threshold also need a specific cost approval.
Reservations are estimates, not a hard limit on gateway charges. Received results
with unquoted fees retain their holds and display **Unquoted**, rather than zero.
Only trusted billing reconciliation may settle those holds.
Requesting revision of a media cost gate lets the agent correct its parameters
and open a new gate. The corrected request needs its own exact approval; the
previous decision does not authorize it.

The single-action threshold follows `budget.single_action_approval_usd` by default.
A trusted `studio.single_action_approval_usd_micros` override takes precedence.
The selected threshold is frozen in each task snapshot; browser requests cannot
replace it. Reject blocks the current plan and preserves the task for a revised
attempt; Stop/abort requests cancellation.

Cancel prevents new calls, clears Pi's queued messages and stops its managed
process group and tool processes. A submitted cloud job may continue and charge;
cancelling locally does not promise a remote refund. Durable external job receipts
are captured before polling finishes. Resuming a known job only polls/downloads;
a lost submission result stays `recovery_required` and cannot trigger another POST.

Stop the worker with Ctrl-C or SIGTERM. After an interrupted run, start the worker
again or inspect recovery explicitly:

```sh
.venv/bin/python -m production recover
```

Recovery verifies the original process identity, session and writer fence before
handing over the slot. File/DB disagreements and unverifiable process identities
remain visible for operator reconciliation. Restore or repair the private evidence
and reconcile gateway receipts through the trusted backend; do not delete intents,
clear reservations or forge approval/checkpoint files to force a retry. The browser
Resume action accepts only tasks whose prerequisites are safe.

Succeeded requires the canonical compose checkpoint, consistent render report and
final review, verified media bytes, and an actual ffprobe matching the requested
duration/aspect ratio. The final player and download use checked result references.
Pi accepting a prompt or becoming idle never completes a task.

## Verify changes without paid services

```sh
make install-studio-dev
.venv/bin/python -m playwright install chromium
make studio-pi
make studio-db
make studio-check
make lint
STUDIO_REQUIRE_INTEGRATION=1 make test
```

The Studio suite uses real PostgreSQL and official Pi with loopback model/media
fixtures and real FFmpeg. It exercises browser submission, multiple approvals,
revision, actual playback/download, leases, cancellation, restart, event replay,
cost admission and file reconciliation. Default CI installs those same dependencies
and contains no paid-provider credentials. See [validation evidence and live smoke](browser-studio-validation.md).
