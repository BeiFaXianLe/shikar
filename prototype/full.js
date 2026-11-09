/* 完整产品原型。全部为确定性模拟数据，独立运行，不请求业务 API。 */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const main = $('main'), drawer = $('drawer');
  const schoolLabel = {primary:'小学',middle:'初中',high:'高中'};
  const cities = ['北京','天津'];
  const lines = {北京:['4号线','6号线','10号线','13号线','15号线'],天津:['1号线','2号线','3号线','6号线']};
  const stations = {北京:{'4号线':['中关村','海淀黄庄','西直门'],'6号线':['朝阳门','青年路','通州北关'],'10号线':['知春路','国贸','三元桥'],'13号线':['五道口','回龙观','上地'],'15号线':['望京','关庄','清华东路西口']},天津:{'1号线':['小白楼','西南角','刘园'],'2号线':['天津站','鼓楼','咸阳路'],'3号线':['营口道','红旗南路','天津南站'],'6号线':['梅江道','乐园道','人民医院']}};
  const destinations = {北京:['国贸','中关村','望京','金融街'],天津:['和平路','滨海','天津站','大学城']};
  const facilities = [];
  let fid = 0;
  function facility(city,type,name,subtype='',grade='') {
    const row = {id:'poi-'+(++fid),city,type,name,subtype,grade,district:city==='北京'?'海淀':'和平'};
    facilities.push(row); return row;
  }
  const namesByCity = {
    北京:{primary:['中关村第一小学','海淀实验小学'],middle:['北大附中','清华附中'],high:['人大附中','北京市第八十中学'],hospital:['北京协和医院','北京大学第三医院','社区综合医院'],hsr:['北京朝阳站','清河站','北京南站']},
    天津:{primary:['岳阳道小学','中营小学'],middle:['汇文中学','南开中学'],high:['耀华中学','天津一中'],hospital:['天津医科大学总医院','天津医院','社区综合医院'],hsr:['天津站','天津西站','天津南站']}
  };
  cities.forEach(city=>{
    ['primary','middle','high'].forEach(stage=>namesByCity[city][stage].forEach(name=>facility(city,'school',name,stage)));
    namesByCity[city].hospital.forEach((name,i)=>facility(city,'hospital',name,'',i===2?'二甲':'三甲'));
    namesByCity[city].hsr.forEach(name=>facility(city,'hsr',name));
  });
  const communityNames = ['知春雅苑','清风里','榆树嘉园','文景家园','朗月花园','城南小筑','青禾雅居','海棠里','竹园新城','晴川苑','文华里','瑞景嘉园','松林家园','望舒花园','林语小筑','和风苑','书香里','晨曦家园','四季雅苑','枫林新城','清河小筑','溪岸嘉园','向阳里','云锦花园','兰庭家园','星河苑','南庭雅居','锦书里'];
  const communities = [];
  cities.forEach(city=>{
    const count = city==='北京'?28:8;
    for(let i=0;i<count;i++) {
      const all = facilities.filter(p=>p.city===city);
      const rel = all.map((p,j)=>({poiId:p.id,distance:Number((.25+((i*7+(j%3)*4)%75)/10).toFixed(2))}));
      const ls = lines[city]; const line = ls[i%ls.length];
      const lineDistances = ls.map((name,j)=>({name,station:stations[city][name][i%3],distance:Number((.15+((i*3+j*7)%20)/10).toFixed(2)),lineDistance:Number((.05+((i*2+j*5)%16)/10).toFixed(2))}));
      const primaryLine = lineDistances.find(l=>l.name===line);
      primaryLine.distance = Number((.25+(i%8)*.11).toFixed(2));
      communities.push({id:city+'-'+i,city,index:i,name:communityNames[i]+(city==='天津'?'·天津':''),district:city==='北京'?['海淀','朝阳','西城','昌平'][i%4]:['和平','河西','南开','北辰'][i%4],price:city==='北京'?38000+(i*3700)%62000:23000+(i*2700)%30000,relations:rel,lineDistances});
    }
  });
  function esc(value){return String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function money(n){return n.toLocaleString();}
  function empty(){return {city:'北京',lines:[],station:'',subwayMode:'station',subwayMax:1,min:'',max:'',q:'',stage:'',schoolMax:2,schoolPoi:'',grade:'',hospitalMax:3,hospitalPoi:'',hsrEnabled:false,hsrMax:5,hsrPoi:'',commuteEnabled:false,a:'国贸',b:'中关村',mode:'transit',w:50,balance:'weighted'};}
  let applied=empty(), draft=null, page=1, sort='default', currentView='', currentStage='primary', selectedFacility=null, opener=null, menuCity='北京', menuGrade='';
  let commute={city:'北京',a:'国贸',b:'中关村',mode:'transit',w:50,balance:'weighted',reuse:false};
  let commuteError='';
  function copy(obj){return {...obj,lines:obj.lines?obj.lines.slice():undefined};}
  function selectOptions(values,current,first){return (first?`<option value="">${esc(first)}</option>`:'')+values.map(v=>`<option value="${esc(Array.isArray(v)?v[0]:v)}" ${(Array.isArray(v)?v[0]:v)===current?'selected':''}>${esc(Array.isArray(v)?v[1]:v)}</option>`).join('');}
  function time(c,target,mode){
    const pos=destinations[c.city].indexOf(target), i=c.index;
    if(pos<0) return null;
    const sample=[12+(i*9)%53,14+((27-i)*7)%57,16+(i*5)%45,10+(i*13)%60][pos];
    return mode==='driving'?Math.round(sample*.78+5):sample;
  }
  function routeScore(c,config){
    const ta=time(c,config.a,config.mode), tb=time(c,config.b,config.mode);
    return {ta,tb,score:config.balance==='max'?Math.max(ta,tb):config.w/100*ta+(1-config.w/100)*tb};
  }
  function nearby(c,type,subtype='',grade='',poiId='') {
    return c.relations.map(r=>({...r,poi:facilities.find(p=>p.id===r.poiId)}))
      .filter(r=>r.poi.type===type&&(!subtype||r.poi.subtype===subtype)&&(!grade||r.poi.grade===grade)&&(!poiId||r.poi.id===poiId))
      .sort((a,b)=>a.distance-b.distance||a.poiId.localeCompare(b.poiId));
  }
  function matchLine(c,f){
    return c.lineDistances.filter(l=>(!f.lines.length||f.lines.includes(l.name))&&(!f.station||l.station===f.station))
      .map(l=>({...l,dist:f.subwayMode==='line'?l.lineDistance:l.distance})).sort((a,b)=>a.dist-b.dist)[0];
  }
  function findRows(f,includeCommute=true){
    return communities.filter(c=>c.city===f.city)
      .filter(c=>(!f.min||c.price>=Number(f.min))&&(!f.max||c.price<=Number(f.max))&&(!f.q||c.name.includes(f.q)))
      .filter(c=>!f.lines.length||matchLine(c,f)&&matchLine(c,f).dist<=f.subwayMax)
      .filter(c=>!f.stage&&!f.schoolPoi||nearby(c,'school',f.stage,'',f.schoolPoi)[0]?.distance<=f.schoolMax)
      .filter(c=>!f.grade&&!f.hospitalPoi||nearby(c,'hospital','',f.grade,f.hospitalPoi)[0]?.distance<=f.hospitalMax)
      .filter(c=>!f.hsrEnabled&&!f.hsrPoi||nearby(c,'hsr','','',f.hsrPoi)[0]?.distance<=f.hsrMax)
      .map(c=>({...c,sub:matchLine(c,f),...(includeCommute&&f.commuteEnabled?routeScore(c,f):{})}));
  }
  function validation(f){
    if(f.min!=null&&f.min!==''&&(!Number.isFinite(Number(f.min))||Number(f.min)<0)||f.max!=null&&f.max!==''&&(!Number.isFinite(Number(f.max))||Number(f.max)<0))return '价格请输入有效的非负数。';
    if(f.min!=null&&f.max!=null&&f.min!==''&&f.max!==''&&Number(f.min)>Number(f.max))return '最低均价不能超过最高均价。';
    if(f.commuteEnabled||currentView==='commute'){
      if(!destinations[f.city].includes(f.a)||!destinations[f.city].includes(f.b))return '原型只提供预设地点的模拟时间，请选择下方提示中的地点。正式版将支持地址搜索定位。';
    }
    return '';
  }
  function changeCity(f,city){
    return {...f,city,lines:[],station:'',schoolPoi:'',hospitalPoi:'',hsrPoi:'',a:destinations[city][0],b:destinations[city][1]};
  }
  function title(eyebrow,heading,sub,note=''){
    return `<div class="eyebrow">${eyebrow}</div><div class="heading"><div><h1>${heading}</h1><p class="subtitle">${sub}</p></div>${note?`<div class="heading-note">${note}</div>`:''}</div>`;
  }
  function chips(){
    const items=[['city',applied.city]];
    applied.lines.forEach(l=>items.push(['line:'+l,l]));
    if(applied.min||applied.max)items.push(['price',`${applied.min||'不限'}–${applied.max||'不限'} 元/㎡`]);
    if(applied.lines.length)items.push(['subway',`${applied.station|| (applied.subwayMode==='line'?'距线路':'距站')} ≤ ${applied.subwayMax} km`]);
    if(applied.q)items.push(['q','名称：'+applied.q]);
    if(applied.stage||applied.schoolPoi)items.push(['school',`${applied.schoolPoi?facilities.find(p=>p.id===applied.schoolPoi).name:schoolLabel[applied.stage]} ≤ ${applied.schoolMax} km`]);
    if(applied.grade||applied.hospitalPoi)items.push(['hospital',`${applied.hospitalPoi?facilities.find(p=>p.id===applied.hospitalPoi).name:applied.grade+'医院'} ≤ ${applied.hospitalMax} km`]);
    if(applied.hsrEnabled||applied.hsrPoi)items.push(['hsr',`${applied.hsrPoi?facilities.find(p=>p.id===applied.hsrPoi).name:'高铁站'} ≤ ${applied.hsrMax} km`]);
    if(applied.commuteEnabled)items.push(['commute',`${applied.a} / ${applied.b} · ${applied.balance==='max'?'最慢一方优先':'A '+applied.w+'%'}`]);
    return `<div class="chips"><span class="chips-label">已选条件</span>${items.map(([k,v])=>`<button class="chip" data-remove="${esc(k)}" aria-label="删除条件：${esc(v)}">${esc(v)}<span>×</span></button>`).join('')}<button class="text-button" id="clear-all">清除其他条件</button></div>`;
  }
  function removeChip(key){
    if(key==='city'){applied=empty();}
    else if(key.startsWith('line:')){applied.lines=applied.lines.filter(l=>l!==key.slice(5));applied.station='';}
    else if(key==='price'){applied.min='';applied.max='';}
    else if(key==='subway'){applied.subwayMax=1;applied.subwayMode='station';applied.station='';}
    else if(key==='school'){applied.stage='';applied.schoolPoi='';}
    else if(key==='hospital'){applied.grade='';applied.hospitalPoi='';}
    else if(key==='hsr'){applied.hsrEnabled=false;applied.hsrPoi='';}
    else if(key==='commute'){applied.commuteEnabled=false;}
    else applied[key]='';
    page=1;renderFind();
  }
  function facilityButton(r){return `<button class="facility-link" data-facility="${r.poi.id}">${esc(r.poi.name)} · ${r.distance.toFixed(2)} km</button>`;}
  function communityRow(c,index,f){
    const school=nearby(c,'school',f.stage||'primary','',f.schoolPoi)[0];
    const hospital=nearby(c,'hospital','',f.grade,f.hospitalPoi)[0], hsr=nearby(c,'hsr','','',f.hsrPoi)[0];
    const sub=c.sub||matchLine(c,f);
    const meta=[`<span class="subway-meta">▪ ${sub.name} · ${f.subwayMode==='line'?'线路走向':sub.station} ${sub.dist.toFixed(2)} km</span>`];
    if(school)meta.push(`<span>${schoolLabel[school.poi.subtype]} ${school.distance.toFixed(1)} km</span>`);
    if(hospital)meta.push(`<span>${hospital.poi.grade}医院 ${hospital.distance.toFixed(1)} km</span>`);
    if(f.hsrEnabled||f.hsrPoi)meta.push(`<span>高铁 ${hsr.distance.toFixed(1)} km</span>`);
    if(f.commuteEnabled)meta.push(`<span class="score">通勤 A / B　${c.ta} / ${c.tb} 分 · ${c.score.toFixed(1)}</span>`);
    return `<article class="result-row"><div class="row-top"><span class="number">${String(index).padStart(2,'0')}</span><div><div class="community-name">${esc(c.name)}<span class="location">${c.city} · ${c.district}</span></div><div class="row-meta">${meta.join('')}</div></div><div class="price">${money(c.price)}<small>元/㎡</small><div class="price-note">最新参考均价 · 演示</div></div></div><details class="details"><summary>查看周边设施与通勤 →</summary><div class="details-body">${['primary','middle','high'].map(s=>{const r=nearby(c,'school',s)[0];return `<div class="detail-pair"><span>附近${schoolLabel[s]}</span>${facilityButton(r)}</div>`;}).join('')}<div class="detail-pair"><span>附近医院 · ${hospital.poi.grade}</span>${facilityButton(hospital)}</div><div class="detail-pair"><span>高铁站</span>${facilityButton(hsr)}</div><div class="detail-pair"><span>双点通勤 · ${f.a} / ${f.b}</span>${time(c,f.a,f.mode)} / ${time(c,f.b,f.mode)} 分钟（模拟）</div></div></details></article>`;
  }
  function renderFind(){
    currentView='find';
    let rows=findRows(applied);
    if(sort==='price_asc')rows.sort((a,b)=>a.price-b.price);
    if(sort==='price_desc')rows.sort((a,b)=>b.price-a.price);
    if(sort==='distance')rows.sort((a,b)=>a.sub.dist-b.sub.dist);
    if(applied.commuteEnabled||sort==='commute'){
      rows=rows.map(c=>({...c,...routeScore(c,applied)})).sort((a,b)=>a.score-b.score);
    }
    const pages=Math.max(1,Math.ceil(rows.length/8));page=Math.min(page,pages);
    main.innerHTML=title('01 / COMMUNITY EXPLORER','围绕生活，找到一个小区。','地铁沿线、附近学校、就医与出行，把重要的偏好放在一起。','先选三项，再按需展开。<br>原型仅用于评审体验。')+
      `<div class="primary-filters"><div class="field"><label for="main-city">01 / 城市</label><select id="main-city">${selectOptions(cities,applied.city)}</select></div><div class="field"><label>02 / 地铁线路</label><button class="button line-summary" id="choose-lines">${esc(applied.lines.length?applied.lines.join('、'):'不限线路')}　⌄</button></div><div class="field"><label>03 / 参考均价（元/㎡）</label><div class="price-pair"><input id="price-min" type="number" min="0" placeholder="不限" value="${esc(applied.min)}" aria-label="最低均价"><span>—</span><input id="price-max" type="number" min="0" placeholder="不限" value="${esc(applied.max)}" aria-label="最高均价"></div></div><button class="button" id="more-filters">更多条件　＋</button></div><button class="button mobile-filter" id="mobile-filters"><span>☷　筛选</span><span>设置生活偏好　→</span></button><p id="main-error" class="form-error" role="alert"></p>`+chips()+
      `<div class="results-bar"><h2>小区目录</h2><span class="result-count" aria-live="polite">${rows.length} 个匹配结果</span><div class="results-tools"><input class="search" id="find-q" aria-label="搜索小区名称" placeholder="搜索小区名称" value="${esc(applied.q)}"><select id="find-sort" aria-label="结果排序">${selectOptions([['default','默认排序'],['distance','地铁距离 ↑'],['price_asc','均价从低到高'],['price_desc','均价从高到低'],['commute','通勤分数 ↑']],applied.commuteEnabled?'commute':sort)}</select></div></div>`+
      (rows.length?rows.slice((page-1)*8,page*8).map((c,i)=>communityRow(c,(page-1)*8+i+1,applied)).join(''):`<div class="empty"><h3>没有满足这些偏好的小区</h3><p>放宽距离、价格，或删除一个条件再试试。</p><button class="button" id="empty-reset">清除其他条件</button></div>`)+
      `<div class="pager"><span>共 ${rows.length} 个小区 · 第 ${page} / ${pages} 页</span><div><button class="button" id="prev" ${page===1?'disabled':''}>← 上一页</button><button class="button" id="next" ${page===pages?'disabled':''}>下一页 →</button></div></div><p class="footnote">全部为模拟数据。设施距离为直线距离，通勤为预设样例；小学 / 初中 / 高中展示附近学校，不代表对口关系。</p>`;
    $('main-city').onchange=e=>{applied=changeCity(applied,e.target.value);page=1;renderFind();};
    ['price-min','price-max'].forEach(id=>$(id).onchange=()=>{const next={...applied,min:$('price-min').value,max:$('price-max').value};const error=validation(next);if(error){$('main-error').textContent=error;return;}applied=next;page=1;renderFind();});
    ['choose-lines','more-filters','mobile-filters'].forEach(id=>$(id).onclick=e=>openDrawer(e.currentTarget));
    document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>removeChip(b.dataset.remove));
    $('clear-all').onclick=()=>{applied=changeCity(empty(),applied.city);page=1;renderFind();};
    if($('empty-reset'))$('empty-reset').onclick=$('clear-all').onclick;
    $('find-q').onchange=e=>{applied.q=e.target.value.trim();page=1;renderFind();};
    $('find-q').onkeydown=e=>{if(e.key==='Enter')e.target.blur();};
    $('find-sort').onchange=e=>{sort=e.target.value;if(sort==='commute')applied.commuteEnabled=true;page=1;renderFind();};
    $('prev').onclick=()=>{page--;renderFind();};$('next').onclick=()=>{page++;renderFind();};
    document.querySelectorAll('[data-facility]').forEach(b=>b.onclick=()=>showFacility(b.dataset.facility));
  }
  function field(label,html){return `<div class="field"><label>${label}</label>${html}</div>`;}
  function range(id,label,value,min,max,unit='km'){
    return `<div class="field"><div class="range-label"><label for="${id}">${label}</label><output id="${id}-value">${Number(value).toFixed(unit==='%'?0:1)} ${unit}</output></div><input id="${id}" type="range" min="${min}" max="${max}" step="${unit==='%'?5:.1}" value="${value}"></div>`;
  }
  function group(number,name,html){return `<section class="group"><h3><span class="group-number">${number}</span>${name}</h3>${html}</section>`;}
  function openDrawer(source){opener=source;draft=copy(applied);$('drawer-error').textContent='';renderDrawer();document.body.style.overflow='hidden';drawer.showModal();}
  function renderDrawer(){
    const lineList=lines[draft.city];
    const chosenStations=[...new Set(draft.lines.flatMap(l=>stations[draft.city][l]))];
    $('drawer-content').innerHTML=
      `<section class="group mobile-main"><h3><span class="group-number">00</span>城市与价格</h3>${field('城市',`<select id="d-city" aria-label="筛选城市">${selectOptions(cities,draft.city)}</select>`)}${field('参考均价（元/㎡）',`<div class="price-pair"><input id="d-min" aria-label="筛选最低均价" type="number" min="0" placeholder="不限" value="${esc(draft.min)}"><span>—</span><input id="d-max" aria-label="筛选最高均价" type="number" min="0" placeholder="不限" value="${esc(draft.max)}"></div>`)}</section>`+
      group('01','地铁出行',field('线路 · 可多选',`<div class="check-grid">${lineList.map(l=>`<label><input type="checkbox" data-line="${l}" ${draft.lines.includes(l)?'checked':''}>${l}</label>`).join('')}</div>`)+field('站点',`<select id="d-station" aria-label="地铁站点" ${!draft.lines.length||draft.subwayMode==='line'?'disabled':''}>${selectOptions(chosenStations,draft.station,'所选线路全部站点')}</select>`)+field('距离口径',`<select id="d-submode" aria-label="地铁距离口径">${selectOptions([['station','距地铁站'],['line','距线路走向']],draft.subwayMode)}</select>`)+range('d-submax','最大直线距离',draft.subwayMax,.1,5)+`<p class="group-note">同类线路取任一满足；不同类别条件需同时满足。</p>`)+
      group('02','附近学校',field('学校阶段',`<select id="d-stage" aria-label="学校阶段">${selectOptions(Object.entries(schoolLabel),draft.stage,'不限制学校')}</select>`)+range('d-schoolmax','学校最大距离',draft.schoolMax,.1,5)+(draft.schoolPoi?`<p class="group-note">已指定：${esc(facilities.find(p=>p.id===draft.schoolPoi).name)} <button class="text-button" id="unset-school">取消指定</button></p>`:'')+`<p class="group-note">按距离找学校，不等于对口入学；重点属性不在本原型评分。</p>`)+
      group('03','就医便利',field('医院等级',`<select id="d-grade" aria-label="医院等级">${selectOptions(['三甲','二甲'],draft.grade,'不限制医院')}</select>`)+range('d-hospitalmax','医院最大距离',draft.hospitalMax,.1,8)+(draft.hospitalPoi?`<p class="group-note">已指定：${esc(facilities.find(p=>p.id===draft.hospitalPoi).name)} <button class="text-button" id="unset-hospital">取消指定</button></p>`:'')+`<p class="group-note">等级和距离由同一家医院同时满足。</p>`)+
      group('04','高铁出行',`<label class="check-label"><input id="d-hsr" type="checkbox" ${draft.hsrEnabled?'checked':''}>按高铁站距离筛选</label>`+range('d-hsrmax','高铁站最大距离',draft.hsrMax,.1,15)+(draft.hsrPoi?`<p class="group-note">已指定：${esc(facilities.find(p=>p.id===draft.hsrPoi).name)} <button class="text-button" id="unset-hsr">取消指定</button></p>`:''))+
      group('05','双点通勤',`<label class="check-label"><input id="d-commute" type="checkbox" ${draft.commuteEnabled?'checked':''}>按双点通勤优先排序</label><div class="commute-fields">${field('地点 A',`<input id="d-a" aria-label="筛选地点 A" list="d-destinations" value="${esc(draft.a)}">`)}${field('地点 B',`<input id="d-b" aria-label="筛选地点 B" list="d-destinations" value="${esc(draft.b)}"><datalist id="d-destinations">${destinations[draft.city].map(s=>`<option value="${s}">`).join('')}</datalist>`)}${field('通勤方式',`<select id="d-mode" aria-label="筛选通勤方式">${selectOptions([['transit','公交 / 地铁'],['driving','驾车']],draft.mode)}</select>`)}${range('d-weight','地点 A 权重',draft.w,0,100,'%')}${field('均衡口径',`<select id="d-balance" aria-label="筛选均衡口径">${selectOptions([['weighted','加权均值'],['max','最慢一方优先']],draft.balance)}</select>`)}</div><p class="group-note">模拟地点：${destinations[draft.city].join(' / ')}。权重只改变排序。</p>`)+`<p class="group-note">关闭或取消不应用修改。所有条件和距离使用演示数据。</p>`;
    $('d-city').onchange=e=>{draft=changeCity(draft,e.target.value);renderDrawer();};
    [['d-min','min'],['d-max','max'],['d-a','a'],['d-b','b']].forEach(([id,key])=>$(id).oninput=e=>draft[key]=e.target.value.trim());
    document.querySelectorAll('[data-line]').forEach(c=>c.onchange=()=>{draft.lines=[...document.querySelectorAll('[data-line]:checked')].map(e=>e.dataset.line);draft.station='';const pos=$('drawer-content').scrollTop;renderDrawer();$('drawer-content').scrollTop=pos;});
    [['d-stage','stage'],['d-grade','grade'],['d-station','station'],['d-mode','mode'],['d-balance','balance']].forEach(([id,key])=>$(id).onchange=e=>{draft[key]=e.target.value;if(key==='stage')draft.schoolPoi='';if(key==='grade')draft.hospitalPoi='';});
    $('d-submode').onchange=e=>{draft.subwayMode=e.target.value;draft.station='';const pos=$('drawer-content').scrollTop;renderDrawer();$('drawer-content').scrollTop=pos;};
    [['d-submax','subwayMax'],['d-schoolmax','schoolMax'],['d-hospitalmax','hospitalMax'],['d-hsrmax','hsrMax'],['d-weight','w']].forEach(([id,key])=>$(id).oninput=e=>{draft[key]=Number(e.target.value);$(id+'-value').textContent=Number(e.target.value).toFixed(key==='w'?0:1)+(key==='w'?' %':' km');});
    $('d-hsr').onchange=e=>{draft.hsrEnabled=e.target.checked;if(!e.target.checked)draft.hsrPoi='';};
    $('d-commute').onchange=e=>draft.commuteEnabled=e.target.checked;
    [['unset-school','schoolPoi'],['unset-hospital','hospitalPoi'],['unset-hsr','hsrPoi']].forEach(([id,key])=>{if($(id))$(id).onclick=()=>{draft[key]='';renderDrawer();};});
  }
  $('close-drawer').onclick=$('cancel-drawer').onclick=()=>drawer.close();
  $('reset-draft').onclick=()=>{draft=changeCity(empty(),draft.city);renderDrawer();};
  $('drawer-form').onsubmit=e=>{e.preventDefault();const error=validation(draft);if(error){$('drawer-error').textContent=error;return;}applied=copy(draft);page=1;if(applied.commuteEnabled)sort='commute';drawer.close();renderFind();};
  drawer.addEventListener('close',()=>{document.body.style.overflow='';if(opener?.isConnected)opener.focus();});
  drawer.addEventListener('click',e=>{const r=drawer.getBoundingClientRect();if(e.target===drawer&&(e.clientX<r.left||e.clientX>r.right))drawer.close();});

  function registry(type){
    return facilities.filter(p=>p.city===menuCity&&p.type===type&&(type!=='school'||p.subtype===currentStage)&&(!menuGrade||type!=='hospital'||p.grade===menuGrade)).map(p=>{
      const all=communities.filter(c=>c.city===p.city).map(c=>({c,distance:c.relations.find(r=>r.poiId===p.id).distance})).sort((a,b)=>a.distance-b.distance||a.c.id.localeCompare(b.c.id));
      const around=all.filter(r=>r.distance<=5);
      return {...p,all,count:around.length,avg:around.length?around.reduce((s,r)=>s+r.distance,0)/around.length:null,min:all[0].distance};
    });
  }
  function renderFacilities(view){
    currentView=view;
    const type={schools:'school',hospitals:'hospital',hsr:'hsr'}[view];
    const titles={schools:['02 / SCHOOL ATLAS','学校，和它附近的小区。','小学、初中、高中各自查看。点击学校，按距离浏览周边小区。'],hospitals:['03 / HEALTHCARE ATLAS','让就医，离生活近一点。','查看医院明细与周边小区 Top20，也可以将医院带回首页筛选。'],hsr:['04 / RAIL ATLAS','把下一次出发，放近一点。','从高铁站出发，找到直线距离最近的小区。']};
    const list=registry(type);
    if(!list.find(p=>p.id===selectedFacility))selectedFacility=list[0]?.id;
    main.innerHTML=title(...titles[view],'Top20 按距离排列。<br>医院与学校不作质量排名。')+
      `<div class="menu-toolbar"><div class="field"><label for="menu-city">城市</label><select id="menu-city">${selectOptions(cities,menuCity)}</select></div>${type==='hospital'?`<div class="field"><label for="menu-grade">医院等级</label><select id="menu-grade">${selectOptions(['三甲','二甲'],menuGrade,'全部等级')}</select></div>`:`<span class="result-count">${list.length} 个${type==='school'?'学校':'站点'} · 演示数据</span>`}</div>`+
      (type==='school'?`<div class="tabs" role="tablist" aria-label="学校阶段">${Object.entries(schoolLabel).map(([key,label])=>`<button role="tab" aria-selected="${key===currentStage}" class="${key===currentStage?'selected':''}" data-stage="${key}">${label}</button>`).join('')}</div>`:'')+
      `<div class="facility-layout"><div class="facility-list">${list.map(p=>`<button class="facility-row ${p.id===selectedFacility?'active-facility':''}" data-poi="${p.id}"><strong>${esc(p.name)}</strong><span class="badge">${type==='school'?schoolLabel[p.subtype]:type==='hospital'?p.grade:'高铁站'}</span><small class="f-meta"><span>${p.city} · ${p.district}（模拟）</span><span>5 km 内 ${p.count} 个小区</span></small><small>${type==='school'?`平均距离 ${p.avg===null?'—':p.avg.toFixed(2)+' km'}`:`最近小区 ${p.min.toFixed(2)} km`}　查看周边 Top20 →</small></button>`).join('')}</div><section class="facility-detail" id="poi-detail"></section></div><p class="footnote">模拟排名仅按设施与小区的直线距离。演示范围为 15 km；不足 20 个时展示实际条数。等级、阶段、位置均待真实数据校核。</p>`;
    $('menu-city').onchange=e=>{menuCity=e.target.value;selectedFacility=null;renderFacilities(view);};
    if($('menu-grade'))$('menu-grade').onchange=e=>{menuGrade=e.target.value;selectedFacility=null;renderFacilities(view);};
    document.querySelectorAll('[data-stage]').forEach(b=>b.onclick=()=>{currentStage=b.dataset.stage;selectedFacility=null;renderFacilities(view);});
    document.querySelectorAll('[data-poi]').forEach(b=>b.onclick=()=>{selectedFacility=b.dataset.poi;renderFacilities(view);});
    renderPoiDetail(list.find(p=>p.id===selectedFacility));
  }
  function renderPoiDetail(p){
    if(!p){$('poi-detail').innerHTML='<div class="empty">暂无设施</div>';return;}
    const top=p.all.filter(r=>r.distance<=15).slice(0,20);
    $('poi-detail').innerHTML=`<span class="eyebrow">THE NEIGHBOURHOOD AROUND</span><h2>${esc(p.name)}</h2><p class="detail-caption">${p.city} · ${p.type==='school'?schoolLabel[p.subtype]:p.type==='hospital'?p.grade:'高铁站'} · 模拟设施资料</p><div class="detail-head-actions"><strong>周边小区 Top20 <span class="result-count">${top.length} 个</span></strong><button id="use-poi" class="button">带入首页筛选 →</button></div><ol class="rank-list">${top.map((r,i)=>`<li><span class="rank">${String(i+1).padStart(2,'0')}</span><span><strong>${esc(r.c.name)}</strong><small>${r.c.district} · ${money(r.c.price)} 元/㎡</small></span><span class="rank-distance">${r.distance.toFixed(2)} km</span></li>`).join('')}</ol><p class="group-note">距离相同时按小区 ID 排序。带入首页默认筛选该设施 3 km 内的小区。</p>`;
    $('use-poi').onclick=()=>{
      if(applied.city!==p.city)applied=changeCity(applied,p.city);
      if(p.type==='school'){applied.stage=p.subtype;applied.schoolPoi=p.id;applied.schoolMax=3;}
      if(p.type==='hospital'){applied.grade=p.grade;applied.hospitalPoi=p.id;applied.hospitalMax=3;}
      if(p.type==='hsr'){applied.hsrEnabled=true;applied.hsrPoi=p.id;applied.hsrMax=3;}
      page=1;location.hash='find';
    };
  }
  function showFacility(id){
    const p=facilities.find(p=>p.id===id);selectedFacility=id;menuCity=p.city;menuGrade='';if(p.subtype)currentStage=p.subtype;
    location.hash={school:'schools',hospital:'hospitals',hsr:'hsr'}[p.type];
  }
  function renderCommute(){
    currentView='commute';
    let pool=commute.reuse?findRows(applied,false).filter(c=>c.city===commute.city):communities.filter(c=>c.city===commute.city);
    const valid=validation({...commute,commuteEnabled:true});
    const rows=valid?[]:pool.map(c=>({...c,...routeScore(c,commute)})).sort((a,b)=>a.score-b.score||a.id.localeCompare(b.id));
    main.innerHTML=title('05 / TWO DESTINATIONS','两个人的通勤，一个更好的折中。','录入两个地点，调节优先级，看看小区排名怎样变化。','通勤为预设样例。<br>未调用真实路径 API。')+
      `<section class="commute-panel"><div class="destinations">${field('城市',`<select id="c-city" aria-label="通勤城市">${selectOptions(cities,commute.city)}</select>`)}${field('地点 A',`<input id="c-a" aria-label="地点 A" list="commute-destinations" value="${esc(commute.a)}" placeholder="输入工作地点">`)}${field('地点 B',`<input id="c-b" aria-label="地点 B" list="commute-destinations" value="${esc(commute.b)}" placeholder="输入工作地点"><datalist id="commute-destinations">${destinations[commute.city].map(s=>`<option value="${s}">`).join('')}</datalist>`)}${field('通勤方式',`<select id="c-mode" aria-label="通勤方式">${selectOptions([['transit','公交 / 地铁'],['driving','驾车']],commute.mode)}</select>`)}</div><div class="weight-config"><div><div class="weight-heading">优先照顾哪一边？<span id="weight-value">A ${commute.w}% / B ${100-commute.w}%</span></div><input id="c-weight" aria-label="地点 A 权重" type="range" min="0" max="100" step="5" value="${commute.w}" ${commute.balance==='max'?'disabled':''}><div class="weight-scale"><span>B 优先</span><span>均衡</span><span>A 优先</span></div><div class="preset-row"><button class="button ${commute.w===80&&commute.balance==='weighted'?'selected':''}" data-weight="80">A 优先 · 80%</button><button class="button ${commute.w===50&&commute.balance==='weighted'?'selected':''}" data-weight="50">均衡 · 50 / 50</button><button class="button ${commute.w===20&&commute.balance==='weighted'?'selected':''}" data-weight="20">B 优先 · 80%</button></div></div><div>${field('均衡策略',`<select id="c-balance" aria-label="均衡策略">${selectOptions([['weighted','加权均值'],['max','最慢一方优先']],commute.balance)}</select>`)}<p class="balance-description">${commute.balance==='weighted'?`分数 = ${commute.w}% × 到 A 时间 + ${100-commute.w}% × 到 B 时间。50 / 50 不保证双方时间都短。`:'分数 = max(到 A 时间, 到 B 时间)。降低最慢一方的时间；该策略不使用权重。'}</p></div></div><div class="commute-actions"><label class="candidate-toggle"><input id="c-reuse" type="checkbox" ${commute.reuse?'checked':''}>只比较首页筛选候选 · ${applied.city}</label><button id="c-calculate" class="button primary">比较通勤 →</button></div><p class="commute-hint">可试用地点：${destinations[commute.city].join(' / ')}。正式版提供地址搜索与位置确认。</p><p id="commute-error" class="form-error" role="alert">${esc(commuteError||valid)}</p></section><div class="commute-summary"><span>${esc(commute.a)} ↔ ${esc(commute.b)}</span><span>${rows.length} 个候选 · ${commute.mode==='transit'?'公交 / 地铁':'驾车'}</span><span>按${commute.balance==='max'?'最慢一方时间':'加权分数'}升序 · 模拟</span></div><div class="commute-row commute-labels"><span class="rank">排名</span><span>小区 / 均价</span><span style="text-align:right">到 A</span><span style="text-align:right">到 B</span><span style="text-align:right">${commute.balance==='max'?'最慢一方':'加权分数'}</span></div><div id="commute-results">${rows.length?rows.slice(0,20).map((c,i)=>`<article class="commute-row"><span class="rank number">${String(i+1).padStart(2,'0')}</span><div><div class="community-name">${esc(c.name)}</div><span class="location">${c.district} · ${money(c.price)} 元/㎡</span></div><div class="time">${c.ta}<small> 分</small></div><div class="time">${c.tb}<small> 分</small></div><div class="score-box">${c.score.toFixed(1)}<small> 分</small></div></article>`).join(''):'<div class="empty">没有可比较的候选，请调整地点或首页条件。</div>'}</div><p class="footnote">预设时间用于验证交互与排序。真实时间随交通方式、出发时段和路线策略变化。正式版先粗筛候选，再调用路径服务并缓存。</p>`;
    $('c-city').onchange=e=>{commute.city=e.target.value;commute.a=destinations[commute.city][0];commute.b=destinations[commute.city][1];commuteError='';renderCommute();};
    $('c-calculate').onclick=()=>{commute.a=$('c-a').value.trim();commute.b=$('c-b').value.trim();commuteError=validation({...commute,commuteEnabled:true});renderCommute();};
    ['c-a','c-b'].forEach(id=>$(id).onkeydown=e=>{if(e.key==='Enter')$('c-calculate').click();});
    $('c-mode').onchange=e=>{commute.mode=e.target.value;renderCommute();};
    $('c-balance').onchange=e=>{commute.balance=e.target.value;renderCommute();};
    $('c-reuse').onchange=e=>{commute.reuse=e.target.checked;renderCommute();};
    $('c-weight').oninput=e=>{commute.w=Number(e.target.value);$('weight-value').textContent=`A ${commute.w}% / B ${100-commute.w}%`;};
    $('c-weight').onchange=()=>renderCommute();
    document.querySelectorAll('[data-weight]').forEach(b=>b.onclick=()=>{commute.w=Number(b.dataset.weight);commute.balance='weighted';renderCommute();});
  }
  function navigate(){
    if(drawer.open)drawer.close();
    const view=location.hash.slice(1)||'find';
    document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.dataset.view===view));
    if(view==='find')renderFind();else if(['schools','hospitals','hsr'].includes(view))renderFacilities(view);else if(view==='commute')renderCommute();else{location.hash='find';return;}
    window.scrollTo(0,0);
  }
  window.addEventListener('hashchange',navigate);navigate();
})();
