// A browser child may exit while inherited stdio remains open elsewhere.
// Playwright waits for ChildProcess 'close' before removing its temporary profile.
// Release only our exited child's streams; never touch a running/user browser.
export function releaseExitedPipes(child) {
  child.once('exit', () => {
    for (const stream of child.stdio) stream?.destroy();
  });
}

export async function ownedBrowser(browserType, options) {
  const server = await browserType.launchServer({ ...options, host: '127.0.0.1', port: 0 });
  releaseExitedPipes(server.process());
  let browser;
  try { browser = await browserType.connect(server.wsEndpoint()); }
  catch (error) {
    try { await server.kill(); }
    catch (cleanup) { throw new AggregateError([error, cleanup], 'Browser connection and cleanup failed'); }
    throw error;
  }
  let closing;
  browser.close = () => closing ??= (async () => {
    const results = await Promise.allSettled(browser.contexts().map(context => context.close()));
    const errors = results.filter(result => result.status === 'rejected').map(result => result.reason);
    try {
      // Normal shutdown; close waits for process exit and temporary-profile cleanup.
      await server.close();
      const child = server.process();
      if (child.exitCode === null && child.signalCode === null) throw new Error('Owned browser process has not exited');
    } catch (error) { errors.push(error); }
    if (errors.length) throw new AggregateError(errors, 'Owned browser cleanup failed');
  })();
  return browser;
}
