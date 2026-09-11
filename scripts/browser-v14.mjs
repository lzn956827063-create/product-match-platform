import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import {createRequire} from 'node:module'

const root=path.resolve(import.meta.dirname,'..')
const require=createRequire(path.join(root,'apps/web/package.json'))
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright')
const base=process.env.APP_URL||'http://127.0.0.1:18765'
const output=path.join(root,'docs/v14/browser')
fs.mkdirSync(output,{recursive:true})

const browser=await chromium.launch({headless:true})
const checks=[],errors=[]

async function open(role,viewport={width:1440,height:1000}){
  const context=await browser.newContext({viewport,reducedMotion:'reduce'})
  const page=await context.newPage()
  page.on('pageerror',error=>errors.push(error.message))
  page.on('response',response=>{if(response.status()>=500)errors.push(`${response.status()} ${response.url()}`)})
  await page.goto(base)
  await page.getByRole('button',{name:role,exact:true}).click()
  await page.getByRole('button',{name:'登录工作空间',exact:true}).click()
  await page.getByRole('heading',{name:'匹配任务',exact:true}).waitFor()
  return {context,page}
}

async function screenshot(page,name){
  await page.evaluate(()=>window.scrollTo(0,0))
  await page.screenshot({path:path.join(output,name+'.png'),fullPage:true,animations:'disabled'})
}

async function fitsViewport(page){
  return page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+2)
}

try{
  {
    const {context,page}=await open('数据专员')
    assert.equal(await page.getByRole('button',{name:'智能审核',exact:true}).count(),1)
    assert.equal(await page.getByRole('button',{name:'算法评测',exact:true}).count(),1)
    await page.getByRole('button',{name:'数据接入',exact:true}).click()
    await page.getByRole('heading',{name:'数据接入',exact:true}).waitFor()
    await page.locator('.settings-tabs').getByRole('button',{name:'接入源',exact:true}).click()
    await page.getByText('共享目录收件',{exact:true}).waitFor()
    await screenshot(page,'01-data-ingestion-desktop')
    checks.push('operator with annotator membership sees ingestion, smart review and algorithm navigation')
    await context.close()
  }
  {
    const {context,page}=await open('审核员')
    assert.equal(await page.getByRole('button',{name:'智能审核',exact:true}).count(),1)
    assert.equal(await page.getByRole('button',{name:'算法评测',exact:true}).count(),1)
    await page.getByRole('button',{name:'智能审核',exact:true}).click()
    await page.getByRole('heading',{name:'智能审核',exact:true}).waitFor()
    await page.locator('.risk-score').first().waitFor()
    await screenshot(page,'02-smart-review-desktop')
    checks.push('reviewer with annotator membership sees risk-prioritized explainable queue')
    await context.close()
  }
  {
    const {context,page}=await open('管理员')
    assert.equal(await page.getByRole('button',{name:'智能审核',exact:true}).count(),1)
    assert.equal(await page.getByRole('button',{name:'算法评测',exact:true}).count(),1)
    await page.getByRole('button',{name:'算法评测',exact:true}).click()
    await page.getByRole('heading',{name:'算法评测',exact:true}).waitFor()
    await page.getByText('构造数据只用于工程正确性和相对对比',{exact:false}).waitFor()
    await page.getByText('v1.4 两阶段匹配构造基准',{exact:true}).waitFor()
    await page.locator('.el-loading-mask').waitFor({state:'hidden'})
    await screenshot(page,'03-algorithm-evaluation-desktop')
    checks.push('admin sees scoped frozen metrics, threshold version and simulation warning')
    await context.close()
  }
  {
    const {context,page}=await open('管理员',{width:390,height:844})
    await page.getByRole('button',{name:'算法评测',exact:true}).click()
    await page.getByRole('heading',{name:'算法评测',exact:true}).waitFor()
    await page.getByText('v1.4 两阶段匹配构造基准',{exact:true}).waitFor()
    await page.locator('.el-loading-mask').waitFor({state:'hidden'})
    assert(await fitsViewport(page))
    await screenshot(page,'04-algorithm-evaluation-mobile')
    checks.push('mobile algorithm page remains within the viewport with stable navigation')
    await context.close()
  }
  assert.deepEqual(errors,[])
  const result={passed:true,checks,errors}
  fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(result,null,2)+'\n')
  console.log(JSON.stringify(result,null,2))
}catch(error){
  fs.writeFileSync(path.join(output,'results.json'),JSON.stringify({passed:false,checks,error:String(error),errors},null,2)+'\n')
  throw error
}finally{
  await browser.close()
}
