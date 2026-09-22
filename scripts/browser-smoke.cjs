const {chromium}=require(process.env.PLAYWRIGHT_MODULE);
const fs=require('fs');const path=require('path');
(async()=>{const browser=await chromium.launch({executablePath:process.env.BROWSER_PATH,headless:true});try{
const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
const token=fs.readFileSync(process.env.UI_TOKEN_FILE,'utf8').trim();await page.goto('http://127.0.0.1:8022/#token='+token);await page.locator('#login').waitFor({state:'hidden'});
await page.locator('#task').fill('修复加法计算错误，查看差异并运行测试');await page.locator('#create').click();await page.waitForFunction(()=>document.querySelector('#status').textContent==='ready');await page.locator('#run').click();await page.waitForFunction(()=>document.querySelector('#status').textContent==='waiting_approval');
if(!await page.locator('#approvals').innerText().then(t=>t.includes('return a + b')))throw Error('Missing diff');
await page.getByRole('button',{name:'批准',exact:true}).click();await page.getByRole('button',{name:'批准',exact:true}).waitFor({state:'hidden'});await page.locator('#run').click();await page.getByRole('button',{name:'批准',exact:true}).waitFor();await page.getByRole('button',{name:'批准',exact:true}).click();await page.getByRole('button',{name:'批准',exact:true}).waitFor({state:'hidden'});await page.locator('#run').click();await page.waitForFunction(()=>document.querySelector('#status').textContent==='completed');
if(!(await page.locator('#stats').innerText()).includes('passed'))throw Error('Tests not passed');
await page.reload();await page.locator('#sessions button').first().click();await page.waitForFunction(()=>document.querySelector('#status').textContent==='completed');
const out=path.resolve('docs/screenshots');fs.mkdirSync(out,{recursive:true});await page.screenshot({path:path.join(out,'desktop.jpg'),fullPage:true,quality:80});
await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(out,'mobile.jpg'),fullPage:true,quality:75});const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);if(overflow||errors.length)throw Error(JSON.stringify({overflow,errors}));
const report={checks:['login','create','tool_loop','edit_review','test_approval','real_fixture_tests','reload_session','mobile_no_overflow'],pageErrors:errors,passed:true};fs.writeFileSync('evaluation/browser-results.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report));
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});
