<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Lock, Refresh, Wallet } from '@element-plus/icons-vue'
import { AccountError, demoRequest, getDemoStatus, type AccountRow, type DemoOrder, type DemoOverview, type DemoResult, type DemoStatus } from '../demo'
import type { Instrument } from '../types'
import { dateTime, price, time } from '../format'

const props=defineProps<{instrument:Instrument; lastPrice?:number}>()
const status=ref<DemoStatus|null>(null)
const statusError=ref('')
const password=ref('')
let token=''
const unlocked=ref(false)
const account=ref<DemoOverview|null>(null)
const error=ref('')
const busy=ref(false)
const submitting=ref(false)
const canceling=ref('')
const resultMessage=ref('')
const tab=ref('balances')
const onlyPair=ref(false)
const side=ref<'buy'|'sell'>('buy')
const orderType=ref<'limit'|'market'>('limit')
const orderPrice=ref('')
const size=ref('')
const pending=ref<{instId:string;clOrdId:string}|null>(restorePending())
const secure=window.location.protocol==='https:' || ['127.0.0.1','localhost','[::1]'].includes(window.location.hostname)
const configured=computed(()=>status.value?.credentialsConfigured && status.value.accessConfigured)
const valid=computed(()=>Number(size.value)>0 && Number.isFinite(Number(size.value)) && (orderType.value==='market' || Number(orderPrice.value)>0 && Number.isFinite(Number(orderPrice.value))))
const estimated=computed(()=>Number(size.value)*(orderType.value==='market' ? props.lastPrice ?? 0:Number(orderPrice.value)))
const available=computed(()=>account.value?.balances.find(r=>r.ccy===(side.value==='buy' ? props.instrument.quote:props.instrument.base))?.availBal)
const tabs=[{id:'balances',label:'资产'},{id:'positions',label:'持仓'},{id:'pending',label:'当前委托'},{id:'history',label:'历史委托'},{id:'fills',label:'成交记录'}]
const rows=computed(()=>{
  const data=account.value?.[tab.value as 'balances'|'positions'|'pending'|'history'|'fills'] ?? []
  return onlyPair.value && tab.value!=='balances' ? data.filter(r=>r.instId===props.instrument.instId):data
})
const unavailable=computed(()=>account.value?.[tab.value as 'positions'|'pending'|'history'|'fills']===null)
const stateNames:Record<string,string>={live:'待成交',partially_filled:'部分成交',filled:'已成交',canceled:'已撤销',mmp_canceled:'已撤销'}
let poll:ReturnType<typeof setTimeout>|undefined
let controller:AbortController|undefined
let mutation:AbortController|undefined
let disposed=false

