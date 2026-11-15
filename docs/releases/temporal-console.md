# Temporal workflow console

The Agents view now follows the native Temporal Web UI: its dark workspace, navigation, namespace selector,
blue running badges, green completed badges, striped execution table, filters and execution detail tabs. The
reference is Terra's running Temporal UI and the [official UI source](https://github.com/temporalio/ui).
The Temporal mark is MIT licensed; its notice is in `docs/licenses/temporal-ui-MIT.txt`.

- **Workflows:** real counts across retained executions, server-side pagination, execution status and workflow type
  filters, Workflow ID prefix search, auto refresh, selectable rows and JSON export.
- **Saved Views:** up to 50 named filter combinations saved in this browser.
- **Execution details:** original Workflow/Run IDs, metadata and copyable deep links; Timeline, Event History,
  Relationships, Workers, Pending Activities and Metadata tabs.
- **Event history:** real Temporal events for the selected run, with original event IDs, timestamps, activity types
  and retry attempts. The latest 200 events are bounded and cached briefly. Timeline spans use recorded event times;
  incomplete history is labelled rather than inventing durations.
- **Worker availability:** recent workflow/activity poller counts and the runtime heartbeat. Poller counts do not
  imply unused execution slots.
- **Start Workflow:** returns to Terra's existing investigation composer for the selected area.
- **View settings:** light/dark themes, UTC/local/relative time, saved-view management and map performance timings.

The shared console remains read-only for existing executions. Workflow inputs, outputs, heartbeat payloads, signal
arguments, failure messages and provider credentials are excluded from the history response. History requests are
validated against Terra's task queue and supported workflow types. Public errors never echo private Temporal errors.

Opening the console pauses the background globe, including when opened directly from an execution link. Closing it
resumes the scene. Data polling stops when the console closes and pauses while the page is hidden. The interface works
from 360 px up; wide execution and timeline tables scroll within their own panels.

References: [Temporal Web UI](https://docs.temporal.io/web-ui),
[Temporal Python client](https://python.temporal.io/temporalio.client.Client.html).
