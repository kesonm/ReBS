<script setup lang="ts">
import { computed, ref } from 'vue'
import { VideoPause, VideoPlay } from '@element-plus/icons-vue'
import type { Instrument, Trade } from '../types'
import { price, compact, time } from '../format'
const props = defineProps<{ trades: Trade[]; instrument: Instrument; frozen: boolean; source: string }>()
const emit = defineEmits<{ 'update:frozen': [value:boolean] }>()
const side = ref('all')
const visible = computed(() => props.trades.filter(t => side.value === 'all' || t.side === side.value).slice(0,100))
</script>
<template>
  <section class="recent-trades panel"><div class="panel-heading"><h2>最新成交</h2><span class="tiny-tag" :class="{ 'paused-tag': frozen }">{{ frozen ? '已暂停显示' : '实时' }}</span><button class="icon-button" :aria-label="frozen ? '继续显示成交' : '暂停显示成交'" :title="frozen ? '继续' : '暂停'" @click="emit('update:frozen',!frozen)"><el-icon><VideoPlay v-if="frozen"/><VideoPause v-else/></el-icon></button></div><div class="trade-controls"><span>{{ source }}</span><select v-model="side" aria-label="成交方向"><option value="all">全部</option><option value="buy">主动买入</option><option value="sell">主动卖出</option></select></div><div class="trade-columns"><span>价格 ({{ instrument.quote }})</span><span>数量 ({{ instrument.base }})</span><span>时间</span></div><div class="trade-scroll"><div v-for="item in visible" :key="item.id" class="trade-row"><span :class="item.side === 'buy' ? 'up' : 'down'">{{ price(item.price,instrument.pricePrecision) }}</span><span>{{ compact(item.size) }}</span><span>{{ time(item.ts) }}</span></div><div v-if="!visible.length" class="trade-placeholder">{{ trades.length ? '暂无该方向成交' : '等待成交数据' }}</div></div></section>
</template>
