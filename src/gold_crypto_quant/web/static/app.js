const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const money = (value, digits = 2) => Number(value || 0).toLocaleString("zh-CN", {minimumFractionDigits: digits, maximumFractionDigits: digits});
const pct = (value) => `${Number(value || 0) >= 0 ? "+" : ""}${(Number(value || 0) * 100).toFixed(2)}%`;
const localTime = (value, withDate = false) => value ? new Date(value).toLocaleString("zh-CN", withDate ? {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"} : {hour:"2-digit",minute:"2-digit"}) : "—";
const trendClass = (value) => Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "";

async function getJSON(url) {
  const response = await fetch(url, {headers:{Accept:"application/json"}, credentials:"same-origin"});
  if (response.status === 401) { window.location.href = "/login"; throw new Error("Unauthorized"); }
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

// 统一刷新调度：监管页面必须让失败可见，静默失败会让人误以为看到的是最新数据。
const REFRESH_BACKOFF_CEILING = 300000;
let refreshFailures = 0;
let lastSuccessLabel = "";
let refreshTimer = null;

const clockText = () => new Date().toLocaleTimeString("zh-CN", {hour:"2-digit",minute:"2-digit",second:"2-digit"});

function showFreshness(ok, text) {
  const element = $("#data-freshness");
  if (!element) return;
  element.classList.toggle("stale", !ok);
  element.textContent = text;
}

async function runRefresh(task) {
  try {
    await task();
    refreshFailures = 0;
    lastSuccessLabel = clockText();
    showFreshness(true, `更新于 ${lastSuccessLabel}`);
    return true;
  } catch (error) {
    // 401已经在getJSON里跳转登录页，不再当成刷新失败提示。
    if (error && error.message === "Unauthorized") return false;
    refreshFailures += 1;
    console.error(error);
    showFreshness(
      false,
      `⚠ 数据刷新失败（连续 ${refreshFailures} 次）· ${lastSuccessLabel ? `上次成功 ${lastSuccessLabel}` : "尚未取到数据"}`,
    );
    return false;
  }
}

// 用setTimeout递归代替setInterval，失败时指数退避，避免后端异常时每10秒空打一次。
function startPolling(task, baseInterval) {
  const tick = async () => {
    await runRefresh(task);
    const delay = refreshFailures
      ? Math.min(baseInterval * 2 ** refreshFailures, REFRESH_BACKOFF_CEILING)
      : baseInterval;
    refreshTimer = window.setTimeout(tick, delay);
  };
  if (refreshTimer) window.clearTimeout(refreshTimer);
  tick();
}

function accountCard(account, detailed = false) {
  const health = account.health || {status:"WAITING",healthy:0,total:0};
  const positionText = account.positions.length ? account.positions.map(p => `${p.symbol.replace("_USDT","")} ${p.side === "LONG" ? "多" : "空"}`).join(" · ") : "无持仓";
  const boxes = account.active_boxes.length ? account.active_boxes.join(" · ") : "等待箱体";
  const positions = detailed ? `<div class="position-list">${account.positions.length ? account.positions.map(p => `<div class="position-row"><span>${esc(p.symbol)} · ${esc(p.interval)} · ${p.side === "LONG" ? "做多" : "做空"}</span><strong>${money(p.entry_price)} → 止损 ${money(p.stop_price)}</strong></div>`).join("") : `<div class="empty-state">当前没有开放仓位</div>`}</div>` : "";
  return `<article class="account-card" style="--account-color:${esc(account.color)}">
    <div class="account-top"><div class="exchange-name"><span class="exchange-icon">${esc(account.label.slice(0,1))}</span><span><strong>${esc(account.name)}</strong><small>系统实验 · SHADOW</small></span></div><span class="health-pill ${health.status !== "HEALTHY" ? "degraded" : ""}"><span class="status-dot ${health.status === "HEALTHY" ? "green" : "red"}"></span>${esc(health.status)} ${health.healthy}/${health.total}</span></div>
    <div class="equity-value">${money(account.equity)} <span>USDT</span></div>
    <div class="pnl-row"><strong class="${trendClass(account.pnl)}">${account.pnl >= 0 ? "+" : ""}${money(account.pnl)} U</strong><span class="${trendClass(account.return_rate)}">${pct(account.return_rate)}</span><span>累计</span></div>
    <div class="account-facts"><div><span>当前持仓</span><strong>${esc(positionText)}</strong></div><div><span>震荡周期</span><strong>${esc(boxes)}</strong></div><div><span>最大回撤</span><strong>${(account.drawdown * 100).toFixed(2)}%</strong></div></div>${positions}
  </article>`;
}

function renderEquityChart(data) {
  const element = $("#equity-chart");
  if (!element || typeof echarts === "undefined") return;
  const chart = echarts.init(element, null, {renderer:"canvas"});
  const colors = {Gate:"#0071e3","币安":"#af52de"};
  const series = Object.entries(data.equity_series).map(([name, rows]) => ({name,type:"line",showSymbol:false,smooth:.25,lineStyle:{width:2},areaStyle:{opacity:.045},data:rows.map(row => [row.time,row.value])}));
  chart.setOption({color:Object.keys(data.equity_series).map(name => colors[name]),animationDuration:500,grid:{left:56,right:20,top:38,bottom:40},legend:{top:4,textStyle:{color:"#86868b"}},tooltip:{trigger:"axis",valueFormatter:v=>`${money(v)} U`},xAxis:{type:"time",axisLine:{lineStyle:{color:"#d2d2d7"}},axisLabel:{color:"#86868b",fontSize:10}},yAxis:{type:"value",scale:true,axisLabel:{color:"#86868b",fontSize:10,formatter:v=>money(v,0)},splitLine:{lineStyle:{color:"rgba(128,128,128,.12)"}}},series});
  window.addEventListener("resize",()=>chart.resize(),{passive:true});
}

async function loadOverview() {
  // 不在此处吞掉异常：由 runRefresh 统一记账，否则失败无法触发退避和页面告警。
  const data = await getJSON("/api/overview");
  const cards = $("#account-cards"); if (cards) cards.innerHTML = data.accounts.map(a => accountCard(a)).join("");
  const detail = $("#accounts-detail"); if (detail) detail.innerHTML = data.accounts.map(a => accountCard(a,true)).join("");
  if ($("#last-refresh")) $("#last-refresh").textContent = `更新于 ${localTime(data.generated_at)}`;
  const stats = $("#summary-stats");
  if (stats && data.accounts.length >= 2) {
    const diff = data.accounts[0].equity - data.accounts[1].equity;
    const open = data.accounts.reduce((n,a)=>n+a.positions.length,0);
    const trades = data.accounts.reduce((n,a)=>n+a.closed_trades,0);
    stats.innerHTML = `<article class="metric-card"><span>账户权益差</span><strong class="${trendClass(diff)}">${diff>=0?"+":""}${money(diff)} U</strong><small>Gate − 币安</small></article><article class="metric-card"><span>开放持仓</span><strong>${open}</strong><small>两个账户合计</small></article><article class="metric-card"><span>已完成交易</span><strong>${trades}</strong><small>持久化记录</small></article><article class="metric-card"><span>真实交易</span><strong class="safe-text">关闭</strong><small>订单提交不可用</small></article>`;
  }
  const events = $("#recent-events");
  if (events) events.innerHTML = data.recent_events.length ? data.recent_events.map(event => `<div class="event-item"><span class="event-dot ${event.severity !== "INFO" ? "warning" : ""}"></span><div class="event-copy"><strong>${esc(event.exchange)} · ${esc(event.symbol)} · ${esc(event.title)}</strong><small>${esc(event.interval)} · ${esc(event.details["本次净盈亏"] || event.details["原因"] || "影子策略事件")}</small></div><time class="event-time">${localTime(event.time)}</time></div>`).join("") : `<div class="empty-state">暂无交易事件，策略正在等待有效触轨。</div>`;
  renderEquityChart(data);
}

// 盘口来自采集器写入的秒级聚合表，不是浏览器直连交易所——展示的是"最近一秒的极值"。
// 每行显式给出数据年龄，采集器挂掉时页面必须看得出来，而不是继续显示几小时前的价格。
function quoteSide(side){
  if(!side) return `<span class="empty-state">无数据</span>`;
  const stale = side.stale ? ` <span class="negative">⚠${side.age_seconds}s</span>` : "";
  return `${money(side.bid)} / ${money(side.ask)}${stale}`;
}
async function loadQuotes(){
  const data = await getJSON("/api/quotes");
  const ages = data.quotes.flatMap(q => Object.values(q.venues).filter(Boolean).map(v => v.age_seconds));
  const worst = ages.length ? Math.max(...ages) : null;
  const badge = $("#quotes-age");
  if(badge){
    badge.textContent = worst === null ? "采集器无数据" : `数据延迟 ${worst.toFixed(1)} 秒`;
    badge.classList.toggle("stale", worst === null || worst > 10);
  }
  const body = $("#quotes-table tbody");
  if(!body) return;
  body.innerHTML = data.quotes.map(q => {
    const basis = q.basis === null
      ? `<span class="empty-state">—</span>`
      : `<strong class="${trendClass(q.basis)}">${q.basis >= 0 ? "+" : ""}${money(q.basis, 4)}</strong> <span class="${trendClass(q.basis)}">${pct(q.basis_rate)}</span>`;
    const frames = Object.entries(q.venues)
      .map(([label, side]) => `${esc(label)} ${side ? side.frame_count.toLocaleString("zh-CN") : "0"}`)
      .join(" · ");
    return `<tr><td><strong>${esc(q.symbol.replace("_USDT",""))}</strong></td>
      <td>${quoteSide(q.venues["Gate"])}</td>
      <td>${quoteSide(q.venues["币安"])}</td>
      <td>${basis}</td>
      <td><small>${frames}</small></td></tr>`;
  }).join("");
}

let marketChart;
async function loadMarket() {
  const venue = $("#market-venue")?.value, symbol = $("#market-symbol")?.value, interval = $("#market-interval")?.value;
  if (!venue) return;
  const data = await getJSON(`/api/market?venue=${encodeURIComponent(venue)}&symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}`);
  $("#market-title").textContent = `${symbol.replace("_","/")} · ${data.exchange}`;
  $("#market-subtitle").textContent = `${interval} · 已收盘K线 · 北京时间`;
  if (data.latest) $("#market-price").textContent = money(data.latest.close);
  const latest = data.latest;
  if (latest) {
    const location = latest.close >= latest.middle ? (latest.close >= latest.upper ? "触及/突破上轨" : "中轨与上轨之间") : (latest.close <= latest.lower ? "触及/突破下轨" : "中轨与下轨之间");
    $("#market-decision").innerHTML = `<div><dt>震荡判断</dt><dd class="${latest.box_active ? "positive" : ""}">${latest.box_active ? "三轨走平 · 成立" : "未确认"}</dd></div><div><dt>价格位置</dt><dd>${location}</dd></div><div><dt>上轨</dt><dd>${money(latest.upper)}</dd></div><div><dt>中轨</dt><dd>${money(latest.middle)}</dd></div><div><dt>下轨</dt><dd>${money(latest.lower)}</dd></div>`;
  }
  if (typeof echarts === "undefined") return;
  if (!marketChart) marketChart = echarts.init($("#market-chart"),null,{renderer:"canvas"});
  const rows=data.rows, times=rows.map(r=>new Date(r.time).toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}));
  marketChart.setOption({animation:false,color:["#0071e3","#ff453a","#8e8e93","#30d158"],legend:{top:3,data:["K线","上轨","中轨","下轨"],textStyle:{color:"#86868b"}},tooltip:{trigger:"axis",axisPointer:{type:"cross"}},grid:[{left:60,right:24,top:42,bottom:105},{left:60,right:24,height:55,bottom:35}],xAxis:[{type:"category",data:times,boundaryGap:true,axisLabel:{color:"#86868b",fontSize:9}},{type:"category",gridIndex:1,data:times,axisLabel:{show:false}}],yAxis:[{scale:true,axisLabel:{color:"#86868b",fontSize:9},splitLine:{lineStyle:{color:"rgba(128,128,128,.12)"}}},{gridIndex:1,scale:true,axisLabel:{show:false},splitLine:{show:false}}],dataZoom:[{type:"inside",xAxisIndex:[0,1],start:40,end:100},{type:"slider",xAxisIndex:[0,1],bottom:6,height:20}],series:[{name:"K线",type:"candlestick",data:rows.map(r=>[r.open,r.close,r.low,r.high]),itemStyle:{color:"#30d158",color0:"#ff453a",borderColor:"#30d158",borderColor0:"#ff453a"}},{name:"上轨",type:"line",data:rows.map(r=>r.upper),showSymbol:false,lineStyle:{width:1.2}},{name:"中轨",type:"line",data:rows.map(r=>r.middle),showSymbol:false,lineStyle:{width:1.1}},{name:"下轨",type:"line",data:rows.map(r=>r.lower),showSymbol:false,lineStyle:{width:1.2}},{name:"成交量",type:"bar",xAxisIndex:1,yAxisIndex:1,data:rows.map(r=>r.volume),itemStyle:{color:"rgba(0,113,227,.28)"}}]});
}

let tradePage=1;
const TRADE_PAGE_SIZE=20;
async function loadTrades(page=tradePage) {
  const venue=$("#trade-venue")?.value||"",symbol=$("#trade-symbol")?.value||"";
  const data=await getJSON(`/api/trades?venue=${encodeURIComponent(venue)}&symbol=${encodeURIComponent(symbol)}&page=${page}&page_size=${TRADE_PAGE_SIZE}`);
  tradePage=data.page;
  const rows=data.items;
  $("#trade-count").textContent=`${data.total} 条`;
  $("#trade-body").innerHTML=rows.length?rows.map(row=>{const result=row.details["本次净盈亏"]||row.details["整笔累计净盈亏"]||"—";return `<tr><td>${localTime(row.time,true)}</td><td><span class="exchange-chip ${row.exchange==="币安"?"binance":""}">${esc(row.exchange)}</span></td><td>${esc(row.symbol)}</td><td>${esc(row.interval)}</td><td class="event-title-cell"><strong>${esc(row.title)}</strong><small>${esc(row.details["原因"]||row.details["方向"]||row.event_type)}</small></td><td class="result-chip ${String(result).startsWith("+")?"positive":String(result).startsWith("-")?"negative":""}">${esc(result)}</td></tr>`}).join(""):`<tr><td colspan="6" class="empty-cell">暂无符合条件的交易事件</td></tr>`;
  $("#trade-page-info").textContent=`第 ${data.page} / ${data.total_pages} 页`;
  $("#trade-prev").disabled=data.page<=1;
  $("#trade-next").disabled=data.page>=data.total_pages;
}

function uptime(seconds){const d=Math.floor(seconds/86400),h=Math.floor(seconds%86400/3600),m=Math.floor(seconds%3600/60);return `${d}天 ${h}小时 ${m}分钟`;}
async function loadSystem(){const data=await getJSON("/api/system");$("#system-cards").innerHTML=`<article class="metric-card"><span>整机CPU</span><strong>${data.cpu_percent.toFixed(1)}%</strong><small>当前短采样</small></article><article class="metric-card"><span>内存</span><strong>${data.memory_percent.toFixed(1)}%</strong><small>${data.memory_used_gib.toFixed(1)} / ${data.memory_total_gib.toFixed(1)} GiB</small></article><article class="metric-card"><span>数据盘</span><strong>${data.disk_percent.toFixed(1)}%</strong><small>${data.disk_used_gib.toFixed(1)} / ${data.disk_total_gib.toFixed(1)} GiB</small></article><article class="metric-card"><span>开机时长</span><strong>${uptime(data.uptime_seconds).split(" ")[0]}</strong><small>${uptime(data.uptime_seconds)}</small></article>`;$("#process-status").innerHTML=`<div><dt>双行情服务</dt><dd class="${data.comparison_running?"positive":"negative"}">${data.comparison_running?"RUNNING":"STOPPED"}</dd></div><div><dt>双行情PID</dt><dd>${data.comparison_pid||"—"}</dd></div><div><dt>Web监管PID</dt><dd>${data.web_pid}</dd></div><div><dt>主机</dt><dd>${esc(data.hostname)}</dd></div>`;}

document.addEventListener("DOMContentLoaded",()=>{
  const page=document.body.dataset.page;
  // 手动触发也走同一套记账，点刷新失败时同样会在页面上报错而不是静默。
  const manual=task=>()=>runRefresh(task);
  if(page==="dashboard"){startPolling(loadOverview,10000);}
  if(page==="accounts"){startPolling(loadOverview,15000);}
  if(page==="market"){
    // 盘口每秒更新，K线30秒才变一次；合并成一个任务，按盘口的节奏刷新。
    startPolling(async()=>{await Promise.all([loadMarket(),loadQuotes()]);},5000);
    $("#market-refresh").addEventListener("click",manual(loadMarket));
    ["#market-venue","#market-symbol","#market-interval"].forEach(s=>$(s).addEventListener("change",manual(loadMarket)));
  }
  if(page==="trades"){
    const reload=manual(()=>loadTrades(1));
    runRefresh(()=>loadTrades(1));
    $("#trade-refresh").addEventListener("click",reload);
    $("#trade-venue").addEventListener("change",reload);
    $("#trade-symbol").addEventListener("change",reload);
    $("#trade-prev").addEventListener("click",manual(()=>loadTrades(tradePage-1)));
    $("#trade-next").addEventListener("click",manual(()=>loadTrades(tradePage+1)));
  }
  if(page==="system"){startPolling(loadSystem,10000);}
});
