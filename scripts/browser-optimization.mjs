import {createRequire} from 'node:module'
import fs from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'
const root=path.resolve(import.meta.dirname,'..'),require=createRequire(path.join(root,'apps/web/package.json'))
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright'),base=process.env.APP_URL||'http://127.0.0.1:18766'
const output=path.join(root,'docs/enterprise/browser-regression');fs.mkdirSync(output,{recursive:true})
const browser=await chromium.launch({headless:true}),context=await browser.newContext({viewport:{width:1440,height:1000},recordVideo:{dir:path.join(output,'video'),size:{width:1440,height:1000}}}),page=await context.newPage(),errors=[],checks=[]
let summarySeen=false,detailSeen=false
page.on('response',r=>{if(r.url().includes('/items?')&&r.url().includes('details=false'))summarySeen=true;if(r.url().endsWith('/candidates'))detailSeen=true})
page.on('pageerror',e=>errors.push(e.message));page.on('response',r=>{if(r.status()>=500)errors.push(`${r.status()} ${r.url()}`)})
async function login(role){await page.getByRole('button',{name:role,exact:true}).click();await page.getByRole('button',{name:'登录工作空间',exact:true}).click();await page.getByRole('heading',{name:'匹配任务',exact:true}).waitFor()}
const shot=async name=>page.screenshot({path:path.join(output,name+'.png'),fullPage:true,animations:'disabled'})
try{
 await page.goto(base);await login('数据专员')
 const taskName='浏览器补数验收 · '+Date.now()
 await page.getByRole('button',{name:'新建匹配任务',exact:true}).click()
 const create=page.getByRole('dialog',{name:'新建匹配任务'});await create.locator('input[type=file]').setInputFiles(path.join(root,'samples/供应商商品表.csv'));await create.getByRole('button',{name:'上传并配置字段'}).click();await create.getByRole('button',{name:'校验数据'}).click();await create.getByText('确认排除以上 1 条错误记录，保留 29 条有效记录',{exact:true}).click();await create.getByPlaceholder('例如：九月手机采购商品核对').fill(taskName);await create.getByPlaceholder('填写供应商来源').fill('浏览器补数验收');await create.getByRole('button',{name:'确认 29 条并开始匹配'}).click();await page.locator('.review-grid').waitFor({timeout:45000})
 await page.locator('.workbench-filters').getByRole('button',{name:/需复核/}).click();await page.locator('.source-row').first().click()
 const originalSku=await page.locator('.source-row.selected .mono').textContent()
 await page.getByRole('button',{name:'补充本条资料',exact:true}).click()
 const desk=page.getByRole('dialog',{name:'待补资料与更正草稿'})
 await desk.getByLabel('补数颜色',{exact:true}).fill(['黑色','白色','银色','黑色','蓝色','黑色','白色','蓝色','黑色','黑色','黑色','黑色'][(Number(originalSku)-1)%12])
 await desk.getByPlaceholder('例如：供应商 9 月 10 日规格说明，第 3 项').fill('受控浏览器验收：独立来源字段补充，不代表真实供应商确认')
 await desk.getByRole('button',{name:'保存本条草稿',exact:true}).click();await desk.getByText('草稿 v1',{exact:true}).waitFor()
 await desk.locator('.correction-grid>section').evaluate(el=>el.scrollTop=0);await shot('01-correction-draft');checks.push('source-only correction fields, evidence and persisted draft')
 const download=page.waitForEvent('download');await desk.getByRole('button',{name:'下载问题行模板'}).click();const d=await download;await d.saveAs(path.join(output,'corrections.csv'));assert(fs.readFileSync(path.join(output,'corrections.csv'),'utf8').includes('source_id'));checks.push('problem-only correction template download')
 const submitted=page.waitForResponse(r=>r.url().endsWith('/corrections/submit')&&r.status()===202)
 await desk.getByRole('button',{name:'提交 1 条补数草稿'}).click();const result=await(await submitted).json();assert.equal(result.affected,1)
 await page.getByText('补数版本：仅含 1 条修正记录。完整交付请前往“批次完整发布”。',{exact:true}).waitFor();await page.locator('.review-grid').waitFor()
 assert.equal(await page.locator('.source-row').count(),1);assert.equal(await page.locator('.source-row.selected .mono').textContent(),originalSku)
 await page.getByText('受控浏览器验收：独立来源字段补充，不代表真实供应商确认',{exact:true}).waitFor()
 assert(await page.getByRole('button',{name:'确认匹配',exact:true}).isDisabled());await shot('02-corrected-subset');checks.push('one-row new revision/run, source provenance, submitter cannot review')
 await page.getByRole('button',{name:'版本比较',exact:true}).click();await page.getByRole('button',{name:'比较变化',exact:true}).click();await page.getByText(/共 1 处变化/).waitFor();await shot('03-stable-comparison');checks.push('stable-ID comparison limits parent to correction subset');await page.getByRole('dialog',{name:'比较同一批次的运行版本'}).getByRole('button',{name:/Close this dialog|关闭此对话框/}).click()
 await page.getByRole('button',{name:'退出并切换账号'}).click();await login('审核员')
 await page.locator('tbody tr').filter({hasText:taskName}).filter({hasText:'输入 v2'}).getByRole('button',{name:taskName,exact:true}).click();await page.locator('.review-grid').waitFor()
 // Open the specific correction run via task list, which is sorted newest first.
 await page.getByText('补数版本：仅含 1 条修正记录。完整交付请前往“批次完整发布”。',{exact:true}).waitFor()
 await page.getByRole('button',{name:'确认匹配',exact:true}).click();await page.getByText('当前结论：已确认',{exact:false}).waitFor();checks.push('independent reviewer confirms corrected run')
 await page.getByRole('button',{name:'退出并切换账号'}).click();await login('数据专员');await page.getByRole('button',{name:'新建匹配任务',exact:true}).click()
 const imp=page.getByRole('dialog',{name:'新建匹配任务'});await imp.locator('input[type=file]').setInputFiles(path.join(root,'samples/供应商商品表.csv'));await imp.getByRole('button',{name:'上传并配置字段'}).click();await imp.getByRole('button',{name:'保存映射模板'}).waitFor();await imp.getByPlaceholder('供应商名称（用于复用映射）').fill('模板验收 '+Date.now());await imp.getByRole('button',{name:'保存映射模板'}).click();await page.getByText('映射模板 v1 已保存',{exact:true}).waitFor();await shot('04-mapping-template');checks.push('asynchronous parse and versioned mapping template')
 await imp.getByRole('button',{name:'校验数据'}).click();await imp.getByRole('button',{name:'下载完整问题清单'}).waitFor();const issuesDownload=page.waitForEvent('download');await imp.getByRole('button',{name:'下载完整问题清单'}).click();await(await issuesDownload).saveAs(path.join(output,'import-issues.csv'));await shot('05-full-validation');checks.push('asynchronous validation and full issues download')
 await imp.getByRole('button',{name:/Close this dialog|关闭此对话框/}).click();await page.getByRole('button',{name:'退出并切换账号'}).click();await login('管理员')
 await page.getByRole('button',{name:'组织设置',exact:true}).click();await page.getByRole('button',{name:'策略评测',exact:true}).click();await page.getByRole('heading',{name:'策略评测与影子对照'}).waitFor();assert(await page.getByRole('button',{name:'发布此评测策略',exact:true}).first().isDisabled());await shot('06-strategy-evaluation');checks.push('simulation report visible and policy promotion disabled');await page.getByRole('button',{name:'运行影子对照',exact:true}).first().click();await page.getByRole('heading',{name:'影子对照 · 计算完成',exact:true}).waitFor({timeout:45000});await shot('07-shadow');checks.push('read-only experimental shadow comparison completed');assert(summarySeen&&detailSeen);checks.push('summary list and selected-candidate details requested independently');await page.getByRole('button',{name:'运行状态',exact:true}).click();await page.getByRole('heading',{name:'任务与存储状态'}).waitFor();await shot('08-operations');checks.push('durable queue and cleanup diagnostics visible')
 await page.setViewportSize({width:390,height:844});await shot('07-mobile');assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+2));checks.push('390px viewport has no page overflow')
 assert.deepEqual(errors,[]);fs.writeFileSync(path.join(output,'results.json'),JSON.stringify({passed:true,checks},null,2));console.log(JSON.stringify({passed:true,checks},null,2))
}catch(e){await shot('failure');fs.writeFileSync(path.join(output,'results.json'),JSON.stringify({passed:false,checks,error:String(e),errors},null,2));throw e}
finally{await context.close();await browser.close()}
