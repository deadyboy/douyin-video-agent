#!/usr/bin/env node
// VideoMind media provider bridge. dy-downloader@0.2.3 (MIT-declared upstream).
// JSON request via stdin; one machine-readable result on stdout. No credential logging.
import { readFileSync, mkdirSync, readdirSync, statSync } from 'node:fs';
import { resolve, relative, isAbsolute } from 'node:path';

function fail(code) {
  process.stdout.write('VIDEOMIND_RESULT=' + JSON.stringify({ok:false,error_code:code}) + '\n');
  process.exitCode=2;
}
function parseCookieFile(filename) {
  if (!filename) return '';
  const lines=readFileSync(filename,'utf8').split(/\r?\n/);
  const now=Date.now()/1000;
  if (lines[0]?.startsWith('# Netscape') || lines.some(x=>x.startsWith('.douyin.com\t'))) {
    const entries=new Map();
    for (const row of lines) {
      const r=row.startsWith('#HttpOnly_') ? row.slice(10) : row;
      if (!r || r[0]==='#') continue;
      const a=r.split('\t');
      if (a.length!==7) continue;
      const domain=a[0].replace(/^\./,'').toLowerCase();
      if (domain!=='douyin.com' && !domain.endsWith('.douyin.com')) continue;
      if (+a[4]>0 && +a[4]<now) continue;
      const name=a[5], value=a[6];
      if (/^[!#$%&'*+.^_\x60|~0-9a-zA-Z-]+$/.test(name) && !/[\r\n;]/.test(value))
        entries.set(name,value);
    }
    return [...entries].map(([k,v])=>k+'='+v).join('; ');
  }
  const result=lines.join('').trim();
  return result.includes('=')&&!/[\r\n]/.test(result) ? result : '';
}
function validInput(url) {
  if (/^\d{15,25}$/.test(url)) return true;
  let parsed;
  try { parsed=new URL(url); } catch { return false; }
  const host=parsed.hostname.toLowerCase();
  return ['https:','http:'].includes(parsed.protocol) &&
    (host==='douyin.com'||host.endsWith('.douyin.com')||
     host==='iesdouyin.com'||host.endsWith('.iesdouyin.com'));
}
function safeInside(parent,child) {
  if (!isAbsolute(child)) return false;
  const r=relative(resolve(parent),resolve(child));
  return r && r!=='.' && !r.startsWith('..') && !isAbsolute(r);
}
function asString(v) { return typeof v==='string'?v:''; }

async function main() {
  const lines=[];
  for await (const part of process.stdin) lines.push(part);
  const q=JSON.parse(Buffer.concat(lines).toString('utf8'));
  const url=asString(q.url);
  if (!validInput(url)) return fail('INVALID_DOUYIN_LINK');
  const cookie=parseCookieFile(asString(q.cookie_file));
  const provider=q.provider==='dy-downloader'?'dy-downloader':'polydl';
  const {DouyinHandler,DouyinDownloader,setConfig}=await import(provider);
  // PolyDL mirrors the engine in the dYm desktop app; prefer its defaults.
  // dy-downloader is the dYm-web engine. Both run behind the same contract.
  if (provider==='dy-downloader') setConfig({encryption:'ab'});
  const handler=new DouyinHandler({cookie});
  // Discard upstream console output; it may contain signed media URLs.
  console.log=()=>{};
  console.warn=()=>{};
  console.error=()=>{};
  let detail;
  try { detail=await handler.fetchOneVideo(url); }
  catch { return fail('DETAIL_UNAVAILABLE'); }
  // Some Douyin API responses return HTTP 200 but no aweme_detail.
  // Fall back to the library's documented share-page provider in that case.
  if (typeof detail?.toAwemeData==='function' && !detail.awemeId) {
    try {
      const fallback=await handler.fetchOneVideoFromSharePage(url);
      if (fallback?.awemeId) detail=fallback;
    } catch { /* keep a typed error below */ }
  }
  const typed=typeof detail?.toAwemeData==='function';
  const data=typed ? detail.toAwemeData() : {
    awemeId:detail.awemeId,
    awemeType:(detail.images?.length||0)>0?68:0,
    desc:detail.desc||'',
    nickname:detail.author?.nickname||'',
    createTime:detail.createTime||0,
    videoPlayAddr:detail.video?.playAddr?.[0],
    images:(detail.images||[]).map(x=>x.urlList?.[0]).filter(Boolean),
    privateStatus:0,isProhibited:false,
  };
  const id=String(data.awemeId||detail.awemeId||'');
  if (!/^\d{15,25}$/.test(id)) return fail('DETAIL_EMPTY_ID');
  const imagePost=(data.awemeType===68)||(Array.isArray(data.images)&&data.images.length>0);
  const duration=Number(detail?.duration||detail?.video?.duration||0);
  const meta={
    video_id:id, title:asString(data.desc||detail?.desc).slice(0,300),
    description:asString(data.desc||detail?.desc),author:asString(data.nickname||detail?.author?.nickname),
    duration_ms:Number.isFinite(duration)&&duration>0?duration:0,
    media_type:imagePost?'image_post':'video',
    fetched_with_cookie:!!cookie,
    metadata_source:typed?'douyin_api':'mobile_share_page',
    provider,
  };
  if (q.mode==='metadata') {
    process.stdout.write('VIDEOMIND_RESULT='+JSON.stringify({ok:true,meta})+'\n');
    return;
  }
  if (q.mode!=='download') return fail('INVALID_MODE');
  const dest=asString(q.output_dir);
  const allowedRoot=process.env.VIDEOMIND_MEDIA_ROOT||'';
  if (!allowedRoot||!isAbsolute(allowedRoot)||!safeInside(allowedRoot,dest))
    return fail('OUTPUT_DIR_NOT_ALLOWED');
  mkdirSync(dest,{recursive:true,mode:0o700});
  if (data.awemeType!==68 && !data.videoPlayAddr)
    return fail('MEDIA_ADDRESS_UNAVAILABLE');
  if (data.awemeType===68 && !(data.images?.length>0))
    return fail('IMAGE_ADDRESS_UNAVAILABLE');
  try {
    const downloader=new DouyinDownloader({
      cookie,downloadPath:dest,naming:'{aweme_id}',maxConcurrency:1,
      timeout:18000,retries:1,folderize:false,cover:false,music:false,desc:false,
    });
    await downloader.createDownloadTasks(data,dest);
  } catch { return fail('DOWNLOAD_EXCEPTION'); }
  const files=readdirSync(dest).filter(x=>x.startsWith(id+'_')&&
    (x.endsWith('.mp4')||x.endsWith('.webp')||x.endsWith('.jpeg')||x.endsWith('.jpg')))
    .map(x=>resolve(dest,x))
    .filter(x=>safeInside(dest,x) && statSync(x).isFile() && statSync(x).size>=1024);
  const videos=files.filter(f=>f.endsWith('.mp4'));
  const images=files.filter(f=>/\.(webp|jpeg|jpg)$/.test(f));
  if ((imagePost ? images.length===0 : videos.length===0))
    return fail('DOWNLOAD_NO_VALID_FILE');
  process.stdout.write('VIDEOMIND_RESULT='+JSON.stringify({
    ok:true,meta,
    local_video_path:imagePost?'':videos[0],
    local_image_paths:imagePost?images:[],
    files_count:files.length,
  })+'\n');
}
main().catch(()=>fail('MEDIA_BRIDGE_EXCEPTION'));