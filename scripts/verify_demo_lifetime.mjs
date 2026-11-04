// Real wall-clock acceptance: a signed short-lived identity expires first,
// then the normal 28-minute demo expires and cleans its owned resources.
// No accelerated clock, real model, vendor message or business task is used.
import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {mkdtemp, mkdir, readFile, readdir, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve, relative, isAbsolute} from 'node:path';
import {fileURLToPath} from 'node:url';
import {parseArgs} from 'node:util';
import {setTimeout as sleep} from 'node:timers/promises';
import {randomUUID} from 'node:crypto';
import {preflight} from './demo-preflight.mjs';
import {stopOwnedChild} from './demo-cleanup.mjs';
import {ownedBrowser} from '../apps/console-web/tests/browser/owned-browser.mjs';

const root = resolve(fileURLToPath(new URL('..', import.meta.url)));
const {values} = parseArgs({options: {'report-dir': {type: 'string'}}, strict: true});
if (!values['report-dir']) throw new Error('--report-dir is required');
const evidence = resolve(values['report-dir']);
await mkdir(evidence, {recursive: true});
const config = await preflight({root, args: [], env: process.env});
const temporary = await mkdtemp(join(tmpdir(), 'aip-demo-lifetime-'));
const report = {status: 'RUNNING', started_at: new Date().toISOString(), real_wall_clock: true, model_calls: false, message_delivery: false, samples: []};
let browser, context, child, ready, spawnError, log = '', monitor;
let resolveExit;
const ended = new Promise(resolve => {resolveExit = resolve;});
async function save(name = 'progress.json') {await writeFile(join(evidence, name), JSON.stringify(report, null, 2));}
function contained(parent, child) {const part = relative(parent, resolve(child)); return part !== '..' && !part.startsWith('../') && !isAbsolute(part);}
async function waitUntil(deadline) {
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) throw new Error('Demo exited before its declared expiry');
    await sleep(Math.min(1000, deadline - Date.now()));
  }
}
try {
  child = spawn(process.execPath, ['scripts/start_local_demo.mjs'], {
    cwd: root, env: {PATH: process.env.PATH, HOME: process.env.HOME, TMPDIR: temporary,
      PLAYWRIGHT_MODULE: config.playwright, S12_DEMO_EVIDENCE: join(evidence, 'startup')}, stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.once('exit', (code, signal) => resolveExit({code, signal}));
  child.on('error', error => {spawnError = error; resolveExit({error: error.code});});
  const collect = bytes => {
    log += bytes.toString();
    if (log.length > 2 * 1024 * 1024) log = log.slice(-2 * 1024 * 1024);
    const match = log.match(/DEMO_READY (http:\/\/127\.0\.0\.1:\d+\/demo) expires=(\S+) pid=(\d+)/);
    if (match && !ready) {assert.equal(Number(match[3]), child.pid); ready = {url: match[1], expiry: Date.parse(match[2])};}
  };
  child.stdout.on('data', collect); child.stderr.on('data', collect);
  const startupDeadline = Date.now() + 120000;
  while (!ready) {
    if (spawnError || child.exitCode !== null || child.signalCode !== null || Date.now() >= startupDeadline) throw new Error('Lifetime fixture startup failed');
    await sleep(100);
  }
  assert.ok(Number.isFinite(ready.expiry) && ready.expiry > Date.now() && ready.expiry - Date.now() <= 28 * 60000);
  report.expires_at = new Date(ready.expiry).toISOString();
  const owned = (await readdir(temporary)).filter(name => name.startsWith('ai-platform-demo-'));
  assert.equal(owned.length, 1);
  const metadata = JSON.parse(await readFile(join(temporary, owned[0], 'fixture.json'), 'utf8'));
  assert.ok(contained(join(temporary, owned[0]), metadata.tokens));
  assert.equal(new URL(metadata.runtime).hostname, '127.0.0.1');
  const tokens = JSON.parse(await readFile(metadata.tokens, 'utf8'));
  assert.ok(Number.isSafeInteger(metadata.jwt_leeway_s) && metadata.jwt_leeway_s >= 0 && metadata.jwt_leeway_s <= 300);
  report.configured_jwt_leeway_seconds = metadata.jwt_leeway_s;
  const playwright = await import(config.playwright);
  browser = await ownedBrowser(playwright.chromium, {channel: 'chrome', headless: true});
  context = await browser.newContext();
  const page = await context.newPage(); page.setDefaultTimeout(5000);
  const posts = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && path.startsWith('/v1/')) posts.push(path);
  });
  await page.goto(new URL('/im-inbox', ready.url).href);
  async function apply(user, source) {
    await page.getByLabel('合成来源令牌', {exact: true}).fill(source);
    await page.getByLabel('本人用户令牌', {exact: true}).fill(user);
    await page.getByRole('button', {name: '应用身份并清空接收结果', exact: true}).click();
  }
  async function receive(id, text) {
    await page.getByLabel('事件ID', {exact: true}).fill(id);
    await page.getByLabel('合成消息', {exact: true}).fill(text);
    await page.getByRole('button', {name: '显式模拟接收一次', exact: true}).click();
    await page.getByRole('status').filter({hasText: '合成消息已接收'}).waitFor();
    await page.getByRole('button', {name: '本人只读核对原事件', exact: true}).click();
    await page.getByRole('article', {name: '本人接收回执'}).waitFor();
  }
  monitor = setInterval(() => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    try {
      const rss = Number(execFileSync('/bin/ps', ['-o', 'rss=', '-p', String(child.pid)], {encoding: 'utf8', timeout: 2000}).trim()) * 1024;
      report.samples.push({at: new Date().toISOString(), launcher_rss_bytes: rss});
      if (rss > 1024 ** 3) {report.resource_guard = 'launcher exceeds 1GiB'; child.kill('SIGINT');}
      void save().catch(error => {report.progress_write_error = error.code || error.message; child.kill('SIGINT');});
    } catch (error) {report.resource_observation_error = error.code || error.message;}
  }, 30000);
  const shortExpiry = Math.min(...[tokens.alice_short_lived, tokens.im_source_short_lived].map(token => JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString()).exp * 1000));
  assert.ok(shortExpiry > Date.now() + 10000, 'Short-lived fixture expired before acceptance setup');
  const shortEvent = 'real-ttl-' + randomUUID();
  await apply(tokens.alice_short_lived, tokens.im_source_short_lived);
  await receive(shortEvent, 'public short-lived expiry fixture');
  report.stage = 'waiting for real signed JWT expiry'; await save();
  await waitUntil(shortExpiry + 1000);
  await page.getByRole('status').filter({hasText: '测试身份已过期'}).waitFor();
  assert.equal(await page.getByRole('article', {name: '本人接收回执'}).count(), 0);
  assert.equal(await page.getByLabel('合成消息', {exact: true}).inputValue(), '');
  report.client_expiry_cleared_before_server_skew_window = true;
  // The existing verifier intentionally allows configured clock skew. Check
  // server rejection after that actual policy window, rather than silently
  // changing AUTH/JWT settings or weakening the expected 401.
  await waitUntil(shortExpiry + metadata.jwt_leeway_s * 1000 + 1000);
  const recovery = `${metadata.runtime}/v1/isolated-im/events/${shortEvent}?app_id=demo-app&platform_tenant_id=demo-org&sender_id=alice`;
  const expired = await context.request.get(recovery, {headers: {Authorization: `Bearer ${tokens.alice_short_lived}`}});
  assert.equal(expired.status(), 401);
  const fresh = await context.request.get(recovery, {headers: {Authorization: `Bearer ${tokens.alice}`}});
  assert.equal(fresh.status(), 200);
  assert.equal((await fresh.json()).run_created, false);
  report.real_jwt_expiry = {passed: true, server_expired_status: 401, fresh_identity_read_only_recovery: 200};
  const retainedBody = 'public demo-lifetime fixture ' + randomUUID();
  await apply(tokens.alice, tokens.im_source);
  await receive('demo-ttl-' + randomUUID(), retainedBody);
  assert.deepEqual(posts, ['/v1/isolated-im/events', '/v1/isolated-im/events']);
  report.stage = 'waiting for real 28-minute demo expiry'; await save();
  await waitUntil(ready.expiry);
  let exitTimer;
  const natural = await Promise.race([
    ended,
    new Promise((_, reject) => {exitTimer = setTimeout(() => reject(new Error('Demo did not stop after its declared expiry')), 15000);}),
  ]).finally(() => clearTimeout(exitTimer));
  assert.deepEqual(natural, {code: 0, signal: null});
  await page.getByText(/演示身份已到期，请停止或重新启动独立环境/).waitFor();
  assert.equal(await page.getByRole('article', {name: '本人接收回执'}).count(), 0);
  assert.notEqual(await page.getByLabel('合成消息', {exact: true}).inputValue(), retainedBody);
  assert.equal(await page.getByRole('button', {name: '显式模拟接收一次', exact: true}).isDisabled(), true);
  assert.deepEqual(posts, ['/v1/isolated-im/events', '/v1/isolated-im/events']);
  assert.ok(log.includes('CLEANUP: owned demo temporary root removed'));
  assert.deepEqual((await readdir(temporary)).filter(name => name.startsWith('ai-platform-demo-')), []);
  report.real_demo_expiry = {passed: true, automatic_stop_exit: natural, automatic_task_posts: 0, cleanup_confirmed: true};
  report.status = 'PASS';
} catch (error) {
  report.status = 'FAIL'; report.error = error.message; process.exitCode = 1;
} finally {
  clearInterval(monitor);
  const cleanupErrors = [];
  try {await context?.close();} catch (error) {cleanupErrors.push(error.message);}
  try {await browser?.close();} catch (error) {cleanupErrors.push(error.message);}
  let stopped = false;
  try {stopped = (await stopOwnedChild(child)).stopped;} catch (error) {cleanupErrors.push(error.message);}
  if (stopped) {try {await rm(temporary, {recursive: true}); report.owned_parent_removed = true;} catch (error) {cleanupErrors.push(error.message);}}
  if (cleanupErrors.length) {report.cleanup_errors = cleanupErrors; report.status = 'FAIL'; process.exitCode = 1;}
  await writeFile(join(evidence, 'launcher.log'), log);
  report.finished_at = new Date().toISOString(); await save('result.json');
  console.log(JSON.stringify({status: report.status, stage: report.stage, report: join(evidence, 'result.json')}));
}
