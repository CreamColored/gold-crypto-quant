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

// 价格由浏览器直连交易所的永续 bookTicker 推送。
// 注意这条链路与策略无关：策略用的是采集器落库的数据，采集器停了这里照常跳动，
// 页面看不出来。采集器的健康状况需要另找地方观察。
const QUOTE_WS = {
  GATE_LIVE_PUBLIC: {
    url: () => "wss://fx-ws.gateio.ws/v4/ws/usdt",
    subscribe: (symbol) => ({time: Math.floor(Date.now()/1000), channel: "futures.book_ticker", event: "subscribe", payload: [symbol]}),
    parse: (msg) => (msg.event === "update" && msg.result && msg.result.b)
      ? {bid: Number(msg.result.b), ask: Number(msg.result.a)} : null,
  },
  BINANCE_LIVE_PUBLIC: {
    url: (symbol) => `wss://fstream.binance.com/ws/${symbol.replace("_","").toLowerCase()}@bookTicker`,
    subscribe: null,
    parse: (msg) => msg && msg.b ? {bid: Number(msg.b), ask: Number(msg.a)} : null,
  },
};
let quoteSocket = null, quoteFrames = 0, quoteLastMid = null, quoteRenderTimer = null, quoteRetry = 0;

function quoteDigits(price){ return price >= 1000 ? 1 : 2; }

function setStreamState(ok, text){
  const element = $("#quote-stream-state");
  if(element) element.innerHTML = `<span class="stream-dot ${ok ? "" : "down"}"></span>${esc(text)}`;
}

function renderDirect(bid, ask){
  const box = $("#quote-direct");
  if(!box) return;
  const mid = (bid + ask) / 2, digits = quoteDigits(mid);
  const direction = quoteLastMid === null ? 0 : mid - quoteLastMid;
  quoteLastMid = mid;
  box.innerHTML = `
    <div class="quote-mid ${trendClass(direction)}">${money(mid, digits)}</div>
    <div class="quote-rows">
      <div><span>卖一</span><strong class="negative">${money(ask, digits)}</strong></div>
      <div><span>买一</span><strong class="positive">${money(bid, digits)}</strong></div>
      <div><span>价差</span><strong>${money(ask - bid, digits)}</strong></div>
    </div>`;
  const badge = $("#market-price");
  if(badge) badge.textContent = money(mid, digits);
}

function stopQuoteStream(){
  if(quoteRenderTimer){ window.clearInterval(quoteRenderTimer); quoteRenderTimer = null; }
  if(quoteSocket){
    // 先摘掉回调再关，避免主动切换品种时触发重连逻辑。
    quoteSocket.onclose = quoteSocket.onerror = quoteSocket.onmessage = null;
    try { quoteSocket.close(); } catch (error) { console.warn(error); }
    quoteSocket = null;
  }
}

function startQuoteStream(){
  stopQuoteStream();
  const venue = $("#market-venue")?.value, symbol = $("#market-symbol")?.value;
  const config = QUOTE_WS[venue];
  if(!config || !symbol) return;
  quoteLastMid = null;
  let pending = null;
  setStreamState(false, "正在连接交易所…");
  let socket;
  try { socket = new WebSocket(config.url(symbol)); }
  catch (error) { setStreamState(false, `无法连接：${error}`); return; }
  quoteSocket = socket;

  socket.onopen = () => {
    quoteRetry = 0;
    if(config.subscribe) socket.send(JSON.stringify(config.subscribe(symbol)));
  };
  socket.onmessage = (event) => {
    let parsed;
    try { parsed = config.parse(JSON.parse(event.data)); } catch { return; }
    if(!parsed) return;
    quoteFrames += 1;
    pending = parsed;   // 每秒上百帧，攒着按固定节奏渲染，不能每帧都改DOM
  };
  socket.onclose = () => {
    if(quoteSocket !== socket) return;   // 主动切换导致的关闭不重连
    setStreamState(false, "连接断开，正在重连…");
    quoteRetry += 1;
    window.setTimeout(startQuoteStream, Math.min(1000 * 2 ** quoteRetry, 20000));
  };
  socket.onerror = () => setStreamState(false, "连接异常");

  // 渲染与收帧解耦：币安每秒推数百帧，逐帧改DOM会让页面一直在重排。
  quoteRenderTimer = window.setInterval(() => {
    if(pending){ renderDirect(pending.bid, pending.ask); pending = null; }
    setStreamState(true, `直连${venue === "GATE_LIVE_PUBLIC" ? "Gate" : "币安"} · ${quoteFrames} 帧/秒`);
    quoteFrames = 0;
  }, 1000);
}


