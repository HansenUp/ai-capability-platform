// Real local Keycloak form, disposable fixture accounts only. Never prints URLs/tokens.
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE);
const configuration=JSON.parse(await readFile(process.argv[2],'utf8'));
assert.equal(new URL(configuration.url).origin,'http://127.0.0.1:8180');
const browser=await chromium.launch({channel:'chrome',headless:true});
try{
 const page=await browser.newPage();page.setDefaultTimeout(10000);
 await page.goto(configuration.url);
 await page.locator('input[name="username"]').fill(configuration.username);
 await page.locator('input[name="password"]').fill('f8-local-development-only');
 await page.locator('input[name="password"]').evaluate(input=>input.form.requestSubmit());
 await page.waitForURL(url=>url.origin==='http://127.0.0.1:18765'&&url.pathname==='/callback');
 await page.getByText(configuration.reject?'Login rejected. Return to the desktop and try again.':'Login complete. Return to the desktop window.',{exact:true}).waitFor();
 console.log(configuration.reject?'PASS real Keycloak rejects invalid or cancelled callback':'PASS real Keycloak authorization code and PKCE callback');
}finally{await browser.close();}