function restorePending() {
  try {
    const value=JSON.parse(sessionStorage.getItem('rebs.demo.pending') ?? 'null')
    if (value && /^[a-f0-9]{32}$/.test(value.clOrdId) && /^[A-Z0-9]+-[A-Z0-9]+$/.test(value.instId)) return {instId:String(value.instId),clOrdId:String(value.clOrdId)}
  } catch { /* Only the public request IDs, never credentials, are stored. */ }
  return null
}
function rememberPending(value:{instId:string;clOrdId:string}|null) {
  pending.value=value
  try { if (value) sessionStorage.setItem('rebs.demo.pending',JSON.stringify(value)); else sessionStorage.removeItem('rebs.demo.pending') } catch { /* Storage is optional. */ }
}
function decimal(value:string|undefined|null, precision=8) {
  return value===undefined || value===null || value==='' ? '—':price(Number(value),Math.min(precision,Math.max(0,(value.split('.')[1] ?? '').replace(/0+$/,'').length)))
}
function stamp(value:string|undefined) { return dateTime(value ? Number(value):null) }
function isOrder(row:AccountRow) { return row.state==='live' || row.state==='partially_filled' }
async function loadStatus() {
  statusError.value=''
  try { status.value=await getDemoStatus(AbortSignal.timeout(15000)) }
  catch (err) { statusError.value=err instanceof Error ? err.message:'无法读取模拟盘状态。' }
}
function schedule() {
  clearTimeout(poll)
  if (!disposed && unlocked.value) poll=setTimeout(()=>void refresh(),15000)
}
async function refresh(fresh=false) {
  if (!unlocked.value || busy.value || disposed) return
  if (document.hidden && !fresh) { schedule(); return }
  clearTimeout(poll)
  busy.value=true
  controller=new AbortController()
  try {
    const snapshot=await demoRequest<DemoOverview>(token,`overview${fresh?'?fresh=true':''}`,undefined,AbortSignal.any([controller.signal,AbortSignal.timeout(30000)]))
    if (disposed || !unlocked.value) return
    if (snapshot.environment!=='demo') throw new Error('账户环境校验失败。')
    account.value=snapshot; error.value=''
  } catch (err) {
    if (!disposed && unlocked.value && !controller.signal.aborted) {
      error.value=err instanceof Error ? err.message:'模拟账户更新失败。'
      if (err instanceof AccountError && [401,403].includes(err.status)) lock()
    }
  } finally { busy.value=false; schedule() }
}
async function unlock() {
  if (!secure || !configured.value || !password.value.trim() || busy.value) return
  token=password.value.trim(); password.value=''; unlocked.value=true
  await refresh(true)
}
function lock() {
  clearTimeout(poll); controller?.abort(); mutation?.abort()
  unlocked.value=false; token=''; password.value=''; account.value=null; resultMessage.value=''
}
function useLast() { if (props.lastPrice) orderPrice.value=props.lastPrice.toFixed(props.instrument.pricePrecision) }
watch(()=>props.instrument.instId,()=>{ size.value='';orderPrice.value='';resultMessage.value='' })
async function submitOrder() {
  if (!valid.value || submitting.value || pending.value || !account.value) return
  const order:DemoOrder={instId:props.instrument.instId,side:side.value,ordType:orderType.value,sz:size.value.trim(),
    ...(orderType.value==='limit'?{px:orderPrice.value.trim()}:{}),clOrdId:crypto.randomUUID().replace(/-/g,'')}
  submitting.value=true
  try {
    try {
      await ElMessageBox.confirm(`模拟${order.side==='buy'?'买入':'卖出'} ${order.sz} ${props.instrument.base}，${order.ordType==='limit'?`限价 ${order.px} ${props.instrument.quote}`:'按市价成交，实际价格以成交记录为准'}。使用模拟资金。`,
        '确认模拟委托',{confirmButtonText:'提交模拟委托',cancelButtonText:'返回修改',type:'info',closeOnClickModal:false})
    } catch { return }
    if (!unlocked.value || disposed) return
    rememberPending({instId:order.instId,clOrdId:order.clOrdId})
    mutation=new AbortController()
    const result=await demoRequest<DemoResult>(token,'order',order,AbortSignal.any([mutation.signal,AbortSignal.timeout(30000)]))
    if (disposed || !unlocked.value) return
    resultMessage.value=result.message
    if (result.outcome!=='unknown') rememberPending(null)
    if (result.outcome==='rejected') ElMessage.error(result.message)
    else if (result.outcome!=='unknown') { ElMessage.success('模拟委托已受理');size.value='' }
    await refresh(true)
  } catch (err) {
    if (!disposed && unlocked.value) {
      if (err instanceof AccountError && [400,401,403,422].includes(err.status)) rememberPending(null)
      resultMessage.value=err instanceof Error ? err.message:'提交结果待确认，请查询委托状态。'
      if (err instanceof AccountError && [401,403].includes(err.status)) lock()
    }
  } finally { submitting.value=false }
}
async function resolvePending() {
  if (!pending.value || submitting.value || !unlocked.value) return
  submitting.value=true
  try {
    const params=new URLSearchParams(pending.value)
    const result=await demoRequest<DemoResult>(token,`order?${params}`,undefined,AbortSignal.timeout(20000))
    if (disposed || !unlocked.value) return
    resultMessage.value=result.message+(result.state ? ` ${stateNames[result.state] ?? result.state}`:'')
    if (result.outcome==='confirmed') rememberPending(null)
    await refresh(true)
  } catch (err) { resultMessage.value=err instanceof Error ? err.message:'查询失败，请稍后重试。' }
  finally { submitting.value=false }
}
async function releasePending() {
  try {
    await ElMessageBox.confirm('请先在欧易模拟盘核对该委托。解除后可提交新的模拟委托，原委托可能已经成交。','解除待确认状态',
      {confirmButtonText:'我已核对',cancelButtonText:'继续查询',type:'warning'})
    rememberPending(null);resultMessage.value='已解除待确认状态。'
  } catch { /* Preserve the unresolved request. */ }
}
async function cancelOrder(row:AccountRow) {
  if (canceling.value || !unlocked.value) return
  canceling.value=row.ordId!
  try {
    try { await ElMessageBox.confirm(`撤销 ${row.instId} 的模拟${row.side==='buy'?'买入':'卖出'}委托？`,'确认模拟撤单',{confirmButtonText:'撤销模拟委托',cancelButtonText:'保留委托',type:'info'}) }
    catch { return }
    if (disposed || !unlocked.value) return
    mutation=new AbortController()
    const result=await demoRequest<DemoResult>(token,'cancel',{instId:row.instId,ordId:row.ordId},AbortSignal.any([mutation.signal,AbortSignal.timeout(30000)]))
    if (disposed || !unlocked.value) return
    resultMessage.value=result.message
    ElMessage.info(result.outcome==='unknown' ? '撤单结果待确认，请刷新委托':'撤单请求已受理，请查看委托状态')
    await refresh(true)
  } catch (err) { if (!disposed && unlocked.value) resultMessage.value=err instanceof Error ? err.message:'撤单结果待确认，请刷新委托。' }
  finally { canceling.value='' }
}
onMounted(loadStatus)
onBeforeUnmount(()=>{ disposed=true;lock() })
</script>

