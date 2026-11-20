import {session} from '../stores/session.js';
import {recoveryAPI} from '../api/entities.js';
import {table} from '../components/common/table.js';
import {esc,formObject} from '../utils/html.js';
import {message} from '../hooks/action.js';
import {RECOVERY_STATUSES} from '../constants/enums.js';

const REC_TYPES={begin:'开始',update:'更新',clr:'补偿 CLR',commit:'提交',end:'了结',begin_checkpoint:'检查点开始',end_checkpoint:'检查点完成'};
const PHASES={analysis:'① 分析',redo:'② 重做',undo:'③ 回滚'};

export async function loadRecovery(){
 const sessions=await recoveryAPI.list();
 let id=Number(localStorage.getItem('recovery-session'))||sessions[0]?.id;
 if(id&&!sessions.some(s=>s.id===id))id=sessions[0]?.id;
 const view=id?await recoveryAPI.get(id):null;
 if(id)localStorage.setItem('recovery-session',id);
 return {sessions,view,current:id};
}

const rbadge=s=>`<span class="badge ${esc(s)}">${esc(RECOVERY_STATUSES[s]||s)}</span>`;
const money=v=>`<b>${v}</b>`;

function desc(r){
 switch(r.type){
  case 'begin':return '事务开始';
  case 'update':return `${r.acct}: ${r.before} → ${r.after}（页 ${r.page}）`;
  case 'clr':return `补偿 ${r.acct} ← ${r.value}（撤销 LSN ${r.ref}，undo_next=${r.undo_next??'无'}）`;
  case 'commit':return '提交（强制刷日志到此即确认）';
  case 'end':return '事务了结';
  case 'begin_checkpoint':return '模糊检查点开始';
  case 'end_checkpoint':return `检查点完成（事务快照 ${Object.keys(r.txns||{}).length} 个 / 脏页 ${Object.keys(r.dpt||{}).length} 个）`;
  default:return r.type;
 }
}

function actionBar(v){
 if(session.role==='viewer')return '<p class="readonly">观察员可查看状态与事件流；分步操作需操作员或管理员。</p>';
 const s=v.status,running=s==='running',crashed=s==='crashed',recovering=s==='recovering';
 const active=v.txns.filter(t=>t.status==='active');
 const dirty=v.buffer.filter(p=>p.dirty);
 const acctOpts=v.accounts.map(a=>`<option value="${a.acct}">${a.acct}（页 ${a.page}）</option>`).join('');
 const dis=f=>f?'':'disabled';
 return `<div class="actbar">
 <form id="rec-transfer" class="mini"><label>转账<select name="src">${acctOpts}</select></label><label>→<select name="dst">${acctOpts.replace('value="C"','value="C" selected')}</select></label><label>金额<input name="amount" type="number" min="1" value="100" required></label><button ${dis(running)}>发起转账</button></form>
 <form id="rec-commit" class="mini"><label>提交<select name="txn">${active.map(t=>`<option>${t.txn}（${t.src}→${t.dst} ${t.amount}）</option>`).join('')}</select></label><button ${dis(running&&active.length)}>提交事务</button></form>
 <form id="rec-flush" class="mini"><label>刷页<select name="page">${dirty.map(p=>`<option>${p.page}（pageLSN ${p.page_lsn}）</option>`).join('')}</select></label><button ${dis((running||recovering)&&dirty.length)}>刷页落盘</button></form>
 <span class="actgroup"><button data-rec="checkpoint_begin" ${dis(running&&!v.checkpoint_in_progress)}>开始检查点</button><button data-rec="checkpoint_end" ${dis(running&&v.checkpoint_in_progress)}>完成检查点</button></span>
 <span class="actgroup"><button data-rec="truncate" ${dis(running)}>安全截断日志</button></span>
 <form id="rec-truncate" class="mini"><label>截断到 LSN<input name="to_lsn" type="number" min="1" placeholder="危险"></label><button ${dis(running)}>强制截断</button></form>
 <span class="actgroup"><button class="danger" data-rec="crash" ${dis(running||recovering)}>⚡ 断电</button><button data-rec="restart" ${dis(crashed)}>重启</button><button data-rec="recover_step" ${dis(recovering)}>恢复一步</button><button data-rec="recover_run" ${dis(recovering)}>一键恢复</button></span>
 <span class="actgroup"><button class="secondary" data-rec="reset">重置演练</button></span>
</div>`;
}

