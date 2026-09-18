"use strict";
const ScoreLyrics=(()=>{
  const drafts=new Map();
  function mount({root,revision,esc,error,selected,save}){
    const data=revision.score_lyric_map;if(!data)return;
    if(!data.items.length){root.textContent='当前没有可配谱的歌词。';return;}
    if(!data.notes.length){root.textContent='当前谱面没有可对应歌词的音符，请先补充人声旋律。';return;}
    if(!data.editable){root.textContent='当前记谱尚不能可靠定位音符，请先修正乐谱。原有参考与试听仍可查看。';return;}
    const key=revision.revision.snapshot_sha256+':'+data.generation;
    let rows=structuredClone(drafts.get(key)||data.rows),index=0,dirty=false;
    root.innerHTML=`<p class="hint">把歌词明确配到输入谱的音符上。小节和音符从 1 开始计数，休止不计作音符。一句可以跨小节，一个字词组可以跨多个音符。理论时间不代表实际演唱时间。</p>
      <label>选择歌词行<select class="mapping-line">${data.items.map((l,i)=>`<option value="${i}">${l.line} · ${esc(l.section)} · ${esc(l.text)}</option>`).join('')}</select></label>
      <div class="mapping-form"></div><div class="review-controls"><button class="map-apply">应用当前行到草稿</button><button class="map-clear">清除当前行配谱</button><button class="map-save">保存全部配谱关系</button><button class="map-discard">放弃未保存配谱</button></div><p class="mapping-status" role="status"></p>`;
    const find=q=>root.querySelector(q),status=find('.mapping-status');let voice,start,end,units;
    const changed=()=>{dirty=true;status.textContent='当前行有修改，应用到草稿或保存后再切换歌词行。';};
    function draw(){
      dirty=false;const line=data.items[index],row=rows.find(r=>r.line===line.line);
      voice=row?.voice||data.notes.find(n=>n.voice.toLowerCase()==='vocal')?.voice||data.notes[0]?.voice;
      start=row?.range?.slice(0,2).join(':')||'';end=row?.range?.slice(2).join(':')||'';
      units=(row?.units||[]).map(u=>({text:u[0],start:u.slice(1,3).join(':'),end:u.slice(3).join(':')}));
      form();
    }
    function options(value){return '<option value="">尚未指定</option>'+data.notes.filter(n=>n.voice===voice).map(n=>{
      const id=n.bar+':'+n.note;
      return `<option value="${id}" ${value===id?'selected':''}>第 ${n.bar} 小节 · 音符 ${n.note} ${esc(n.pitch)}${n.start==null?'':` · ${n.start.toFixed(2)}–${n.end.toFixed(2)} 秒`}</option>`;
    }).join('');}
    function form(){
      const box=find('.mapping-form');
      box.innerHTML=`<p><strong>${esc(data.items[index].text)}</strong></p><label>谱面声部<select class="mapping-voice">${[...new Set(data.notes.map(n=>n.voice))].map(v=>`<option value="${esc(v)}" ${v===voice?'selected':''}>${esc(v)}</option>`).join('')}</select></label>
        <div class="mapping-range"><label>句子起始音符<select class="mapping-start">${options(start)}</select></label><label>句子结束音符<select class="mapping-end">${options(end)}</select></label></div><button class="map-selection">使用当前选中的小节范围</button>
        <details ${units.length?'open':''}><summary>细化字词对应（可选）</summary><p class="hint">有字词配谱时，句子范围由首尾字词确定。所有字词按顺序覆盖这句歌词；可以分组，不能把同一音符重复分给不同字词。</p><div class="mapping-units">${units.map((u,i)=>`<div class="mapping-unit"><label>字词 ${i+1}<input data-text="${i}" value="${esc(u.text)}"></label><label>开始音符<select data-start="${i}">${options(u.start)}</select></label><label>结束音符<select data-end="${i}">${options(u.end)}</select></label><button data-delete="${i}">删除</button></div>`).join('')}</div><button class="map-unit-add">增加字词组</button><button class="map-split">按字拆分，手动选音符</button><button class="map-phrase-only">改为仅句级配谱</button></details>`;
      find('.mapping-voice').onchange=e=>{voice=e.target.value;start=end='';units=[];form();changed();};
      find('.mapping-start').onchange=e=>{start=e.target.value;changed();};find('.mapping-end').onchange=e=>{end=e.target.value;changed();};
      find('.map-selection').onclick=()=>{const available=data.notes.filter(n=>n.voice===voice&&selected().has(n.bar_id));if(!available.length){status.textContent='请先在乐谱中选择包含该声部音符的小节。';return;}
        start=available[0].bar+':'+available[0].note;end=available.at(-1).bar+':'+available.at(-1).note;units=[];form();changed();};
      box.querySelectorAll('[data-text],[data-start],[data-end]').forEach(el=>el.oninput=()=>{const k=el.hasAttribute('data-text')?'text':el.hasAttribute('data-start')?'start':'end';units[Number(el.dataset[k])][k]=el.value;changed();});
      box.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>{units.splice(+b.dataset.delete,1);form();changed();});
      find('.map-unit-add').onclick=()=>{units.push({text:'',start:'',end:''});form();changed();};
      find('.map-split').onclick=()=>{units=Array.from(data.items[index].text).filter(c=>/[\p{L}\p{N}]/u.test(c)).map(text=>({text,start:'',end:''}));form();changed();};
      find('.map-phrase-only').onclick=()=>{units=[];form();changed();};
    }
    function apply(){
      const note=value=>{if(!/^\d+:\d+$/.test(value))throw new Error('请为每个配谱范围选择开始和结束音符。');return value.split(':').map(Number);};
      const row={line:data.items[index].line,voice,units:[]};
      if(units.length){row.units=units.map(u=>{if(!u.text.trim())throw new Error('字词不能为空。');return [u.text,...note(u.start),...note(u.end)];});row.range=row.units[0].slice(1,3).concat(row.units.at(-1).slice(3));}
      else row.range=[...note(start),...note(end)];
      rows=rows.filter(r=>r.line!==row.line).concat([row]).sort((a,b)=>a.line-b.line);drafts.set(key,structuredClone(rows));dirty=false;
      status.textContent=`草稿中已有 ${rows.length}/${data.items.length} 行配谱，尚未保存。`;
    }
    find('.mapping-line').onchange=e=>{if(dirty){e.target.value=String(index);status.textContent='请先应用当前行或清除当前行修改，再切换。';return;}index=+e.target.value;draw();};
    find('.map-apply').onclick=()=>{try{apply();}catch(e){status.textContent=error(e);}};
    find('.map-clear').onclick=()=>{rows=rows.filter(r=>r.line!==data.items[index].line);drafts.set(key,structuredClone(rows));draw();status.textContent='当前行配谱已从草稿清除，保存后生效。';};
    find('.map-save').onclick=async()=>{try{if(dirty)apply();await save(rows);drafts.delete(key);}catch(e){status.textContent=error(e);}};
    find('.map-discard').onclick=()=>{drafts.delete(key);mount({root,revision,esc,error,selected,save});};
    draw();
  }
  return {mount};
})();
