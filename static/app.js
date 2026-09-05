let token = '', state = null, busy = false, actionBusy = false, historyBusy = false, historyPage = null;
const $ = id => document.getElementById(id);
const number = (n, digits=2) => n == null ? '—' : Number(n).toLocaleString('zh-CN',{maximumFractionDigits:digits});
const escape = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = s => s ? new Date(s).toLocaleString('zh-CN',{hour12:false}) : '等待采集';
async function api(path, body){
  const controller = new AbortController();
  const timer = setTimeout(()=>controller.abort(), 15000);
  try {
    const response = await fetch('/api/'+path,{signal:controller.signal,method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
    const data = await response.json();
    if(!response.ok) throw Error(data.error || '请求失败');
    return data;
  } catch(e) {
    if(e.name==='AbortError') throw Error(body===undefined?'连接超时，显示的是上次状态；正在等待重连。':'操作响应超时，结果尚未确认，请查看最新状态；请求不会自动重发。');
    throw e;
  } finally {clearTimeout(timer)}
}
function render(s){
  state=s; $('login').hidden=true; $('dashboard').hidden=false;
  $('mode').textContent=({paper:'模拟运行',testnet:'币安测试网',live:'真实资金 · 实盘'})[s.mode];
  $('mode').className='tag'+(s.mode==='live'?' live':'');
  $('running').textContent=s.stop_requested&&s.executor_busy?'已请求暂停 · 等待当前处理完成':s.running?'自动跟单中':'已暂停';
  $('updated').textContent='源数据 '+date(s.source.updated);
  $('equity').textContent=number(s.source.equity); $('capital').textContent=number(s.capital);
  $('multiple').textContent=s.multiplier+'×'; $('cap').textContent=number(s.max_gross);
  $('ratio').textContent=Number(s.source.equity)>0?'数量比例 '+number(Number(s.capital)*Number(s.multiplier)/Number(s.source.equity)*100,5)+'%':'等待带单余额';
  $('credentials').textContent=(s.credentials_configured?'API凭据已配置':'未配置API凭据，模拟模式无需密钥')+' · 本服务更新 '+date(s.last_poll);
  const ding=s.dingtalk||{};
  $('credentials').textContent+=' · 钉钉'+(ding.enabled?'已启用':'未配置/未启用')+(ding.pending?'，待发 '+ding.pending+' 条':'')+(ding.error?'，'+ding.error:'');
  const warning=s.error || (s.source_stale?'源数据已过期，请先启动网页采集；当前金额仅为历史快照。':'');
  $('error').hidden=!warning; $('error').textContent=warning; $('resolve').hidden=!s.pending;
  $('pnl').textContent='累计平仓毛盈亏 '+number(s.realized)+' USDT';
  $('aum').textContent='源资产管理规模（仅展示）：'+number(s.source.aum)+' USDT';
  const positions=Object.entries(s.positions).filter(([,p])=>Number(p.quantity)>0);
  $('positionList').innerHTML=positions.length?positions.map(([k,p])=>{const [symbol,side]=k.split(':');return `<article class="card"><div class="cardhead"><strong>${escape(symbol)}</strong><span class="tag">${side==='LONG'?'多头':'空头'}</span></div><div class="grid"><div><small>持仓数量</small>${number(p.quantity,8)}</div><div><small>开仓均价</small>${number(p.entry,8)}</div><div><small>开仓名义金额</small>${number(Number(p.quantity)*Number(p.entry))} USDT</div></div></article>`}).join(''):'<div class="empty">暂无跟单仓位，启动后等待新的开仓信号。</div>';
  const labels={filled:'成交',skipped:'跳过',blocked:'已阻止',baseline:'初始化',rejected:'未成交'};
  const records=historyPage?historyPage.records:s.records;
  $('orderList').innerHTML=records.length?records.map(r=>`<article class="card"><div class="cardhead"><strong>${escape(r.symbol||'系统')}</strong><span>${r.operation==='OPEN'?'开仓':r.operation==='CLOSE'?'平仓':''} ${r.side==='LONG'?'多头':r.side==='SHORT'?'空头':''}</span><span class="tag">${escape(labels[r.status]||r.status)}</span></div><p>${escape(r.note)}</p>${r.quantity?`<p>数量 ${number(r.quantity,8)} · 成交价 ${number(r.price,8)}</p>`:''}<small>${date(r.time)}</small></article>`).join(''):'<div class="empty">暂无记录</div>';
  $('older').disabled=historyBusy || Boolean(historyPage&&!historyPage.next_before);
  $('latest').hidden=!historyPage;
}
async function refresh(){if(!token||busy)return;busy=true;try{render(await api('status'));$('connection').textContent='已连接'}catch(e){$('connection').textContent='连接异常：'+e.message;if(state)$('running').textContent='连接中断 · 运行状态未知';else $('message').textContent=e.message}finally{busy=false}}
$('loginForm').addEventListener('submit',e=>{e.preventDefault();token=$('token').value.trim();$('token').value='';refresh()});
for(const button of document.querySelectorAll('.tab'))button.addEventListener('click',()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===button));document.querySelectorAll('.panel').forEach(x=>x.hidden=x.id!==button.dataset.tab)});
async function action(path,body={}){
  // Pause remains available even while start/reconcile is waiting for a reply.
  if(path!=='stop'&&actionBusy)return;
  if(path==='stop'&&$('stop').disabled)return;
  if(path==='stop')$('stop').disabled=true;
  else {actionBusy=true;$('start').disabled=true;$('resolve').disabled=true}
  try{await api(path,body);$('message').textContent=path==='stop'?'已收到暂停请求，已提交的订单仍需等待确认。':path==='start'?'启动请求已完成，请以最新运行状态为准。':'订单核对已完成。';await refresh()}
  catch(e){$('message').textContent=e.message}
  finally{if(path==='stop')$('stop').disabled=false;else{actionBusy=false;$('start').disabled=false;$('resolve').disabled=false}}
}
$('start').addEventListener('click',()=>{let confirmation='';if(state?.mode==='live'){confirmation=prompt('将使用真实资金下单。请输入：启动100U实盘跟单');if(!confirmation)return}action('start',{confirmation})});
$('stop').addEventListener('click',()=>action('stop'));
$('resolve').addEventListener('click',()=>action('reconcile'));
$('older').addEventListener('click',async()=>{
  if(historyBusy)return;
  historyBusy=true;$('older').disabled=true;
  try{
    if(!historyPage)historyPage=await api('records');
    if(historyPage.next_before)historyPage=await api('records?before='+historyPage.next_before);
  }catch(e){$('message').textContent=e.message}
  finally{historyBusy=false;if(state)render(state)}
});
$('latest').addEventListener('click',()=>{historyPage=null;if(state)render(state);refresh()});
setInterval(refresh,5000);