function balancesCard(v){
 return table(['账户','页','台账应有','当前可见','磁盘落盘'],v.accounts.map(a=>[
  a.acct,a.page,money(v.expected[a.acct]),money(a.current)+(a.in_buffer?'':' <small>（读磁盘）</small>'),money(a.disk)]));
}

function walCard(v){
 const rows=v.wal_disk.map(r=>[r.lsn,REC_TYPES[r.type]||r.type,r.txn||'—',esc(desc(r)),
  r.retain.length?`<span class="pill">${esc(r.retain.join('；'))}</span>`:'<span class="ok">可清理</span>']);
 return `<div class="section-title"><h2>磁盘 WAL（已落盘日志）</h2><span>主记录 → ${v.master_checkpoint??'无'} · 安全截断线 ${v.safe_truncate_lsn??'全部可清'} · 已累计清理 ${v.truncated_count} 条</span></div>
 ${v.retention_pins.length?`<p class="pins">压线原因：${v.retention_pins.map(p=>`LSN ${p.lsn}（${esc(p.reason)}）`).join('；')}</p>`:'<p class="pins">没有压线因素，磁盘日志可全部清理。</p>'}
 ${table(['LSN','类型','事务','内容','留存原因'],rows,'磁盘日志为空。')}`;
}

function recoveryCard(v){
 const r=v.recovery;
 if(!r)return '';
 return `<section class="recovering-box"><h2>恢复进行中：${PHASES[r.phase]||r.phase}</h2>
 <p>事务表 ${esc(JSON.stringify(r.txn_table))} · 脏页表 ${esc(JSON.stringify(r.dpt))} · 待回滚 ${esc(JSON.stringify(r.undo_queue))}</p>
 <p>已重做 ${r.stats.redone} 条 · 幂等跳过 ${r.stats.skipped} 条 · 已写 CLR ${r.stats.clrs} 条。可随时再点「断电」验证重试不重复扣款。</p></section>`;
}

function reportCard(v){
 const r=v.last_recovery;
 if(!r)return '';
 const accts=Object.keys(r.balances);
 return `<section class="verdict"><div><p>恢复报告</p><h2>${r.lost.length?'有转账丢失':'恢复一致'}</h2></div>
 <p>重做 ${r.redone} 条 · 幂等跳过 ${r.skipped} 条 · 回滚事务 ${r.undone_txns.join('、')||'无'}（CLR ${r.clrs} 条）<br>
 ${r.lost.length?`<span class="bad">已提交转账丢失：${r.lost.map(l=>`${l.txn} ${l.src}→${l.dst} ${l.amount}`).join('；')}（日志被误删）</span>`:'已提交转账全部补齐，未提交改动已撤回。'}</p></section>
 ${table(['账户','恢复后余额','台账应有','结论'],accts.map(a=>[a,money(r.balances[a]),money(r.expected[a]),r.balances[a]===r.expected[a]?'<span class="ok">一致</span>':'<span class="bad">不一致（丢失）</span>']))}`;
}

