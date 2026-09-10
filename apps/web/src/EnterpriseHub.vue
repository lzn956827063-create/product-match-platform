<script setup lang="ts">
import {ref,computed} from 'vue'
import {hasRole} from './api'
import ReleaseCenter from './ReleaseCenter.vue'
import ReviewQueue from './ReviewQueue.vue'
import SupplierQuality from './SupplierQuality.vue'
import IntegrationCenter from './IntegrationCenter.vue'
import CatalogImpact from './CatalogImpact.vue'
import TodoCenter from './TodoCenter.vue'
import {api} from './api'
import AnnotationDesk from './AnnotationDesk.vue'
defineProps<{initialBatch?:string}>();const emit=defineEmits(['open']);const tab=ref('release'),impact=ref(''),releaseBatch=ref(''),delivery=ref('')
async function navigate(r:any){if(r.kind==='impact')impact.value=r.id;if(r.kind==='delivery')delivery.value=r.id;if(r.kind==='release')releaseBatch.value=(await api('/releases/'+r.id)).batch_id;tab.value=r.route}
const tabs=computed(()=>[{id:'release',name:'批次发布',allowed:true},{id:'todo',name:'统一待办',allowed:true},{id:'review',name:'审核协作',allowed:hasRole('reviewer','supervisor','admin','publisher')},{id:'catalog',name:'标准库影响',allowed:true},{id:'quality',name:'供应商质量',allowed:true},{id:'integration',name:'系统交付',allowed:hasRole('integration_manager','publisher','admin')},{id:'annotation',name:'独立标注',allowed:hasRole('admin','annotator','adjudicator')}].filter(t=>t.allowed))
</script>
<template><div class="enterprise-hub"><div class="page-heading"><div><div class="heading-kicker">企业试用工作流</div><h1>发布与协作</h1><p>完整交付、责任分派、下游回执与数据质量。</p></div></div><div class="settings-tabs enterprise-tabs"><button v-for="t in tabs" :key="t.id" :class="{selected:tab===t.id}" @click="tab=t.id;impact='';delivery='';releaseBatch=''">{{ t.name }}</button></div><ReleaseCenter v-if="tab==='release'" :initial-batch="releaseBatch||initialBatch" @open="emit('open',$event)"/><ReviewQueue v-if="tab==='review'" @open="emit('open',$event)"/><TodoCenter v-if="tab==='todo'" @navigate="navigate" @open="emit('open',$event)"/><CatalogImpact :initial-impact="impact" v-if="tab==='catalog'" @open="emit('open',$event)"/><SupplierQuality v-if="tab==='quality'"/><IntegrationCenter :initial-delivery="delivery" v-if="tab==='integration'"/><AnnotationDesk v-if="tab==='annotation'"/></div></template>
