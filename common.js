// ============================================================
// みがわ町整形外科クリニック 各表示ページ 共通ロジック
// index.html / now.html で共有。
// JST(UTC+9固定)の時刻取得・更新時刻表示・受付時間表示・
// データ取得ポーリングをまとめている。
// getSession() / render() はページ固有のため各ページ側に残す。
// ============================================================
window.ClinicCommon = (function(){
  var DATA_URL = "https://raw.githubusercontent.com/Oshinji/clinics-wait-time/main/data/status.json";
  var INTERVAL  = 5 * 60 * 1000;

  function toMin(s){ var p=s.split(':'); return +p[0]*60+ +p[1]; }

  // JST (UTC+9 固定, 夏時間なし) の時刻部品を安全に取り出す。
  // toLocaleString + new Date() は Safari/iOS が narrow no-break space (U+202F)
  // を挿入する既知不具合があり Invalid Date になる → UTC+9h ms 加算で回避。
  function jstParts(epochMs){
    var t = (typeof epochMs === 'number') ? epochMs : new Date().getTime();
    var j = new Date(t + 9 * 60 * 60 * 1000);
    return { w: j.getUTCDay(), h: j.getUTCHours(), m: j.getUTCMinutes() };
  }

  function fmtUpdated(iso){
    if(!iso) return '更新 --:--';
    var d = new Date(iso);                // ISO8601 (+09:00 付き) は全ブラウザでパース可
    if(isNaN(d.getTime())) return '更新 --:--';
    var p = jstParts(d.getTime());
    return '更新 '+('0'+p.h).slice(-2)+':'+('0'+p.m).slice(-2);
  }

  function hoursHtml(s){
    var amC = (s.t==='open' && s.half==='am') ? ' is-current' : '';
    var pmC = (s.t==='open' && s.half==='pm') ? ' is-current' : '';
    return '<div class="hours">'
      +'<div class="hours-title">受付時間</div>'
      +'<div class="hours-rows">'
      +'<div class="hours-row'+amC+'"><span class="hours-period">午前</span><span class="hours-time">8:30–11:30</span></div>'
      +'<div class="hours-row'+pmC+'"><span class="hours-period">午後</span><span class="hours-time">14:30–'+s.ctx.pmEndStr+'</span></div>'
      +'</div></div>';
  }

  // 直前と同一内容なら innerHTML を書き換えない。
  // 不要な再描画を防ぎ、aria-live の読み上げも実際に変化があったときだけにする。
  function setContent(el, html){
    if(el.__lastHtml === html) return;
    el.__lastHtml = html;
    el.innerHTML = html;
  }

  function startPolling(render){
    function load(){
      fetch(DATA_URL+'?t='+Date.now())
        .then(function(r){ return r.json(); })
        .then(function(d){
          render(d);
          var u = document.getElementById('updated');
          if(u) u.textContent = fmtUpdated(d.updated_at);
        })
        .catch(function(){ render({error:true}); });
    }
    load();
    setInterval(load, INTERVAL);
  }

  return {
    toMin: toMin,
    jstParts: jstParts,
    fmtUpdated: fmtUpdated,
    hoursHtml: hoursHtml,
    setContent: setContent,
    startPolling: startPolling
  };
})();