<template>
  <section id="demo-account" class="demo-account panel">
    <div class="demo-heading"><el-icon><Wallet/></el-icon><h2>模拟盘账户</h2><span class="demo-badge">DEMO · 虚拟资金</span><div class="demo-heading-actions"><template v-if="unlocked"><span class="demo-updated">{{ account ? time(account.fetchedAt):'连接中' }} · 15s</span><button class="icon-button" aria-label="刷新模拟账户" :disabled="busy" @click="refresh(true)"><el-icon><Refresh/></el-icon></button><button class="text-button" @click="lock"><el-icon><Lock/></el-icon>锁定</button></template><button v-else class="text-button" :disabled="busy" @click="loadStatus">检查连接</button></div></div>
    <div v-if="!unlocked" class="demo-connect">
      <div class="demo-intro"><strong>连接你的欧易模拟账户</strong><p>资产与持仓、现货买卖、委托管理和成交记录，一处查看。</p><span>API 密钥由服务器保管；仅连接模拟环境。</span></div>
      <div class="demo-connect-action">
        <p v-if="statusError" class="demo-error">{{ statusError }}</p>
        <p v-else-if="!status" class="demo-muted">正在检查账户配置…</p>
        <template v-else-if="!configured"><span class="demo-status-label">等待配置</span><p>请先在服务器配置模拟盘专用 API Key，并开启读取与交易权限。</p></template>
        <form v-else-if="secure" @submit.prevent="unlock"><label for="demo-access">账户访问口令</label><div class="demo-unlock-row"><input id="demo-access" v-model="password" type="password" autocomplete="off" placeholder="输入服务器设置的访问口令" maxlength="256"/><button class="demo-primary" :disabled="busy || !password.trim()">解锁模拟盘</button></div></form>
        <template v-else><span class="demo-status-label">账户已配置 · 等待安全连接</span><p>请通过 HTTPS，或在本机终端运行下方 SSH 转发后访问 <a href="http://127.0.0.1:18000">本地工作台</a>。</p><code>ssh -N -L 127.0.0.1:18000:127.0.0.1:18080 ubuntu</code></template>
      </div>
    </div>
    <div v-if="error" class="demo-alert" role="alert">{{ error }}<span v-if="account">保留上次数据，更新于 {{ time(account.fetchedAt) }}</span></div>
    <template v-if="unlocked">
      <div v-if="!account" class="demo-loading">{{ busy?'正在连接欧易模拟盘…':'暂未取得账户数据' }}<button v-if="!busy" class="text-button" @click="refresh(true)">重试</button></div>
      <template v-else>
        <div class="demo-summary"><div><span>账户总权益（USD 估值）</span><strong>{{ decimal(account.totalEquity,2) }}</strong></div><div><span>{{ instrument.base }} 可用</span><strong>{{ decimal(account.balances.find(r=>r.ccy===instrument.base)?.availBal) }}</strong></div><div><span>{{ instrument.quote }} 可用</span><strong>{{ decimal(account.balances.find(r=>r.ccy===instrument.quote)?.availBal) }}</strong></div><span class="demo-summary-note">模拟现货 · 无杠杆</span></div>
        <div class="demo-trade-layout">
          <form class="demo-order-form" @submit.prevent="submitOrder"><div class="demo-side"><button type="button" :class="{selected:side==='buy'}" @click="side='buy'">买入 {{ instrument.base }}</button><button type="button" :class="{selected:side==='sell',sell:true}" @click="side='sell'">卖出 {{ instrument.base }}</button></div><div class="demo-form-title"><strong>{{ instrument.base }}/{{ instrument.quote }}</strong><select v-model="orderType" aria-label="模拟委托类型"><option value="limit">限价委托</option><option value="market">市价委托</option></select></div><div v-if="orderType==='limit'" class="demo-field"><span><label for="demo-price">价格</label> <button type="button" class="text-button" @click="useLast">使用最新价</button></span><div><input id="demo-price" v-model="orderPrice" inputmode="decimal" aria-label="模拟委托价格" placeholder="输入委托价格" maxlength="48"/><span>{{ instrument.quote }}</span></div></div><div v-else class="demo-market-note">以市场可成交价格买卖，实际成交价可能变化。</div><label class="demo-field"><span>数量 <small>最小 {{ instrument.minSize }} · 步长 {{ instrument.lotSize }}</small></span><div><input v-model="size" inputmode="decimal" aria-label="模拟委托数量" placeholder="输入币数量" maxlength="48"/><span>{{ instrument.base }}</span></div></label><div class="demo-form-meta"><span>可用 {{ decimal(available) }} {{ side==='buy'?instrument.quote:instrument.base }}</span><span>预估 {{ price(estimated || null,2) }} {{ instrument.quote }}</span></div><button class="demo-submit" :class="{sell:side==='sell'}" :disabled="!valid || submitting || !!pending || !!canceling">{{ submitting?'处理中…':`模拟${side==='buy'?'买入':'卖出'} ${instrument.base}` }}</button><p class="demo-form-note">数量均以 {{ instrument.base }} 计；余额及精度以欧易校验为准。</p></form>
          <div class="demo-order-context"><span class="eyebrow">YOUR DEMO WORKSPACE</span><h3>在真实行情中，练习你的交易。</h3><p>所有订单使用欧易模拟资金。支持现货限价与市价委托，资产和委托每 15 秒更新。</p><div><span>当前委托</span><strong>{{ account.pending?.length ?? '—' }}</strong></div><div><span>近期成交</span><strong>{{ account.fills?.length ?? '—' }}</strong></div><p class="demo-muted">历史委托展示近 7 日最近 50 条；成交记录展示近 3 日最近 50 条。</p></div>
        </div>
        <div v-if="pending" class="demo-pending"><strong>委托结果待确认 · {{ pending.instId }}</strong><code>{{ pending.clOrdId }}</code><div><button class="text-button" :disabled="submitting" @click="resolvePending">查询委托状态</button><button class="text-button" :disabled="submitting" @click="releasePending">已在欧易核对</button></div></div>
        <div v-if="resultMessage" class="demo-result" role="status">{{ resultMessage }}</div>
        <div v-for="warning in account.warnings" :key="warning.section" class="demo-alert">{{ warning.section }}：{{ warning.message }}</div>
        <div class="demo-data-tabs"><button v-for="item in tabs" :key="item.id" :class="{active:tab===item.id}" @click="tab=item.id">{{ item.label }}<small v-if="item.id==='pending'">{{ account.pending?.length ?? '—' }}</small></button><label v-if="tab!=='balances'"><input v-model="onlyPair" type="checkbox"/>只看当前币对</label></div>
        <div class="demo-table-scroll">
          <table v-if="rows.length" class="demo-table">
            <thead><tr v-if="tab==='balances'"><th>币种</th><th>权益</th><th>可用</th><th>冻结</th><th>USD 估值</th></tr><tr v-else-if="tab==='positions'"><th>品种 / 方向</th><th>持仓数量</th><th>开仓均价</th><th>标记价格</th><th>未实现盈亏</th></tr><tr v-else-if="tab==='fills'"><th>时间 / 币对</th><th>方向</th><th>成交价格</th><th>成交数量</th><th>手续费</th></tr><tr v-else><th>时间 / 币对</th><th>方向 / 类型</th><th>委托价格</th><th>数量 / 已成交</th><th>状态</th><th v-if="tab==='pending'">操作</th></tr></thead>
            <tbody><tr v-for="(row,index) in rows" :key="row.ordId ? `${row.ordId}:${row.tradeId ?? ''}`:row.ccy ?? row.instId ?? index">
              <template v-if="tab==='balances'"><td><strong>{{ row.ccy }}</strong></td><td>{{ decimal(row.eq) }}</td><td>{{ decimal(row.availBal) }}</td><td>{{ decimal(row.frozenBal) }}</td><td>{{ decimal(row.eqUsd,2) }}</td></template>
              <template v-else-if="tab==='positions'"><td><strong>{{ row.instId }}</strong><small>{{ row.posSide==='net'?'净持仓':row.posSide==='long'?'多仓':'空仓' }}</small></td><td>{{ decimal(row.pos) }} {{ row.posCcy }}</td><td>{{ decimal(row.avgPx) }}</td><td>{{ decimal(row.markPx) }}</td><td :class="Number(row.upl)>=0?'up':'down'">{{ decimal(row.upl) }}</td></template>
              <template v-else-if="tab==='fills'"><td><strong>{{ row.instId }}</strong><small>{{ stamp(row.fillTime || row.ts) }}</small></td><td :class="row.side==='buy'?'up':'down'">{{ row.side==='buy'?'买入':'卖出' }}</td><td>{{ decimal(row.fillPx) }}</td><td>{{ decimal(row.fillSz) }}</td><td>{{ decimal(row.fee) }} {{ row.feeCcy }}</td></template>
              <template v-else><td :title="`委托号 ${row.ordId}`"><strong>{{ row.instId }}</strong><small>{{ stamp(row.cTime) }}</small></td><td :class="row.side==='buy'?'up':'down'">{{ row.side==='buy'?'买入':'卖出' }}<small>{{ row.ordType==='market'?'市价':'限价' }}</small></td><td>{{ row.ordType==='market'?'市价':decimal(row.px) }}</td><td>{{ decimal(row.sz) }}<small>已成交 {{ decimal(row.accFillSz) }}</small></td><td><span class="demo-order-state">{{ stateNames[row.state!] ?? row.state }}</span></td><td v-if="tab==='pending'"><button v-if="isOrder(row)" class="text-button" :disabled="!!canceling || submitting" @click="cancelOrder(row)">{{ canceling===row.ordId?'处理中…':'撤单' }}</button></td></template>
            </tr></tbody>
          </table><div v-else class="demo-table-empty">{{ unavailable?'这部分数据暂时不可用，请稍后刷新':tab==='positions'?'暂无衍生品持仓；现货持有数量请在资产中查看':'暂无记录' }}</div>
        </div>
      </template>
    </template>
  </section>
