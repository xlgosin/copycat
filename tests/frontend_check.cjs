// Isolated browser checks: every request is fulfilled locally; no service or trading account.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const root = path.resolve(__dirname, '..');
    const state = {mode:'paper',running:false,capital:'100',multiplier:'3',max_gross:'300',leverage:3,
      source:{equity:300000,updated:new Date().toISOString()},positions:{},records:[],realized:'0',dingtalk:{}};
    let hangStatus = false, starts = 0, stops = 0, releaseStart;
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if(url.pathname === '/api/status') {
        if(hangStatus) return; // AbortController must recover the poller.
        return route.fulfill({json:state});
      }
      if(url.pathname === '/api/start') {
        starts++;
        await new Promise(resolve => {releaseStart = resolve});
        return route.fulfill({json:{ok:true}}).catch(()=>{});
      }
      if(url.pathname === '/api/stop') {stops++; return route.fulfill({json:{ok:true}})}
      if(url.pathname === '/api/records') return route.fulfill({json:url.searchParams.has('before')
        ? {records:[{note:'older record',status:'filled'}],next_before:null}
        : {records:[{note:'latest record',status:'filled'}],next_before:100}});
      const file = url.pathname === '/' ? 'templates/index.html' : url.pathname.slice(1);
      return route.fulfill({body:fs.readFileSync(path.join(root,file)),
        contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});
    });
    await page.goto('http://copycat.test/');
    await page.locator('#token').fill('test-token');
    await page.locator('#loginForm button').click();
    await page.locator('#dashboard').waitFor({state:'visible'});
    await page.locator('#start').click();
    await page.waitForFunction(()=>actionBusy);
    assert(await page.locator('#start').isDisabled());
    assert(await page.locator('#resolve').isDisabled());
    assert(await page.locator('#stop').isEnabled());
    await page.locator('#stop').click();
    assert.equal(stops,1);
    releaseStart();
    await page.waitForFunction(()=>!actionBusy);
    assert.equal(starts,1);

    // Shorten just the application's 15s timeout to keep this regression fast.
    await page.evaluate(()=>{
      const original = window.setTimeout;
      window.setTimeout = (fn,delay,...args)=>original(fn,delay===15000?50:delay,...args);
    });
    hangStatus=true;
    await page.evaluate(()=>refresh());
    assert.match(await page.locator('#running').textContent(),/状态未知/);
    assert.equal(await page.evaluate(()=>busy),false);
    hangStatus=false;
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#connection').textContent(),'已连接');
    await page.locator('[data-tab=orders]').click();
    await page.locator('#older').click();
    await page.waitForFunction(()=>!historyBusy);
    assert.match(await page.locator('#orderList').textContent(),/older record/);
    await page.evaluate(()=>refresh());
    assert.match(await page.locator('#orderList').textContent(),/older record/);
    await page.locator('#latest').click();
    assert.equal(await page.evaluate(()=>historyPage),null);
    for(const width of [375,1440]) {
      await page.setViewportSize({width,height:1000});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: duplicate action prevention, pause during start, timeout recovery, history pagination, desktop/mobile layout');
  } finally {await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