let marketChart;
async function loadMarket() {
  const venue = $("#market-venue")?.value, symbol = $("#market-symbol")?.value, interval = $("#market-interval")?.value;
  if (!venue) return;
  const data = await getJSON(`/api/market?venue=${encodeURIComponent(venue)}&symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}`);
  $("#market-title").textContent = `${symbol.replace("_","/")} · ${data.exchange}`;
  $("#market-subtitle").textContent = `${interval} · 已收盘K线 · U本位永续 · 北京时间`;
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
function renderReadiness(data){
  const box=$("#entry-readiness"); if(!box) return;
  if(!data||!data.accounts||!data.accounts.length){box.innerHTML='<p class="muted">暂无数据</p>';return;}
  // 结论用颜色区分：可开仓是绿的，熔断是红的，其余是等待类的黄。
  const tone=v=>v==="可开仓"?"ok":(v==="熔断"?"bad":(v==="持仓中"?"ok":"warn"));
  let html="";
  if(data.blackout) html+='<p class="readiness-note">宏观静默中：'+data.blackout+'</p>';
  for(const a of data.accounts){
    if(!a.available){html+='<div class="readiness-acct"><h3>'+a.label+'</h3><p class="muted">'+a.reason+'</p></div>';continue;}
    html+='<div class="readiness-acct"><h3>'+a.label+' <span class="muted">权益 '+a.equity.toFixed(2)+'U</span></h3>';
    for(const s of a.symbols){
      const ivs=Object.keys(s.boxes||{});
      const chips=ivs.map(function(i){
        const on=s.boxes[i], blk=s.blocked[i];
        const cls=blk?"chip blk":(on?"chip on":"chip");
        const tip=blk?"止损封锁中":(on?"箱体已确认":"箱体未确认");
        return '<span class="'+cls+'" title="'+tip+'">'+i+'</span>';
      }).join("");
      html+='<div class="readiness-row"><div class="readiness-head">'+
        '<span class="sym">'+s.symbol.replace("_USDT","")+'</span>'+
        '<span class="verdict '+tone(s.verdict)+'">'+s.verdict+'</span></div>'+
        '<p class="readiness-why">'+s.reason+'</p>'+
        '<div class="chips">'+chips+'</div></div>';
    }
    html+='</div>';
  }
  box.innerHTML=html;
}

async function loadSystem(){const data=await getJSON("/api/system");renderReadiness(data.entry_readiness);$("#system-cards").innerHTML=`<article class="metric-card"><span>整机CPU</span><strong>${data.cpu_percent.toFixed(1)}%</strong><small>当前短采样</small></article><article class="metric-card"><span>内存</span><strong>${data.memory_percent.toFixed(1)}%</strong><small>${data.memory_used_gib.toFixed(1)} / ${data.memory_total_gib.toFixed(1)} GiB</small></article><article class="metric-card"><span>数据盘</span><strong>${data.disk_percent.toFixed(1)}%</strong><small>${data.disk_used_gib.toFixed(1)} / ${data.disk_total_gib.toFixed(1)} GiB</small></article><article class="metric-card"><span>开机时长</span><strong>${uptime(data.uptime_seconds).split(" ")[0]}</strong><small>${uptime(data.uptime_seconds)}</small></article>`;$("#process-status").innerHTML=`<div><dt>量化服务</dt><dd class="${data.comparison_running?"positive":"negative"}">${data.comparison_running?"RUNNING":"STOPPED"}</dd></div><div><dt>量化服务PID</dt><dd>${data.comparison_pid||"—"}</dd></div><div><dt>Web监管PID</dt><dd>${data.web_pid}</dd></div><div><dt>主机</dt><dd>${esc(data.hostname)}</dd></div>`;renderCollector(data.quote_collector);}

// 采集器健康只能在这里看：行情页的价格是浏览器直连交易所的，采集器停了那边照样跳动，
// 而策略和回测用的正是采集器落库的数据。
function renderCollector(health){
  const summary=$("#collector-summary"), list=$("#collector-status");
  if(!summary||!list) return;
  if(!health){ summary.textContent="状态不可用"; list.innerHTML=""; return; }
  const ok = health.status==="HEALTHY";
  summary.innerHTML=`<span class="stream-dot ${ok?"":"down"}"></span>${esc(health.status)} · ${health.healthy}/${health.total} 条流正常`
    + (health.worst_age_seconds===null?"":` · 最新数据 ${health.worst_age_seconds} 秒前`);
  summary.classList.toggle("stale", !ok);
  list.innerHTML = health.streams.map(item=>{
    const healthy = item.status==="HEALTHY";
    const detail = item.age_seconds===null
      ? "无数据"
      : `${item.age_seconds}s 前 · ${item.frame_count.toLocaleString("zh-CN")} 帧/秒`;
    return `<div><dt>${esc(item.label)} ${esc(item.symbol.replace("_USDT",""))}</dt><dd class="${healthy?"positive":"negative"}">${esc(detail)}</dd></div>`;
  }).join("");
}

// 交易开关：总开关独占一行，交易所各成一张卡片。
// 上层关闭时下层收起而不改动状态——重新打开会回到之前配置的状态。级联写入看似直观，
// 却会把"单独禁用某个品种"的决定静默抹掉，而且抹掉在页面上完全看不出来。
function switchToggle(scope, enabled, label){
  return `<label class="ios-switch" title="${esc(label)}">
    <input type="checkbox" data-scope="${esc(scope)}" ${enabled ? "checked" : ""}
           aria-label="${esc(label)} 开仓开关">
    <span class="ios-track"><span class="ios-knob"></span></span></label>`;
}

function renderSwitches(view){
  const hint = $("#switch-hint");
  if(hint) hint.textContent = `总开关 → 交易所 → 品种，任一级关闭即禁止开仓 · 当前 ${view.allowed}/${view.total} 个品种允许开仓`;

  const globalRow = $("#switch-global");
  if(globalRow) globalRow.innerHTML =
    `<span class="switch-label">总开关<small>关闭后所有交易所、所有品种都不再开仓</small></span>
     <span class="switch-state ${view.global.enabled ? "" : "blocked"}">${view.global.enabled ? "允许开仓" : "全部停止"}</span>
     ${switchToggle(view.global.scope_key, view.global.enabled, "总开关")}`;

  const grid = $("#switch-venues");
  if(grid) grid.innerHTML = view.venues.map(venue => {
    const body = venue.collapsed
      ? `<div class="venue-collapsed">${view.global.enabled ? "该交易所已关闭" : "总开关已关闭"}，品种开关已收起<br><small>下层状态保留，重新打开即恢复</small></div>`
      : venue.symbols.map(item =>
          `<div class="switch-row">
             <span class="switch-label">${esc(item.label)}<small>${esc(item.sub)}</small></span>
             <span class="switch-state ${item.effective ? "" : "blocked"}">${item.effective ? "允许开仓" : "禁止开仓"}</span>
             ${switchToggle(item.scope_key, item.enabled, item.label)}
           </div>`).join("");
    return `<article class="venue-card">
      <div class="venue-head">
        <span class="switch-label">${esc(venue.label)}<small>该交易所全部品种</small></span>
        ${switchToggle(venue.scope_key, venue.enabled, venue.label)}
      </div>
      <div class="venue-body">${body}</div>
    </article>`;
  }).join("");

  document.querySelectorAll("#switch-global input, #switch-venues input").forEach(box => {
    box.addEventListener("change", async () => {
      box.disabled = true;
      try {
        const response = await fetch("/api/switches", {
          method: "POST",
          headers: {"Content-Type": "application/json", Accept: "application/json"},
          credentials: "same-origin",
          body: JSON.stringify({scope_key: box.dataset.scope, enabled: box.checked}),
        });
        if(response.status === 401){ window.location.href = "/login"; return; }
        if(!response.ok) throw new Error(await response.text());
        renderSwitches(await response.json());
      } catch (error) {
        console.error(error);
        // 写入失败必须回滚勾选状态，否则页面显示的和实际生效的会不一致。
        box.checked = !box.checked;
        box.disabled = false;
        showFreshness(false, "⚠ 开关保存失败，请重试");
      }
    });
  });
}
async function loadSwitches(){ renderSwitches(await getJSON("/api/switches")); }

// ---- 策略对照 v1：震荡 vs 顺势 ----
function renderCompare(data){
  const cards=$("#compare-cards"), events=$("#compare-events");
  const live=data.strategies.filter(s=>s.available);
  $("#compare-gap").textContent = data.gap===null ? "等待两个策略都产出数据"
    : `震荡 − 顺势 = ${data.gap>=0?"+":""}${money(data.gap)}U`;
  $("#compare-gap").className = "soft-pill " + (data.gap>0?"safe":data.gap<0?"warn":"");

  cards.innerHTML = data.strategies.map(s=>{
    if(!s.available) return `<article class="panel"><div class="panel-heading"><div><h2>未启动</h2>
      <p>状态文件不存在：${esc(s.path)}</p></div></div>
      <p class="muted">运行 <code>main.py strategy-comparison</code> 后这里才会有数据。</p></article>`;
    const holds = s.positions.length
      ? s.positions.map(p=>`<li><strong>${esc(p.symbol)}</strong> ${esc(p.side)}
          入场 ${money(p.entry)} · 止损 ${money(p.stop)}${p.target?` · 目标 ${money(p.target)}`:""}</li>`).join("")
      : "<li class='muted'>全部空仓</li>";
    return `<article class="panel">
      <div class="panel-heading">
        <div><h2>${esc(s.strategy)} v${esc(s.version)}</h2><p>${esc(s.venue)}</p></div>
        <span class="soft-pill ${s.pnl>=0?"safe":"warn"}">${s.pnl>=0?"+":""}${money(s.pnl)}U</span>
      </div>
      <div class="metric-grid">
        <div><small>权益</small><strong>${money(s.equity)}U</strong></div>
        <div><small>收益率</small><strong class="${trendClass(s.total_return)}">${pct(s.total_return)}</strong></div>
        <div><small>最大回撤</small><strong>${pct(s.max_drawdown)}</strong></div>
        <div><small>交易笔数</small><strong>${s.trades}</strong></div>
        <div><small>胜率</small><strong>${(s.win_rate*100).toFixed(0)}%</strong></div>
        <div><small>手续费</small><strong>${money(s.total_fees)}U</strong></div>
      </div>
      <ul class="plain-list">${holds}</ul>
      <p class="muted">更新于 ${localTime(s.updated_at,true)}</p>
    </article>`;
  }).join("");

  const rows=[];
  live.forEach(s=>(s.events||[]).forEach(e=>rows.push({...e,strategy:s.strategy})));
  rows.sort((a,b)=>String(b.time).localeCompare(String(a.time)));
  events.innerHTML = rows.length ? `<table class="data-table"><thead><tr>
      <th>时间</th><th>策略</th><th>动作</th><th>品种</th><th>方向</th>
      <th class="num">价格</th><th class="num">盈亏</th><th>说明</th></tr></thead><tbody>` +
    rows.slice(0,40).map(e=>`<tr>
      <td>${localTime(e.time,true)}</td><td>${esc(e.strategy)}</td><td>${esc(e.kind)}</td>
      <td>${esc(e.symbol)}</td><td>${esc(e.side)}</td>
      <td class="num">${money(e.price)}</td>
      <td class="num ${trendClass(e.pnl)}">${e.kind==="平仓"?(e.pnl>=0?"+":"")+money(e.pnl):"—"}</td>
      <td>${esc(e.reason)}</td></tr>`).join("") + "</tbody></table>"
    : "<p class='muted'>两个策略都还没有成交。策略按已收线K线决策，最快也要等到下一根15分钟收线。</p>";
}
async function loadCompare(){ renderCompare(await getJSON("/api/compare")); }

document.addEventListener("DOMContentLoaded",()=>{
  const page=document.body.dataset.page;
  // 手动触发也走同一套记账，点刷新失败时同样会在页面上报错而不是静默。
  const manual=task=>()=>runRefresh(task);
  if(page==="dashboard"){startPolling(loadOverview,10000);}
  if(page==="accounts"){startPolling(loadOverview,15000);}
  if(page==="market"){
    // 盘口每秒变、K线要30秒才变一次：统一2秒一轮，K线每15轮才重新拉，
    // 既让价格跳动可见，又不会每2秒重算一遍布林带。
    // 价格由浏览器直连交易所推送，轮询只负责K线与布林带——那要到主周期收线才变。
    startPolling(loadMarket,30000);
    startQuoteStream();
    const reloadChart = manual(loadMarket);
    $("#market-refresh").addEventListener("click",reloadChart);
    $("#market-interval").addEventListener("change",reloadChart);
    // 换交易所或品种要重连直连通道，否则会一直显示上一个品种的价格。
    ["#market-venue","#market-symbol"].forEach(s=>$(s).addEventListener("change",()=>{ startQuoteStream(); reloadChart(); }));
    window.addEventListener("beforeunload", stopQuoteStream);
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
  if(page==="compare"){startPolling(loadCompare,15000);}
  if(page==="system"){startPolling(loadSystem,60000);}
  if(page==="trading"){startPolling(loadSwitches,30000);}
});