</template>

<style scoped>
.demo-account{scroll-margin-top:18px;overflow:hidden}.demo-heading{display:flex;align-items:center;gap:9px;min-height:48px;padding:10px 17px;border-bottom:1px solid var(--border)}.demo-heading>.el-icon{color:#dbb96d;font-size:16px}.demo-heading h2{font-size:13px;color:#dce6f4;font-weight:550}.demo-badge{color:#dbb96d;font-size:9px;border:1px solid #6a542c66;background:#59411c33;padding:3px 6px;border-radius:3px;white-space:nowrap}.demo-heading-actions{display:flex;align-items:center;gap:10px;margin-left:auto}.demo-heading-actions .text-button{gap:5px}.demo-updated{font-size:9px;color:#6e86a5}.demo-connect{display:grid;grid-template-columns:1fr 1fr;gap:25px;padding:25px 20px}.demo-intro strong{font-size:16px;color:#d2dfed;font-weight:500}.demo-connect p{font-size:11px;color:#8aa0bc;line-height:1.9;margin:10px 0}.demo-intro>span{font-size:10px;color:#557395}.demo-connect-action{align-self:center;font-size:11px;min-width:0}.demo-status-label{color:#d3b370;font-size:10px}.demo-connect-action>code{display:block;overflow:auto;padding:9px;background:#0a1320;border:1px solid #26364d;border-radius:4px;color:#91a8c5;font-size:10px;white-space:nowrap}.demo-connect a{color:var(--accent)}.demo-connect form>label{display:block;font-size:10px;color:#7c96b4;margin-bottom:8px}.demo-unlock-row{display:flex;gap:8px}.demo-unlock-row input{min-width:0;flex:1;border:1px solid #304360;background:#101d30;color:#c5d6ec;border-radius:4px;padding:11px 10px;font-size:11px;outline:none}.demo-unlock-row input:focus{border-color:#8dbb60}.demo-primary{background:var(--accent);color:#182b1d;padding:10px 12px;border-radius:4px;white-space:nowrap;font-size:11px;font-weight:600}.demo-muted{color:#5f799a!important}.demo-error{color:#ee8f9d!important}.demo-alert{padding:10px 17px;color:#d9b77e;background:#5c432722;font-size:11px;line-height:1.8;display:flex;gap:12px;flex-wrap:wrap}.demo-alert>span{color:#7a8ca6}.demo-loading{padding:30px;display:flex;align-items:center;justify-content:center;gap:12px;font-size:11px;color:#768fac}.demo-summary{display:flex;align-items:center;gap:36px;padding:20px;border-bottom:1px solid var(--border)}.demo-summary>div{display:flex;flex-direction:column;gap:10px;min-width:0}.demo-summary>div>span{font-size:9px;color:#6f89a9}.demo-summary strong{font-size:18px;font-weight:500;color:#d0dfef;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.demo-summary>div:first-child strong{font-size:23px}.demo-summary-note{margin-left:auto;color:#907c54;font-size:9px;white-space:nowrap}.demo-trade-layout{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:32px;padding:20px}.demo-order-form{min-width:0}.demo-side{display:flex;gap:5px;background:#0c1728;border-radius:5px;padding:4px;margin-bottom:15px}.demo-side>button{flex:1;border-radius:3px;padding:8px;font-size:12px;color:#647e9f}.demo-side>button.selected{color:var(--up);background:#18463966}.demo-side>button.selected.sell{color:var(--down);background:#60303b55}.demo-form-title{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#b7cce6;margin-bottom:15px}.demo-form-title select{font-size:10px;padding:5px;background:#102035;border:1px solid #263f5d;border-radius:4px}.demo-field{display:block;margin-bottom:12px}.demo-field>span{font-size:10px;color:#8ba3c2;display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:7px}.demo-field small{font-size:8px;color:#566f91}.demo-field>div{display:flex;align-items:center;gap:8px;padding:0 11px;height:40px;border:1px solid #2a3d56;background:#0c192a;border-radius:4px}.demo-field>div:focus-within{border-color:#71974f}.demo-field input{width:100%;min-width:0;border:0;background:none;color:#d1e1f3;outline:none;font-size:13px;font-variant-numeric:tabular-nums}.demo-field>div>span{color:#6b84a5;font-size:11px}.demo-form-meta{display:flex;justify-content:space-between;gap:10px;color:#6c86a6;font-size:9px;margin:14px 0}.demo-submit{width:100%;background:#21b993;color:#062d25;border-radius:4px;padding:12px;font-weight:600;font-size:12px}.demo-submit.sell{background:#e66c80;color:#340e18}.demo-submit:disabled,.demo-primary:disabled{opacity:.4;cursor:not-allowed}.demo-form-note{font-size:8px;color:#536f92;text-align:center;line-height:1.8;margin-top:9px}.demo-market-note{padding:15px 11px;margin-bottom:12px;font-size:10px;background:#1b2a3d;color:#869cb9;border-radius:4px;min-height:64px;line-height:1.8}.demo-order-context{padding:18px 0 0 25px;border-left:1px solid #21334c;min-width:0}.demo-order-context h3{font-size:16px;font-weight:500;line-height:1.8;color:#b9cde4;margin:12px 0}.demo-order-context>p{font-size:10px;color:#718bab;line-height:1.9;margin:10px 0 20px}.demo-order-context>div{display:flex;justify-content:space-between;align-items:center;color:#6e8aac;font-size:10px;padding:13px 0;border-bottom:1px solid #203149}.demo-order-context>div>strong{font-size:16px;color:#bdd0e7;font-weight:500}.demo-pending{padding:13px 20px;background:#58432033;border-block:1px solid #77603455;display:flex;flex-wrap:wrap;align-items:center;gap:10px}.demo-pending>strong{font-size:11px;font-weight:500;color:#d8b574}.demo-pending>code{font-size:9px;color:#9f947d;overflow-wrap:anywhere}.demo-pending>div{display:flex;gap:14px}.demo-result{padding:10px 20px;font-size:11px;line-height:1.9;color:#a9bfdb;background:#22384c44}.demo-data-tabs{display:flex;align-items:stretch;gap:21px;padding:0 18px;min-height:43px;border-block:1px solid var(--border);overflow:auto}.demo-data-tabs>button{color:#7890ae;font-size:11px;white-space:nowrap;position:relative;padding:12px 0}.demo-data-tabs>button.active{color:#d1e2f5}.demo-data-tabs>button.active:after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px;background:var(--accent)}.demo-data-tabs small{font-size:8px;padding:2px 4px;margin-left:5px;background:#23354c;color:#92adc9;border-radius:3px}.demo-data-tabs>label{margin-left:auto;display:flex;align-items:center;gap:4px;white-space:nowrap;color:#6983a6;font-size:9px}.demo-data-tabs input{accent-color:#9abf6e;width:11px;height:11px}.demo-table-scroll{overflow:auto;max-height:300px;min-height:120px}.demo-table{width:100%;border-collapse:collapse;text-align:right;font-size:10px;font-variant-numeric:tabular-nums;white-space:nowrap}.demo-table th{color:#587799;font-size:9px;font-weight:400;padding:12px 16px;background:#0f1c2d;position:sticky;top:0}.demo-table td{padding:12px 16px;border-top:1px solid #1b2c42;color:#9fb5d1}.demo-table td:first-child,.demo-table th:first-child{text-align:left}.demo-table td.up{color:var(--up)}.demo-table td.down{color:var(--down)}.demo-table td>strong{font-weight:500;color:#b7cee7}.demo-table td small{display:block;font-size:8px;color:#527498;margin-top:6px}.demo-table td .text-button{margin-left:auto}.demo-table-empty{padding:45px 20px;text-align:center;font-size:11px;line-height:1.8;color:#5b789b}.demo-order-state{padding:3px 5px;border-radius:3px;background:#20354b;color:#8eaacb;font-size:9px}
@media(max-width:1350px){.demo-summary{gap:24px}.demo-summary-note{display:none}.demo-summary strong{font-size:15px}.demo-summary>div:first-child strong{font-size:19px}.demo-trade-layout{gap:20px}.demo-order-context{padding-left:20px}.demo-data-tabs{gap:16px}.demo-heading{padding-inline:14px}.demo-connect{grid-template-columns:1fr;gap:14px;padding:22px 16px}.demo-connect p{margin-block:7px}.demo-updated{display:none}}
@media(max-width:760px){.demo-trade-layout{grid-template-columns:1fr;padding:17px;gap:0}.demo-order-context{display:none}.demo-summary{gap:20px;padding:18px 15px;flex-wrap:wrap}.demo-summary>div:first-child{flex-basis:100%}.demo-summary>div:first-child strong{font-size:24px}.demo-summary strong{font-size:17px}.demo-data-tabs{gap:20px;padding-inline:15px}.demo-data-tabs>label{display:none}.demo-heading{gap:6px;padding-inline:13px}.demo-heading h2{font-size:12px}.demo-heading-actions{gap:6px}.demo-badge{font-size:8px;padding-inline:4px}.demo-connect{padding:20px 16px}.demo-unlock-row{flex-wrap:wrap}.demo-unlock-row input{flex-basis:100%}.demo-primary{width:100%}.demo-field small{font-size:9px}.demo-form-meta{font-size:10px}.demo-form-note{font-size:9px}.demo-submit{font-size:13px}.demo-table td,.demo-table th{padding-inline:14px}}
</style>
