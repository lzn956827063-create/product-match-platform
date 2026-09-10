<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Activity, RefreshCw } from 'lucide-vue-next'
import { api } from './api'
const state=ref<any>(null)
async function load(){try{state.value=await api('/operations')}catch(e:any){ElMessage.error(e.message)}}
onMounted(load)
</script>
<template><section class="panel settings-card"><div class="section-title"><h3><Activity :size="18"/>任务与存储状态</h3><el-button @click="load"><RefreshCw :size="14"/>刷新</el-button></div><template v-if="state"><div class="quality-stats"><div><strong>{{ state.outbox_pending }}</strong><span>待完成后台事件</span></div><div><strong>{{ Math.round(state.oldest_outbox_seconds) }}s</strong><span>最老待处理事件</span></div><div><strong>{{ state.expired_leases }}</strong><span>过期任务租约</span></div><div><strong>{{ state.cleanup_pending }}</strong><span>待清理到期文件</span></div></div><el-alert v-for="alert in state.alerts" :title="alert" type="warning" :closable="false"/><el-alert v-if="!state.alerts.length" title="当前没有触发队列、心跳或清理告警" type="success" :closable="false"/><div class="two-columns"><div><h4>任务阶段累计</h4><dl class="spec-list"><dt>队列等待</dt><dd>{{ state.timings.queue_seconds.toFixed(2) }} 秒</dd><dt>索引准备</dt><dd>{{ state.timings.index_seconds.toFixed(2) }} 秒</dd><dt>匹配计算</dt><dd>{{ state.timings.matching_seconds.toFixed(2) }} 秒</dd><dt>结果写入准备</dt><dd>{{ state.timings.persistence_prepare_seconds.toFixed(2) }} 秒</dd><dt>租约回收</dt><dd>{{ state.timings.lease_recoveries }} 次</dd></dl></div><div><h4>导入状态与失败原因</h4><p v-for="(count,status) in state.imports">{{ status }} <strong>{{ count }}</strong></p><p v-for="reason in state.import_errors" class="conflict-text">{{ reason.reason }} · {{ reason.count }} 次</p><p v-if="!state.import_errors.length" class="muted">没有失败的导入记录</p></div></div><p class="muted">到期文件每小时进入清理队列；失败后重试。原始输入、有效版本和模型制品保留引用。HTTP 延迟与失败率可通过管理员指标接口接入监控。</p></template></section></template>
