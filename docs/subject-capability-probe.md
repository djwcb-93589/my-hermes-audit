# Subject Capability Probe

The Subject Capability Probe is a strict JSON file-protocol subprocess started
with the same `sys.executable` as Audit. It is a compatibility check, not a
Trial and not a smoke run of MyHermes.

The parent creates a temporary `HERMES_HOME`, workspace, unused database path,
and capability-restricted config. Credential-shaped config references receive
probe-only placeholder values because the probe never performs a model call.
The Subject repository and the Audit `src` directory are the only application
import roots. Langfuse and Judge environment variables are never forwarded.

The subprocess checks these public surfaces:

- package import and Subject origin;
- `run_conversation`, `ToolRegistry`, `register_all`, and `ToolPolicy`;
- `build_system_prompt`;
- public Observation repository and view contracts;
- database initialization, session creation, and resource cleanup entrypoints;
- exported config projections used by the Worker;
- resolution of file and terminal declarations through a temporary registry.

The file/terminal check registers inert placeholder handlers from public tool
declarations. It never calls `register_all`, executes a handler, starts a
process, initializes SQLite, creates a session, or triggers Background Review.

`SubjectCapabilityReport` records the protocol version, Subject commit,
individual checks, missing capabilities, bounded warnings, and a public API
fingerprint. The fingerprint uses only module names, public object names, safe
call signatures, and the probe protocol version; it never hashes MyHermes
source text. If any required capability is absent, Suite preflight fails once
and lists the missing names before any Trial Sandbox is created.

Audit no longer imports `hermes.config._config`. The generated config validated
by the parent process is authoritative for disabled plugins. The Worker may
only perform a second check through MyHermes' exported `BROWSER_CONFIG` and
`BACKGROUND_REVIEW_CONFIG` projections. MyHermes currently has no equivalent
public plugins projection, so Audit does not work around that gap by reading a
private object or copying the Subject config model.

`myhermes-audit doctor` exposes the same local check. Its optional Langfuse and
Judge checks inspect dependency and configuration presence only. They do not
write remote data or send a model request, and output includes names/statuses
but not credential values or the complete environment.
