(function(){
  var DATA_URL = "https://raw.githubusercontent.com/Oshinji/clinics-wait-time/main/data/status.json";
  var INTERVAL  = 15 * 60 * 1000;

  /* ── CSS注入 ── */
  var css = '#mw-widget{font-family:"Hiragino Kaku Gothic ProN","Meiryo",sans-serif;max-width:560px;margin:8px auto 24px}'
    + '#mw-widget .mw-card{background:#fff;border:2px solid #b3d4f0;border-radius:12px;padding:28px 32px;text-align:center;box-shadow:0 2px 12px rgba(21,101,192,.08)}'
    + '#mw-widget .mw-label{display:inline-block;background:#1565c0;color:#fff;font-size:12px;font-weight:700;letter-spacing:.1em;padding:3px 14px;border-radius:20px;margin-bottom:14px}'
    + '#mw-widget .mw-title{font-size:17px;font-weight:700;color:#1a3a6e;margin-bottom:20px;line-height:1.6}'
    + '#mw-widget .mw-icon{font-size:40px;margin-bottom:10px}'
    + '#mw-widget .mw-msg-gray{font-size:17px;font-weight:700;color:#555}'
    + '#mw-widget .mw-msg-blue{font-size:18px;font-weight:700;color:#1565c0}'
    + '#mw-widget .mw-sub{font-size:13px;color:#888;margin-top:8px;line-height:1.8}'
    + '#mw-widget .mw-timeval{font-size:60px;font-weight:800;color:#c0392b;line-height:1}'
    + '#mw-widget .mw-timeunit{font-size:20px;font-weight:700;color:#c0392b;margin-bottom:8px}'
    + '#mw-widget .mw-count{font-size:12px;color:#888}'
    + '#mw-widget .mw-timelabel{font-size:13px;color:#555;margin-bottom:4px}'
    + '#mw-widget .mw-footer{margin-top:18px;padding-top:14px;border-top:1px solid #e8f0fb;font-size:11px;color:#bbb;line-height:1.7}'
    + '#mw-widget .mw-updated{font-size:11px;color:#b0bec5;margin-bottom:4px}'
    + '#mw-widget .mw-loading{font-size:14px;color:#b0bec5;padding:16px 0}';
  var st = document.createElement('style');
  st.textContent = css;
  document.head.appendChild(st);

  /* ── 診療時間判定 ── */
  // 受付時間: 月火水金土 8:30-11:30 / 14:30-17:30（土のみ〜16:30） 木日休診
  function toMin(s){ var p=s.split(':'); return +p[0]*60+ +p[1]; }
  function session(){
    var _d=new Date(), d=new Date(_d.toLocaleString("en-US",{timeZone:"Asia/Tokyo"}));
    var wd=d.getDay(), n=d.getHours()*60+d.getMinutes();
    var amS=toMin('08:30'), amE=toMin('11:30'), pmS=toMin('14:30');
    var pmE=(wd===6)?toMin('16:30'):toMin('17:30');
    var hrs='8:30\u301611:30 / 14:30\u3016'+((wd===6)?'16:30':'17:30');
    if(wd===0||wd===4) return {t:'holiday'};
    if(n<amS)  return {t:'before',next:'8:30',hrs:hrs};
    if(n<amE)  return {t:'open'};
    if(n<pmS)  return {t:'lunch',next:'14:30',hrs:hrs};
    if(n<pmE)  return {t:'open'};
    return {t:'after',hrs:hrs};
  }

  function fmt(iso){
    if(!iso) return '';
    var d=new Date(iso);
    return '\u6700\u7d42\u66f4\u65b0: '+('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);
  }

  function render(data){
    var el=document.getElementById('mw-body');
    if(!el) return;
    var s=session();
    if(data && data.error){
      el.innerHTML='<div class="mw-icon">\u26a0\ufe0f</div><div class="mw-msg-gray">\u60c5\u5831\u3092\u53d6\u5f97\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f</div>';
      return;
    }
    if(s.t==='holiday'){
      el.innerHTML='<div class="mw-icon">\ud83c\udfe5</div><div class="mw-msg-gray">\u672c\u65e5\u306f\u4f11\u8a3a\u65e5\u3067\u3059</div><div class="mw-sub">\u8a3a\u7642\u306f\u6708\u30fb\u706b\u30fb\u6c34\u30fb\u91d1\u30fb\u571f\u66dc\u65e5\u306b\u884c\u3063\u3066\u304a\u308a\u307e\u3059</div>';
      return;
    }
    if(s.t==='before'){
      el.innerHTML='<div class="mw-icon">\ud83d\udd57</div><div class="mw-msg-gray">\u672c\u65e5\u306e\u53d7\u4ed8\u306f\u307e\u3060\u59cb\u307e\u3063\u3066\u3044\u307e\u305b\u3093</div><div class="mw-sub">\u5348\u524d\u306e\u53d7\u4ed8\u958b\u59cb\uff1a<strong>'+s.next+'</strong><br>\u53d7\u4ed8\u6642\u9593\uff1a'+s.hrs+'</div>';
      return;
    }
    if(s.t==='lunch'){
      el.innerHTML='<div class="mw-icon">\ud83c\udf7d\ufe0f</div><div class="mw-msg-gray">\u53ea\u4ecax\u3001\u304a\u6615\u4f11\u307f\u3067\u3059</div><div class="mw-sub">\u5348\u5f8c\u306e\u53d7\u4ed8\u958b\u59cb\uff1a<strong>'+s.next+'</strong><br>\u53d7\u4ed8\u6642\u9593\uff1a'+s.hrs+'</div>';
      return;
    }
    if(s.t==='after'){
      el.innerHTML='<div class="mw-icon">\ud83c\udfe5</div><div class="mw-msg-gray">\u672c\u65e5\u306e\u53d7\u4ed8\u306f\u7d42\u4e86\u3057\u307e\u3057\u305f</div><div class="mw-sub">\u53d7\u4ed8\u6642\u9593\uff1a'+s.hrs+'</div>';
      return;
    }
    if(!data||!data.is_open){
      el.innerHTML='<div class="mw-icon">\ud83c\udfe5</div><div class="mw-msg-gray">\u73fe\u5728\u53d7\u4ed8\u6642\u9593\u5916\u3067\u3059</div>';
      return;
    }
    if(data.count===0){
      el.innerHTML='<div class="mw-icon">\u2705</div><div class="mw-msg-blue">\u73fe\u5728 \u304a\u5f85\u3061\u306e\u65b9\u306f\u3044\u307e\u305b\u3093</div><div class="mw-sub">\u6bd4\u8f03\u7684\u30b9\u30e0\u30fc\u30ba\u306b\u3054\u6848\u5185\u3067\u304d\u307e\u3059</div>';
      return;
    }
    el.innerHTML='<div class="mw-timelabel">\u73fe\u5728\u306e\u76ee\u5b89\u5f85\u3061\u6642\u9593</div>'
      +'<div class="mw-timeval">'+data.estimated_minutes+'</div>'
      +'<div class="mw-timeunit">\u5206 \u7a0b\u5ea6</div>'
      +'<div class="mw-count">(\u4e88\u7d04\u306a\u3057\u6765\u9662\u306e\u65b9 '+data.count+'\u540d \u304c\u8a3a\u5bdf\u3092\u304a\u5f85\u3061\u3067\u3059)</div>';
  }

  function load(){
    fetch(DATA_URL+'?t='+Date.now())
      .then(function(r){return r.json();})
      .then(function(d){
        render(d);
        var u=document.getElementById('mw-updated');
        if(u) u.textContent=fmt(d.updated_at);
      })
      .catch(function(){render({error:true});});
  }

  load();
  setInterval(load,INTERVAL);
})();
