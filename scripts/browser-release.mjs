import {createRequire} from 'node:module'
import fs from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'
import crypto from 'node:crypto'
const root=path.resolve(import.meta.dirname,'..'),require=createRequire(path.join(root,'apps/web/package.json'))
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright'),fixture=JSON.parse(fs.readFileSync(process.argv[2])),output=path.resolve(process.env.BROWSER_OUTPUT||path.join(root,'docs/enterprise/browser'))
const browser=await chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[]
page.on('pageerror',e=>errors.push(e.message))
try{
 await page.goto(process.env.APP_URL||'http://127.0.0.1:18766');await page.getByRole('button',{name:'管理员',exact:true}).click();await page.getByRole('button',{name:'登录工作空间',exact:true}).click();await page.getByRole('heading',{name:'匹配任务',exact:true}).waitFor();await page.getByRole('button',{name:'发布与协作',exact:true}).click()
 await page.getByRole('heading',{name:'完整结果与发布版本'}).waitFor();await page.locator('.enterprise-section .el-select').first().click();await page.locator('.el-select-dropdown__item').filter({hasText:fixture.batch_name}).click()
 await page.getByRole('button',{name:'发布完整批次',exact:true}).waitFor();await page.getByRole('button',{name:'发布完整批次',exact:true}).click();await page.getByRole('button',{name:'确认发布',exact:true}).click();await page.getByRole('button',{name:'下载 CSV',exact:true}).first().waitFor();await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='下载 CSV'&&!b.disabled))
 const full=page.locator('.enterprise-issue').filter({has:page.getByText('完整核对结果',{exact:true})});await full.getByRole('button',{name:'下载 CSV'}).waitFor();await page.screenshot({path:path.join(output,'09-published-release.png'),fullPage:true});const pending=page.waitForEvent('download');await full.getByRole('button',{name:'下载 CSV'}).click();const download=await pending;const file=path.join(output,'published-full.csv');await download.saveAs(file);const bytes=fs.readFileSync(file);assert(bytes.toString().startsWith('\uFEFFstable_key,source_sku'));assert.deepEqual(errors,[])
 fs.writeFileSync(path.join(output,'release-results.json'),JSON.stringify({passed:true,release_id:fixture.release_id,download_sha256:crypto.createHash('sha256').update(bytes).digest('hex'),checks:['publisher freezes reviewed complete batch via confirmation dialog','generated full CSV downloaded through UI','no browser exceptions']},null,2))
}catch(e){await page.screenshot({path:path.join(output,'release-failure.png'),fullPage:true});throw e}finally{await browser.close()}
