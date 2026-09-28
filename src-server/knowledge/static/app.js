/* VideoMind 收藏暗房 — 前端。
   数据源：GET /api/v1/knowledge + GET /api/v1/search + GET /video/{id}。
   零依赖：纯 JS 解析 markdown → DOM，离线可跑。 */

const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];

const state = {
  feed: [],
  waiting: true,
  view: 'feed',           // feed | detail
};

/* ── 工具 ────────────────────────────── */
function esc(s){ const d=document.createElement('div'); d.textContent=s??''; return d.innerHTML; }
function escAttr(s){ return (s??'').replace(/["&<>]/g, c=>({'"':'&quot;','&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function fmtDur(ms){
  if(!ms) return null;
  const s = Math.round(ms/1000);
  if(s<60) return s+'秒';
  const m = Math.floor(s/60); const r = s%60;
  if(m<60) return r? m+'分'+r+'秒' : m+'分钟';
  return Math.floor(m/60)+'小时'+ (m%60? (m%60)+'分' : '');
}
const fmtNum = n => { n=+n||0; return n>=10000? (n/10000).toFixed(1).replace(/\.0$/,'')+'万' : String(n); };

function leadText(item){
  // AI 读到的第一段思想：learning_points 里「核心思想」引语
  const lp = item.learning_points || '';
  // 优先提取 human_summary 的第一句「一句话概括」；markdown 只取文字
  const hs = item.human_summary || '';
  const m = hs.match(/#+\s*一句话概括\s*\n+([^\n]+)/);
  if(m && m[1].trim()) return m[1].trim();
  if(lp) return stripMd(lp).replace(/\s+/g,' ').slice(0,90);
  return (item.description||item.title||'—').replace(/\s+/g,' ').slice(0,80);
}
function stripMd(s){ return (s||'').replace(/[#*>`~[\]]/g,'').replace(/\s+/g,' ').trim(); }

function durLabel(item){
  if(item.status==='mock'||item.media_type==='mock') return null;
  return fmtDur(+item.duration_ms) || fmtDur(+(item.duration||0)) || null;
}

/* ── Markdown → HTML（安全：只生成受控标签，文本一律 esc） ── */
function md(src){
  if(!src) return '';
  const S = esc(src);
  let html = '';
  const lines = S.split('\n');
  let i=0;
  while(i<lines.length){
    let ln = lines[i];
    const mh = ln.match(/^(#{1,6})\s+(.*)$/);
    if(mh){ const lv=Math.min(mh[1].length,6); html+=`<h${lv}>${inline(mh[2])}</h${lv}>\n`; i++; continue; }
    if(/^\s*(---|___|\*\*\*)\s*$/.test(ln)){ html+='<hr>\n'; i++; continue; }
    if(/^\s*$/.test(ln)){ if(!html.endsWith('\n\n')) html+='\n'; i++; continue; }
    if(/^\s*>\s?/.test(ln)){
      let buf=[];
      while(i<lines.length && /^\s*>\s?/.test(lines[i])){ buf.push(lines[i].replace(/^\s*>\s?/,'')); i++; }
      html+=`<blockquote>${inline(buf.join('\n'))}</blockquote>\n`; continue;
    }
    const mU = ln.match(/^\s*[-*+]\s+(.*)$/);
    if(mU){
      let items=[];
      while(i<lines.length){ const mm=lines[i].match(/^\s*[-*+]\s+(.*)$/); if(!mm){if(/^\s*$/.test(lines[i])){i++;break}break;} items.push(inline(mm[1])); i++; }
      html+=`<ul class="dash">${items.map(x=>`<li>${x}</li>`).join('')}</ul>\n`; continue;
    }
    const mN = ln.match(/^\s*\d+[.、]\s+(.*)$/);
    if(mN){
      let items=[];
      while(i<lines.length){ const mm=lines[i].match(/^\s*\d+[.、]\s+(.*)$/); if(!mm){if(/^\s*$/.test(lines[i])){i++;break}break;} items.push(inline(mm[1])); i++; }
      html+=`<ol>${items.map(x=>`<li>${x}</li>`).join('')}</ol>\n`; continue;
    }
    // 段落（合并连续文本行，遇空行/块结束）
    let buf=[ln];
    while(i+1<lines.length){
      const nx=lines[i+1];
      if(/^\s*$/.test(nx)||/^(#{1,6})\s/.test(nx)||/^\s*(>|---|[-\d+*] )/.test(nx)) break;
      buf.push(nx); i++;
    }
    html+=`<p>${inline(buf.join('\n'))}</p>\n`;
    i++;
  }
  return html;
}
function inline(s){
  if(!s) return '';
  // 代码 `x`
  s = s.replace(/`([^`]+)`/g, (_,c)=>`<code>${c}</code>`);
  // 粗体 **x**
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // 链接 [t](u) —— 仅 http(s) 白名单，防止 javascript:
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g, (_,t,u)=>`<a href="${escAttr(u)}" target="_blank" rel="noopener noreferrer">${t}</a>`);
  return s;
}

/* ── Feed：收藏网格 ──────────────────── */
function renderFeed(items){
  state.feed = items;
  const grid = document.createElement('div');
  grid.className='grid';
  // 少于 3 条才内联列数；否则交给 CSS 媒体查询（移动端 1 列）
  if(items.length<3) grid.style.setProperty('--cols', items.length);
  items.forEach((it,idx)=>{
    const lead = leadText(it);
    const d = durLabel(it);
    const st = it.statistics ? (typeof it.statistics==='string'? JSON.parse(it.statistics): it.statistics) : null;
    const a = escAttr(it.author||'佚名');
    const li = document.createElement('button');
    li.type='button'; li.className='card reveal'; li.setAttribute('aria-label','查看笔记：'+escAttr(it.title||''));
    li.style.setProperty('--d', (idx*35)+'ms');
    li.innerHTML =
      `<span class="card-lead">${inline(esc(lead))}</span>` +
      `<span class="card-meta">` +
        `<span class="card-author">${a}</span>` +
        (st && st.digg_count!=null? `<span class="card-counts"><span class="c-like">♥ <b>${fmtNum(st.digg_count)}</b></span><span>藏<b> ${fmtNum(st.collect_count)}</b></span><span>转<b> ${fmtNum(st.share_count)}</b></span></span>`:'') +
        (d? `<span class="card-dur">${d}</span>` : (it.status==='mock'? `<span class="card-dur live">模拟</span>`:'')) +
        `<span class="card-idx">${String(idx+1).padStart(2,'0')}</span>` +
      `</span>`;
    li.addEventListener('click',()=>openDetail(it.video_id));
    grid.appendChild(li);
  });
  const slot = $('#feed');
  slot.querySelector('.island-note').textContent = items.length? '在暗房里逐帧显影 · ' : '还没有收藏';
  const old = slot.querySelector('.grid'); if(old) old.remove();
  slot.appendChild(grid);
}

/* ── 检索 ───────────────────────────── */
let searchTimer=null, lastQ='';
function openSearch(){ $('#search-overlay').hidden=false; $('#search-overlay').setAttribute('aria-hidden','false'); $('#search-q').value=''; $('#search-results').innerHTML=''; $('#search-q').focus(); }
function closeSearch(){ $('#search-overlay').hidden=true; $('#search-overlay').setAttribute('aria-hidden','true'); }
function runSearch(){
  const q = $('#search-q').value.trim();
  if(!q){ $('#search-results').innerHTML=''; return; }
  if(q===lastQ) return; lastQ=q;
  clearTimeout(searchTimer);
  $('#search-results').innerHTML='<p class="search-loading">显影中…</p>';
  searchTimer=setTimeout(async ()=>{
    try{
      const r = await fetch('/api/v1/search?q='+encodeURIComponent(q)+'&limit=12');
      const j = await r.json();
      renderResults(j.results||[], q);
    }catch(e){ $('#search-results').innerHTML='<p class="search-zero">读取失败，稍后再试。</p>'; }
  }, 260);
}
function renderResults(list, q){
  if(!list.length){ $('#search-results').innerHTML='<p class="search-zero">没有找到「'+esc(q)+'」的沉淀。换个说法，或直接浏览上面的收藏。</p>'; return; }
  const qr = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'i');
  const hl = t => esc(t).replace(qr, m=>'<mark>'+m+'</mark>');
  $('#search-results').innerHTML = list.map(it=>{
    const meta = state.feed.find(f=>f.video_id===it.video_id);
    const author = (it.video&&it.video.author)||(meta&&meta.author)||'佚名';
    return `<button type="button" class="sresult" data-id="${escAttr(it.video_id)}">
      <span class="s-title">${hl(it.title||'')}</span>
      <span class="s-snippet">${hl(it.snippet||'')}</span>
      <span class="s-meta"><span class="s-author">${esc(author)}</span><span class="s-score">匹配度 ${Math.round(+it.score)}%</span></span>
    </button>`;
  }).join('');
  $$('#search-results .sresult').forEach(b=> b.addEventListener('click',()=>{ closeSearch(); openDetail(b.dataset.id); }));
}
function openResult(id){ closeSearch(); openDetail(id); }

/* ── 详情页 ─────────────────────────── */
let currentVid=null;
async function openDetail(video_id){
  try{
    const r = await fetch('/video/'+encodeURIComponent(video_id));
    if(!r.ok) throw 0;
    const j = await r.json();
    currentVid=video_id;
    renderDetail(j.video, j.analysis);
    showView('detail');
    window.scrollTo(0,0);
    history.replaceState(null,'','#video/'+video_id);
  }catch(e){ $('#detail-body').innerHTML='<p class="search-zero">未能读取这条收藏。</p>'; showView('detail'); }
}
function renderDetail(v, a){
  const st = v.statistics ? (typeof v.statistics==='string'? JSON.parse(v.statistics): v.statistics) : null;
  const d = fmtDur(+v.duration_ms)||'';
  const live = v.status==='mock';
  const stats = st && (st.digg_count!=null||st.collect_count!=null)
    ? `<div class="detail-stats">
         ${st.digg_count!=null? `<span class="stat ${st.digg_count>0?'stat-live':''}"><b>${fmtNum(st.digg_count)}</b><span>赞</span></span>`:''}
         ${st.collect_count!=null? `<span class="stat"><b>${fmtNum(st.collect_count)}</b><span>收藏</span></span>`:''}
         ${st.share_count!=null? `<span class="stat"><b>${fmtNum(st.share_count)}</b><span>转发</span></span>`:''}
         ${st.comment_count!=null? `<span class="stat"><b>${fmtNum(st.comment_count)}</b><span>评论</span></span>`:''}
         ${d? `<span class="stat"><b>${d}</b><span>时长</span></span>`:''}
       </div>` : (d? `<div class="detail-stats"><span class="stat"><b>${d}</b><span>时长</span></span></div>`:'');
  const tags = v.hashtags ? (typeof v.hashtags==='string'? JSON.parse(v.hashtags): v.hashtags) : [];
  const tagHtml = tags.length? `<p class="detail-tags">${tags.map(t=>'#'+esc(t.replace(/^#/,''))).join(' ')}</p>`:'';
  let noteHtml='';
  if(a){
    noteHtml += '<p class="note-intro">AI 逐帧读屏后写下</p>';
    let hs = a.human_summary||'';
    if(hs){
      // 抽取结构小节
      const secs = parseSections(hs).filter(s=> s.title || s.body.trim());
      noteHtml += secs.map(s=>`<section class="note-sec">${s.title? `<h2>${s.title}</h2>`:''}${mdBlock(s.body)}</section>`).join('');
    } else if(a.summary||a.learning_points||a.visual_summary){
      noteHtml += md('<h2>学习要点</h2>'+ (a.learning_points||''));
      if(a.visual_summary) noteHtml += md('<h2>画面所见</h2>'+a.visual_summary);
      if(a.summary) noteHtml += md('<h2>总结</h2>'+a.summary);
    } else {
      noteHtml = '<p class="search-zero">这段还没有沉淀笔记。</p>';
    }
  } else {
    noteHtml = '<p class="search-zero">这段还没有分析笔记。</p>';
  }

  $('#detail-body').innerHTML =
    `<p class="detail-kicker">${live?'模拟帧':'收藏'} · ${escAttr(v.video_id)}</p>` +
    `<h1 class="detail-title">${esc(v.title||'（无标题）')}${live?'<span class="tag-mock">模拟</span>':''}</h1>` +
    `<p class="detail-author">${esc(v.author||'佚名')}</p>` +
    (v.description? `<p class="detail-desc">${esc(v.description)}</p>`:'') +
    tagHtml + stats +
    `<div class="note">${noteHtml}</div>`;
}

/* 从 markdown 切「##/###」小节；文档级 # 标题跳过（详情页 h1 已展示） */
function parseSections(mdSrc){
  const lines = String(mdSrc||'').split('\n');
  while(lines.length && /^#(?!#)\s/.test(lines[0].trim())) lines.shift();  // 去开头单井标题
  const secs=[]; let cur=null;
  for(const ln of lines){
    const m=ln.match(/^#{2,6}\s+(.*)$/);
    if(m){ cur={title:m[1].trim(), body:[]}; secs.push(cur); }
    else if(ln.trim()){ if(!cur){ cur={title:'', body:[]}; secs.push(cur); } cur.body.push(ln); }
  }
  return secs.map(s=>({title:s.title, body:s.body.join('\n')}))
             .filter(s=> s.body.trim() || s.title);
}
function mdBlock(src){ return md(src); }

function showView(v){
  state.view=v;
  $$('#feed, #detail').forEach(el=> el.classList.toggle('hidden', v!==el.dataset.view));
  $('#feed').hidden = (v!=='feed');
  $('#detail').hidden = (v!=='detail');
  $$('.search-overlay').forEach(o=> o.hidden=true);
}

/* ── URL hash 直达某条收藏 ──────────── */
function hashOpen(){
  const m = location.hash.match(/^#video\/(.+)$/);
  if(m) openDetail(decodeURIComponent(m[1]));
}
window.addEventListener('hashchange', hashOpen);

/* ── 键盘 ───────────────────────────── */
document.addEventListener('keydown', e=>{
  const overlayOpen = !$('#search-overlay').hidden;
  if(e.key==='Escape'){
    if(overlayOpen) closeSearch();
    else if(state.view==='detail'){ history.replaceState(null,'','#'); showView('feed'); window.scrollTo(0,0); }
    return;
  }
  if((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='k'){ e.preventDefault(); overlayOpen? closeSearch() : openSearch(); }
});

/* ── 事件绑定 ───────────────────────── */
window.addEventListener('load', init);
async function init(){
  $('#open-search').addEventListener('click', openSearch);
  $('#close-search').addEventListener('click', closeSearch);
  $('#search-form').addEventListener('submit', e=>{ e.preventDefault(); runSearch(); });
  $('#search-q').addEventListener('input', runSearch);
  $('#search-q').addEventListener('keydown', e=>{ if(e.key==='Enter') e.preventDefault(); });
  $('#back').addEventListener('click', ()=>{ history.replaceState(null,'','#'); showView('feed'); window.scrollTo(0,0); });
  $('.brand').addEventListener('click', ()=>{ history.replaceState(null,'','#'); showView('feed'); });

  showView('feed');
  try{
    const r = await fetch('/api/v1/knowledge?limit=200');
    const j = await r.json();
    renderFeed(j.items||[]);
  }catch(e){
    $('#feed-note').textContent = '读取失败 —— 确认 API 已启动';
  }
  hashOpen();
}
