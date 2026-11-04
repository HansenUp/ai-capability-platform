// Only ChildProcess objects created by this demo invocation may be supplied.
// No PID lookup, process adoption, shell command, or unrelated process cleanup.
function exited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

function waitForExit(child, milliseconds) {
  if (exited(child)) return Promise.resolve(true);
  return new Promise(resolve => {
    let timer;
    const finish = stopped => {
      clearTimeout(timer);
      child.off('exit', onExit);
      child.off('error', onError);
      resolve(stopped);
    };
    const onExit = () => finish(true);
    const onError = () => { if (!child.pid) finish(true); };
    child.once('exit', onExit);
    child.on('error', onError);
    timer = setTimeout(() => finish(exited(child)), milliseconds);
    // Exit could have been delivered between the caller's check and subscription.
    if (exited(child)) finish(true);
  });
}

export async function stopOwnedChild(child, {
  signal = 'SIGINT', graceMs = 5000, terminateMs = 2000, killMs = 2000,
} = {}) {
  if (!child || exited(child)) return { stopped: true, forced: false };
  // A failed spawn has no OS child. Its error is reported by the startup path.
  if (!child.pid) return { stopped: true, forced: false };
  let forced = false;
  for (const [nextSignal, waitMs] of [[signal, graceMs], ['SIGTERM', terminateMs], ['SIGKILL', killMs]]) {
    if (exited(child)) return { stopped: true, forced };
    const pending = waitForExit(child, waitMs);
    try { child.kill(nextSignal); }
    catch { /* The exit event still determines whether the owned child is gone. */ }
    if (await pending) return { stopped: true, forced };
    forced = true;
  }
  throw new Error('Owned demo child exit is unconfirmed; temporary directory retained.');
}

async function settledWithin(pending, milliseconds) {
  let timer;
  try {
    return await Promise.race([
      pending.then(() => true),
      new Promise(resolve => { timer = setTimeout(() => resolve(false), milliseconds); }),
    ]);
  } finally { clearTimeout(timer); }
}

export async function closeOwnedServer(server, { graceMs = 5000, forceMs = 2000 } = {}) {
  if (!server) return { forced: false };
  const pending = Promise.resolve().then(() => server.close());
  if (await settledWithin(pending, graceMs)) return { forced: false };
  // The demo tests have already exited. Only this invocation's HTTP connections
  // can be closed; no shared listener or unrelated browser is involved.
  server.httpServer?.closeAllConnections?.();
  if (await settledWithin(pending, forceMs)) return { forced: true };
  throw new Error('Owned demo server close is unconfirmed; temporary directory retained.');
}

export async function cleanupDemo({ tests, portal, server, fixture, writeEvidence, removeTemporary }) {
  const errors = [];
  let resourcesStopped = true, removed = false;
  async function step(label, action) {
    try { await action(); }
    catch (cause) { resourcesStopped = false; errors.push(new Error(label, { cause })); }
  }
  await step('test process cleanup failed', async () => {
    const result = await stopOwnedChild(tests, { signal: 'SIGTERM' });
    if (result.forced) errors.push(new Error('Owned tests required forced termination.'));
  });
  for (const [label, serverToClose] of [['Portal', portal], ['Console', server]]) {
    await step(`${label} server cleanup failed`, async () => {
      if ((await closeOwnedServer(serverToClose)).forced) errors.push(new Error(`${label} connections required forced cleanup.`));
    });
  }
  await step('fixture cleanup failed', async () => {
    const result = await stopOwnedChild(fixture);
    if (result.forced) errors.push(new Error('Owned fixture required forced termination.'));
    if (fixture?.exitCode !== null && fixture?.exitCode !== undefined && fixture.exitCode !== 0) {
      errors.push(new Error(`Owned fixture exited with code ${fixture.exitCode}.`));
    }
  });
  // Evidence failures must not prevent process/server cleanup above.
  let evidenceSaved = false;
  try { await writeEvidence(); evidenceSaved = true; }
  catch (cause) { errors.push(new Error('Demo cleanup evidence could not be saved.', { cause })); }
  if (resourcesStopped && evidenceSaved) {
    try { await removeTemporary(); removed = true; }
    catch (cause) { errors.push(new Error('Owned demo temporary directory cleanup failed.', { cause })); }
  }
  return { errors, resourcesStopped, removed };
}
