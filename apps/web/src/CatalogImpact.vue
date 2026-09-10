<script setup lang="ts">
import {ref,computed,onMounted} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import {api,post,hasRole} from './api'
import CatalogPlanDesk from './CatalogPlanDesk.vue'
import ImpactDisposition from './ImpactDisposition.vue'
const props=defineProps<{initialImpact?:string}>()
const emit=defineEmits(['open']),catalogs=ref<any[]>([]),changes=ref<any[]>([]),impacts=ref<any[]>([]),selected=ref<any>(null),cursor=ref<string|null>(null),handling=ref(props.initialImpact||'')
const versions=computed(()=>catalogs.value.flatMap(c=>c.versions.map((v:any)=>({...v,label:c.name+' v'+v.number}))))
const kind:Record<string,string>={added:'新增商品',retired:'停用商品',identity:'身份或编码变化',display:'展示信息变化'}
const statuses:Record<string,string>={OPEN:'需要复核',NOTICE:'待知悉',LOCAL_RESOLVED:'平台内处置完成',WAITING_DOWNSTREAM:'下游待确认',RESOLVED:'已完成'}
async function act(fn:()=>Promise<any>){try{await fn()}catch(e:any){if(e!=='cancel'&&e!=='close')ElMessage.error(e.message)}}
async function load(){const [c,j,t]=await Promise.all([api('/catalogs'),api('/catalog-changes'),api('/impact-tasks')]);catalogs.value=c.items;changes.value=j.items;impacts.value=t.items;cursor.value=t.next_cursor}
async function activate(c:any){await act(async()=>{await ElMessageBox.confirm('启用后，停用或身份变化的旧商品会阻断新批次发布；历史发布仍绑定原标准库。','启用新标准库',{confirmButtonText:'确认启用',cancelButtonText:'取消'});await post('/catalog-changes/'+c.id+'/activate');ElMessage.success('已启用新标准库');await load()})}
onMounted(()=>act(load))
</script>
<template><div class="enterprise-section"><div class="section-title"><div><h2>标准库变更影响</h2><p class="muted">先确认跨版本商品身份，再定位受影响的映射和发布。旧发布继续保留原依据。</p></div><el-button @click="act(load)">刷新分析</el-button></div>
<CatalogPlanDesk v-if="hasRole('admin')" @submitted="act(load)"/>
<section class="panel"><table class="data-table"><thead><tr><th>版本对</th><th>执行状态</th><th>影响统计</th><th>操作</th></tr></thead><tbody><tr v-for="c in changes"><td>{{ versions.find(v=>v.id===c.from_version_id)?.label }} → {{ versions.find(v=>v.id===c.to_version_id)?.label }}</td><td>{{ c.status==='SUCCEEDED'?'分析完成':c.status==='FAILED'?'分析失败':'等待分析' }}</td><td>{{ c.report.affected_items??'—' }} 条映射 · {{ c.report.affected_releases??'—' }} 份发布</td><td><el-button @click="act(async()=>selected=await api('/catalog-changes/'+c.id))">查看变化</el-button><el-button v-if="hasRole('admin')" :disabled="c.status!=='SUCCEEDED'" @click="activate(c)">启用新版本</el-button></td></tr></tbody></table><el-empty v-if="!changes.length" description="尚未提交标准库变更分析"/></section>
<section class="panel enterprise-card"><h3>受影响记录</h3><div v-for="i in impacts" class="enterprise-issue"><div><strong>{{ kind[i.kind] }}</strong><p>{{ i.item_id?'有效映射':'历史发布' }} · {{ (i.item_id||i.release_id).slice(0,8) }} · {{ statuses[i.status]||i.status }}</p></div><el-button @click="handling=i.id">处理影响</el-button><el-button v-if="i.item_id" @click="act(async()=>{const item=await api('/items/'+i.item_id+'/candidates');emit('open',{id:item.run_id,item_id:item.id})})">查看受影响商品</el-button></div><el-empty v-if="!impacts.length" description="暂无受影响记录" :image-size="50"/><el-button v-if="cursor" @click="act(async()=>{const r=await api('/impact-tasks?cursor='+cursor);impacts.push(...r.items);cursor=r.next_cursor})">更多影响</el-button></section>
<el-dialog :model-value="!!selected" @close="selected=null" title="标准商品变化明细" width="720px"><table class="data-table"><thead><tr><th>类型</th><th>原编号</th><th>新编号</th><th>变化字段</th></tr></thead><tbody><tr v-for="c in selected?.changes"><td>{{ kind[c.kind] }}</td><td>{{ c.old_sku||'—' }}</td><td>{{ c.new_sku||'—' }}</td><td>{{ c.fields?.join('、')||'—' }}</td></tr></tbody></table><el-button v-if="selected?.next_offset!==null" @click="act(async()=>{const r=await api('/catalog-changes/'+selected.id+'?offset='+selected.next_offset);selected={...r,changes:[...selected.changes,...r.changes]}})">更多变化</el-button></el-dialog>
<ImpactDisposition v-if="handling" :id="handling" @close="handling=''" @resolved="act(load)" @open="emit('open',$event)"/></div></template>
