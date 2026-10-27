import { RuntimeRunClient, type WorkbenchRun, type WorkbenchEvent } from "@ai-platform/sdk-web/run-client";

/** Business-host integration example. Each mount owns one identity and one Run.
 * Host must close it before changing identity or navigation. No business writes.
 */
export function mountReadonlyRun(
  client: RuntimeRunClient,
  runId: string,
  callbacks: {
    snapshot: (run: WorkbenchRun, events: WorkbenchEvent[]) => void;
    event: (event: WorkbenchEvent) => void;
    error: () => void;
  },
) {
  const controller = new AbortController();
  let closed = false;
  const finished = (async () => {
    try {
      const snapshot = await client.inspect(runId, controller.signal);
      if (closed) return;
      callbacks.snapshot(snapshot.run, snapshot.events);
      if (closed) return;
      const cursor = snapshot.events.at(-1)?.seq ?? 0;
      for await (const event of client.follow(runId, cursor, controller.signal)) {
        if (closed) return;
        callbacks.event(event);
      }
    } catch {
      if (!closed) callbacks.error(); // No raw response, token, or automatic retry.
    }
  })();
  return { finished, close() { closed = true; controller.abort(); } };
}
