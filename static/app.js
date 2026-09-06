let token = localStorage.getItem('copycat_admin_token') || '', state = null, busy = false, actionBusy = false, historyBusy = false, historyPage = null, closeKey = null;
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
    if(response.status===401){localStorage.removeItem('copycat_admin_token');token='';$('login').hidden=false;$('dashboard').hidden=true}
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
  $('start').disabled=actionBusy||Boolean(s.running);
  $('start').textContent=s.running?'跟单中':'开始跟单';
  $('updated').textContent='源数据 '+date(s.source.updated);
  $('equity').textContent=number(s.source.equity);   $('capital').textContent=number(s.capital);
  const heroLine=document.querySelector('.hero p');
  if(heroLine)heroLine.textContent=`${number(s.capital,0)} USDT 本金 · ${s.multiplier} 倍比例跟单 · 独立逐仓执行`;
  const account=s.account||{};
  if(account.error){$('wallet').textContent='—';$('walletHint').textContent=account.error}
  else{$('wallet').textContent=number(account.margin_balance);$('walletHint').textContent=(account.label||'U本位')+'权益 '+number(account.margin_balance)+' · 可用 '+number(account.available)+(account.mismatch?' · 与本金差超过5U':'')}
  $('multiple').textContent=s.multiplier+'×'; $('cap').textContent=number(s.max_gross);
  $('ratio').textContent=Number(s.source.equity)>0?'数量比例 '+number(Number(s.capital)*Number(s.multiplier)/Number(s.source.equity)*100,5)+'%':'等待带单余额';
  $('credentials').textContent=(s.credentials_configured?'API凭据已配置':'未配置API凭据，模拟模式无需密钥')+' · 本服务更新 '+date(s.last_poll);
  const ding=s.dingtalk||{};
  $('credentials').textContent+=' · 钉钉'+(ding.enabled?'已启用':'未配置/未启用')+(ding.pending?'，待发 '+ding.pending+' 条':'')+(ding.error?'，'+ding.error:'');
  const warning=s.error || (s.source_stale?'源数据已过期，请先启动网页采集；当前金额仅为历史快照。':'') || (account.hedge_mode?'当前为双向持仓模式，开始跟单前需改为单向。':'') || (account.mismatch?`合约账户权益 ${number(account.margin_balance)} USDT 与配置本金 ${number(s.capital)} 相差超过5U，请划转至接近预算后再开仓。`:'');
  $('error').hidden=!warning;
  if(account.hedge_mode && !s.error && !s.source_stale){
    $('error').innerHTML='当前为双向持仓模式，CopyCat 需要单向持仓。<button type="button" id="fixHedge" class="linkish">改为单向</button>';
    $('fixHedge').onclick=()=>openHedgeDialog();
  }else{$('error').textContent=warning}
  $('resolve').hidden=!s.pending;
  const positions=Object.entries(s.positions||{}).filter(([,p])=>Number(p.quantity)>0);
  const live=s.live_positions||{};
  const unrealized=Object.values(live).reduce((sum,row)=>sum+(Number(row?.unrealized_pnl)||0),0);
  $('pnl').textContent='累计平仓毛盈亏 '+number(s.realized)+' USDT'+(positions.length?` · 未实现 ${unrealized>=0?'+':''}${number(unrealized)} USDT`:'');
  $('aum').textContent='源资产管理规模（仅展示）：'+number(s.source.aum)+' USDT';
  $('closeAll').hidden=!positions.length;
  $('closeAll').disabled=actionBusy||Boolean(s.pending);
  const signed=(value,digits=2)=>{const n=Number(value);if(!Number.isFinite(n))return '—';const text=(n>0?'+':'')+number(n,digits);return `<span class="${n>0?'green':n<0?'red':''}">${text}</span>`};
  $('positionList').innerHTML=positions.length?positions.map(([k,p])=>{
    const [symbol,side]=k.split(':');
    const L=live[k]||{};
    const mark=L.mark_price!=null?number(L.mark_price,8):'—';
    const notional=L.notional!=null?number(L.notional):number(Number(p.quantity)*Number(p.entry));
    const roe=L.roe_percent!=null?signed(L.roe_percent)+'%':'—';
    const margin=L.margin!=null?number(L.margin)+' USDT':'—';
    const lev=L.leverage!=null?L.leverage+'×':(s.leverage?s.leverage+'×':'—');
    const liq=L.liquidation_price!=null&&Number(L.liquidation_price)>0?number(L.liquidation_price,8):'—';
    const status=L.error?escape(L.error):(L.status||'持有中');
    return `<article class="card"><div class="cardhead"><strong>${escape(symbol)}</strong><span class="tag">${side==='LONG'?'多头':'空头'}</span><span class="tag">${status}</span><button class="danger position-close" data-key="${escape(k)}">平仓</button></div><div class="grid"><div><small>持仓数量</small>${number(p.quantity,8)}</div><div><small>开仓均价</small>${number(p.entry,8)}</div><div><small>标记价格</small>${mark}</div><div><small>当前名义金额</small>${notional} USDT</div><div><small>未实现盈亏</small>${L.unrealized_pnl!=null?signed(L.unrealized_pnl)+' USDT':'—'}</div><div><small>收益率</small>${roe}</div><div><small>逐仓保证金</small>${margin}</div><div><small>杠杆</small>${lev}</div><div><small>强平价</small>${liq}</div></div></article>`;
  }).join(''):'<div class="empty">暂无跟单仓位，启动后等待新的开仓信号。</div>';
  const labels={filled:'成交',skipped:'跳过',blocked:'已阻止',baseline:'初始化',rejected:'未成交'};
  const records=historyPage?historyPage.records:s.records;
  $('orderList').innerHTML=records.length?records.map(r=>{
    const bits=[];
    if(r.operation==='CLOSE'&&r.source_quantity){
      bits.push(`源平仓 ${number(r.source_quantity,8)} · 占源仓 ${number(r.source_close_percent,2)}%`+(r.source_before?`（平前 ${number(r.source_before,8)}）`:''));
    }
    if(r.quantity){
      bits.push(`本地数量 ${number(r.quantity,8)}`+(r.price?` · 成交价 ${number(r.price,8)}`:'')+(r.local_close_percent?` · 本地平 ${number(r.local_close_percent,2)}%`:''));
    }
    return `<article class="card"><div class="cardhead"><strong>${escape(r.symbol||'系统')}</strong><span>${r.operation==='OPEN'?'开仓':r.operation==='CLOSE'?'平仓':''} ${r.side==='LONG'?'多头':r.side==='SHORT'?'空头':''}</span><span class="tag">${escape(labels[r.status]||r.status)}</span></div><p>${escape(r.note)}</p>${bits.map(t=>`<p>${t}</p>`).join('')}<small>${date(r.time)}</small></article>`;
  }).join(''):'<div class="empty">暂无记录</div>';
  $('older').disabled=historyBusy || Boolean(historyPage&&!historyPage.next_before);
  $('latest').hidden=!historyPage;
}
async function refresh(){if(!token||busy)return;busy=true;try{render(await api('status'));localStorage.setItem('copycat_admin_token',token);$('connection').textContent='已连接'}catch(e){$('connection').textContent='连接异常：'+e.message;if(state)$('running').textContent='连接中断 · 运行状态未知';else $('message').textContent=e.message}finally{busy=false}}
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
  finally{if(path==='stop')$('stop').disabled=false;else{actionBusy=false;$('start').disabled=Boolean(state?.running);$('start').textContent=state?.running?'跟单中':'开始跟单';$('resolve').disabled=false}}
}
function openLiveDialog(){
  $('liveSummary').textContent=`将使用真实资金下单。当前本金 ${number(state.capital)} USDT · 倍率 ${state.multiplier}× · 敞口上限 ${number(state.max_gross)} USDT。`;
  $('liveDialog').showModal();
}
function openHedgeDialog(){
  $('hedgeError').hidden=true;
  $('hedgeDialog').showModal();
}
function openCloseDialog(key){
  closeKey=key;
  const position=key&&state?.positions?.[key];
  $('closeTitle').textContent=key?'确认平仓':'确认全部平仓';
  $('closeSummary').textContent=key?`${key.replace(':LONG',' 多头').replace(':SHORT',' 空头')} · 数量 ${number(position?.quantity,8)}`:'将关闭全部 CopyCat 跟单持仓。';
  $('closeDialog').showModal();
}
$('start').addEventListener('click', async ()=>{
  if($('start').disabled||state?.running)return;
  if(state?.mode!=='live'){action('start',{});return}
  $('start').disabled=true;
  try{
    const mode=await api('position-mode');
    if(state){state.account={...(state.account||{}), ...(mode.account||{}), hedge_mode:mode.hedge_mode}}
    if(mode.hedge_mode){openHedgeDialog();return}
    openLiveDialog();
  }catch(e){$('message').textContent=e.message}
  finally{$('start').disabled=Boolean(state?.running);$('start').textContent=state?.running?'跟单中':'开始跟单'}
});
$('liveForm').addEventListener('submit',e=>{
  const submitter=e.submitter;
  if(submitter&&submitter.value==='cancel')return;
  e.preventDefault();
  $('liveDialog').close();
  action('start',{});
});
$('hedgeForm').addEventListener('submit',async e=>{
  const submitter=e.submitter;
  if(submitter&&submitter.value==='cancel')return;
  e.preventDefault();
  $('hedgeOk').disabled=true;
  try{
    await api('position-mode',{mode:'one_way'});
    $('hedgeDialog').close();
    $('message').textContent='已切换为单向持仓，请再次点击开始跟单';
    await refresh();
    if(state?.mode==='live' && !state?.account?.hedge_mode) openLiveDialog();
  }catch(err){
    $('hedgeError').hidden=false;
    $('hedgeError').textContent=err.message;
  }finally{$('hedgeOk').disabled=false}
});
$('stop').addEventListener('click',()=>action('stop'));
$('resolve').addEventListener('click',()=>action('reconcile'));
$('closeAll').addEventListener('click',()=>openCloseDialog(null));
$('positionList').addEventListener('click',e=>{const button=e.target.closest('.position-close');if(button)openCloseDialog(button.dataset.key)});
$('closeForm').addEventListener('submit',e=>{
  const submitter=e.submitter;
  if(submitter&&submitter.value==='cancel'){closeKey=null;return}
  e.preventDefault();
  $('closeDialog').close();
  const key=closeKey;closeKey=null;
  action(key?'close-position':'close-all',key?{key}:{});
});
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
if(token) refresh();
