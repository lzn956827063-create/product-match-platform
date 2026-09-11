<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { CircleAlert, Gauge, RefreshCw, ShieldAlert } from 'lucide-vue-next'
import { api } from './api'
const emit=defineEmits(['open'])
const rows=ref<any[]>([]),busy=ref(false),level=ref('')
const visible=computed(()=>rows.value.filter(r=>!level.value||r.risk_level===level.value))
async function load(){busy.value=true;try{rows.value=(await api('/learning-queue')).items}catch{}finally{busy.value=false}}
onMounted(load)
</script>
<template><div class="v14-page"><div class="page-heading"><div><div class="heading-kicker">可解释风险分流</div><h1>智能审核</h1><p>先处理冲突、候选不足和不确定性高的记录，每个排序因素均可追溯。</p></div><el-button @click="load"><RefreshCw :size="15"/>刷新队列</el-button></div><div class="risk-toolbar"><el-segmented v-model="level" :options="[{label:'全部',value:''},{label:'最高优先',value:'CRITICAL'},{label:'高优先',value:'HIGH'},{label:'普通',value:'NORMAL'}]"/><span>当前 {{ visible.length }} 条待审核</span></div><section class="panel" v-loading="busy"><div class="table-scroll"><table class="data-table risk-table"><thead><tr><th>优先级</th><th>来源商品</th><th>排序依据</th><th>候选状态</th><th class="align-right">操作</th></tr></thead><tbody><tr v-for="r in visible"><td><span class="risk-score" :class="r.risk_level"><Gauge :size="15"/>{{ r.risk_score.toFixed(1) }}</span><small class="cell-sub">{{ {CRITICAL:'最高优先',HIGH:'高优先',NORMAL:'普通'}[r.risk_level as 'HIGH'] }}</small></td><td><strong>{{ r.source.name }}</strong><small class="cell-sub mono">{{ r.source.sku }}</small></td><td><div class="risk-factors"><span v-for="f in r.factors" :class="f.code==='HARD_CONFLICT'?'danger':''"><ShieldAlert v-if="f.code==='HARD_CONFLICT'" :size="12"/><CircleAlert v-else :size="12"/>{{ f.label }}<small>{{ f.detail }}</small></span></div></td><td>{{ r.rejected?'明确拒识':'进入人工建议区' }}<small class="cell-sub">首选分 {{ r.top_score??'—' }} · 分差 {{ r.candidate_margin??'—' }}</small></td><td class="align-right"><button class="text-button" @click="emit('open',{id:r.run_id,item_id:r.id})">进入审核</button></td></tr></tbody></table></div><el-empty v-if="!visible.length" description="当前筛选下没有待审核记录"/></section></div></template>
