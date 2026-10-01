# Managed deployments

Managed mode runs Artemis as one user's executor on a hosted cloud phone, next
to that user's runtime ([AutoGLM-GUI](https://github.com/jbqvibecoding/AutoGLM-GUI)
in managed mode) and under a control plane that owns accounts, phones and
approvals ([cloudphone-agent](https://github.com/jbqvibecoding/jbqvibecoding-cloudphone-agent)).
The runtime submits goals to Artemis and streams its progress into the user's chat.

Turn it on with `ARTEMIS_MANAGED=1`. The code lives in `artemis/managed/`.

## What changes

| | Local install | Managed |
|---|---|---|
| API access | Whoever reaches the port | `Authorization: Bearer $ARTEMIS_API_TOKEN` on every request |
| Routes | Everything | `POST /api/run`, `POST /api/stop`, `GET /api/stream/{uuid}`, `GET /api/sessions/{uuid}[/steps]`, `GET /api/images/{name}`; anything else is 404 |
| Profiles | Flash, Pro | Flash only (Pro's `run_adb_command`, `manage_task` and diagnoser tools reach a raw adb shell) |
| `/api/run` fields | All | `goal`, `profile`, `device_serial`, `session_id` (a UUID, required), `ingress` |
| Payment and bank apps | No check | Input actions need the user's approval (below) |
| Models | Provider keys | Only the OpenAI-compatible gateway in `config/artemis.managed.jsonc` |
| Device awake service | On | Off (the control plane sleeps idle phones) |

## App guard

`GuardedActuator` (`artemis/managed/guard.py`) wraps the ADB actuator, the
single path every model-chosen device action takes.

Before a tap, long press, swipe, text input, erase, or a key other than back
or home, it reads the foreground package from the device; it never asks the
model which app is in front.

When that package is in `ARTEMIS_GUARDED_APPS`, the guard asks the user
through the control plane approvals API:

1. The control plane receives `kind="app_access"`, the package, and a
   540 px JPEG of the screen.
2. The user allows the agent once, always, or not at all.
3. While the guard waits, the session's SSE stream carries
   `approval_required` and then `approval_resolved`.

The guard handles each outcome as follows:

- **Allowed:** the agent may act in that app for the rest of the run.
- **Refused, expired or failed:** the action is not performed, and the run is
  cancelled. Anything other than an explicit approval counts as a refusal.
- **Foreground app unknown:** only that one action is refused; the run
  continues.

The guard also rejects key names that are not `[A-Za-z0-9_]` and links that
contain quotes or control characters. The ADB driver puts both into device
shell commands.

## Environment

| Variable | Meaning |
|---|---|
| `ARTEMIS_MANAGED` | `1` to enable |
| `ARTEMIS_API_TOKEN` | Bearer token for the API, at least 32 characters, fresh per launch |
| `ARTEMIS_ARTEMIS_JSONC` | Path to the managed model config (`/app/config/artemis.managed.jsonc` in the image) |
| `OPENAI_BASE_URL`, `OPENAI_API_KEY` | Model gateway and the user's gateway key |
| `ARTEMIS_MANAGED_UTILITY_MODEL` | Gateway model for helpers otherwise wired to Gemini (default `artemis-flash-lite`) |
| `ARTEMIS_GUARDED_APPS` | `package` or `package=Display name`, comma-separated; a package also covers its sub-packages |
| `ARTEMIS_CONTROL_PLANE_URL`, `ARTEMIS_RUNTIME_TOKEN` | Where and as whom to ask for approvals; required when guarded apps are set |

The server refuses to start without a valid token or config path, or with
guarded apps but no control plane.

Serve on loopback only:

```
uvicorn apps.admin_console.server:app --host 127.0.0.1 --port 8100
```