export function recoveryPage(rec){
 const {sessions,view:v}=rec;
 const head=`<section class="split compact"><div><h2>账页刷到一半，突然断电</h2><p>一笔转账改两页；未提交的页也能落盘（steal）；提交确认不等两页刷完（no-force，只强制刷日志）；检查点与转账并发（模糊检查点）。分步断电、重启，观察三段恢复如何补齐已提交、撤回未提交，以及日志为什么留、为什么能清。</p><p class="big-note">WAL：日志先于数据页</p></div>
 <div>${sessions.length?`<label>演练会话<select id="rec-session">${sessions.map(s=>`<option value="${s.id}" ${s.id===rec.current?'selected':''}>#${s.id} ${esc(s.name)}（${RECOVERY_STATUSES[s.status]||s.status}）</option>`).join('')}</select></label>`:'<p>还没有恢复演练会话。</p>'}
 ${session.role!=='viewer'?`<form id="rec-create" class="mini"><label>新会话<input name="name" value="夜班断电演练" required minlength="2" maxlength="120"></label><button>新建会话</button></form>`:''}</div></section>`;
 if(!v)return head+'<section class="empty"><p>新建一个会话开始分步演练。</p></section>';
 return `${head}
 <div class="section-title"><h2>${esc(v.name||'')} <small>#${rec.current}</small></h2>${rbadge(v.status)}</div>
 ${actionBar(v)}
 ${recoveryCard(v)}
 ${reportCard(v)}
 <div class="rec-grid">
  <section><h2>账户余额</h2>${balancesCard(v)}</section>
  <section><h2>缓冲池（内存，断电即失）</h2>${table(['页','脏','pageLSN','recLSN','余额'],v.buffer.map(p=>[p.page,p.dirty?'<span class="bad">脏</span>':'净',p.page_lsn,p.rec_lsn??'—',esc(JSON.stringify(p.balances))]),'缓冲池为空，页面按需从磁盘读入。')}</section>
  <section><h2>磁盘页</h2>${table(['页','pageLSN','余额'],v.disk_pages.map(p=>[p.page,p.page_lsn,esc(JSON.stringify(p.balances))]))}</section>
  <section><h2>内存事务表</h2>${table(['事务','状态','首LSN','末LSN','内容'],v.txns.map(t=>[t.txn,t.status==='active'?'未提交':'已提交',t.first_lsn,t.last_lsn,`${t.src}→${t.dst} ${t.amount}`]),'没有活跃事务。')}${Object.keys(v.locks).length?`<p class="pins">账户锁：${Object.entries(v.locks).map(([a,t])=>`${a} ← ${t}`).join('；')}（未提交事务锁定其改过的账户）</p>`:''}</section>
 </div>
 ${walCard(v)}
 <div class="section-title"><h2>日志缓冲区（内存，未刷盘）</h2><span>${v.wal_buffer.length} 条待刷</span></div>
 ${table(['LSN','类型','事务','内容'],v.wal_buffer.map(r=>[r.lsn,REC_TYPES[r.type]||r.type,r.txn||'—',esc(desc(r))]),'日志缓冲区为空。')}
 <details open><summary>事件流（最新在前）</summary><ol class="events" reversed>${v.events.map(e=>`<li>${esc(e)}</li>`).join('')}</ol></details>`;
}

export function bindRecovery(rec,render){
 const sel=document.querySelector('#rec-session');
 if(sel)sel.onchange=()=>{localStorage.setItem('recovery-session',sel.value);render();};
 const cf=document.querySelector('#rec-create');
 if(cf)cf.addEventListener('submit',async e=>{e.preventDefault();try{const d=formObject(cf);const v=await recoveryAPI.create(d.name);localStorage.setItem('recovery-session',v.id);message('会话已创建');await render();}catch(err){message(err.message,true);}});
 if(!rec.view)return;
 const id=rec.current;
 const run=async(act,params={})=>{try{await recoveryAPI.step(id,act,params);message('已执行，状态已更新');await render();}catch(e){message(e.message,true);}};
 const wire=(fid,fn)=>{const f=document.querySelector(fid);if(f)f.addEventListener('submit',async e=>{e.preventDefault();await fn(formObject(f));});};
 wire('#rec-transfer',d=>run('transfer',{src:d.src,dst:d.dst,amount:Number(d.amount)}));
 wire('#rec-commit',d=>run('commit',{txn:(d.txn||'').split('（')[0]}));
 wire('#rec-flush',d=>run('flush_page',{page:(d.page||'').split('（')[0]}));
 wire('#rec-truncate',d=>{if(!d.to_lsn)return message('请输入截断点 LSN',true);if(confirm('强制截断可能越过安全线，崩溃后已提交转账会丢失。继续？'))return run('truncate',{to_lsn:Number(d.to_lsn)});});
 document.querySelectorAll('[data-rec]').forEach(b=>b.addEventListener('click',()=>{
  const a=b.dataset.rec;
  if(a==='reset'&&!confirm('重置会清空当前演练进度，继续？'))return;
  run(a);
 }));
}
