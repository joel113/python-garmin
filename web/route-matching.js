"use strict";
window.routeMatchUI=(()=>{
  const controllers={};
  let settingsState=null,settingsDirty=false,settingsBusy=false;
  const settingsForm=$("#route-matching-settings"),settingsStatus=$("#route-matching-settings-status");
  async function api(path,method="GET",body) {
    const response=await fetch("/api/"+path,{method,headers:body?{"Content-Type":"application/json"}:{},body:body?JSON.stringify(body):undefined});
    const data=await response.json();
    if(!response.ok){const error=Error(data.error||"Could not load route matching.");error.status=response.status;throw error;}
    return data;
  }
  function stateText(state) {
    if(state.status==="failed")return (state.error||"Matching failed. Use Retry matching.")+" Previous results may be out of date.";
    if(state.status==="pending"||state.status==="processing")return "Updating matches ("+state.progress_done+" / "+state.progress_total+" routes). Previous results may be out of date.";
    return "Matching complete.";
  }
  function updateSettings(state) {
    if(!settingsDirty&&!settingsBusy) {
      settingsState=state;
      for(const key of ["tolerance_m","partial_percent","full_percent","partial_km","full_km"])settingsForm.elements[key].value=state[key]??"";
    }
    if(!settingsBusy)settingsStatus.textContent=stateText(state);
  }
  function controller(kind) {
    if(controllers[kind])return controllers[kind];
    const isTrack=kind==="track",root=$(isTrack?"#track-route-matches":"#route-track-matches"),toggle=$(isTrack?"#track-show-route-match":"#route-show-track-match");
    const q=selector=>root.querySelector(selector),message=text=>q(".match-status").textContent=text;
    const c={id:null,map:null,layer:null,page:0,data:null,chosen:null,timer:null,version:0,drawVersion:0,drawKey:null,visible:false};
    function clearLayer(){c.drawVersion++;c.drawKey=null;if(c.layer){c.layer.remove();c.layer=null;}q(".match-zoom").disabled=true;}
    function clear(){c.version++;clearTimeout(c.timer);clearLayer();c.id=null;c.data=null;c.chosen=null;q(".match-prev").disabled=true;q(".match-next").disabled=true;q(".match-retry").hidden=true;q(".match-list").replaceChildren();q(".match-page").textContent="";}
    function selectedMatch(){return c.data?.matches.find(m=>m.route_id+":"+m.activity_id===c.chosen);}
    async function draw() {
      const match=selectedMatch();
      const key=match?c.id+":"+c.chosen+":"+match.generation+":"+c.data.processing.revision:null;
      if(toggle.checked&&c.visible&&c.layer&&key===c.drawKey)return;
      clearLayer();
      if(!toggle.checked||!match||!c.visible)return;
      const token=c.drawVersion,id=c.id;
      try {
        const data=await api("route-matching/geometry?route_id="+encodeURIComponent(match.route_id)+"&activity_id="+encodeURIComponent(match.activity_id));
        if(token!==c.drawVersion||id!==c.id||!c.visible||!toggle.checked)return;
        if(!c.map.getPane("route-match"))c.map.createPane("route-match").style.zIndex=isTrack?"390":"425";
        c.drawKey=key;c.layer=L.featureGroup().addTo(c.map);
        const base=isTrack?data.route:data.track,highlight=isTrack?data.matched_route:data.matched_track;
        L.polyline(base,{pane:"route-match",color:"#7548ac",weight:4,opacity:.8,dashArray:"8 7",interactive:false}).addTo(c.layer);
        L.polyline(highlight,{pane:"route-match",color:"#fff",weight:12,opacity:.95,interactive:false}).addTo(c.layer);
        L.polyline(highlight,{pane:"route-match",color:match.classification==="full"?"#218354":"#c77712",weight:10,opacity:1,interactive:false}).addTo(c.layer);
        q(".match-zoom").disabled=!c.layer.getBounds().isValid();
      }catch(error){if(token===c.drawVersion)message(error.message);}
    }
    function render() {
      const data=c.data,list=q(".match-list");list.replaceChildren();
      if(!data.matches.some(m=>m.route_id+":"+m.activity_id===c.chosen))c.chosen=data.matches.length?data.matches[0].route_id+":"+data.matches[0].activity_id:null;
      message(data.counts.full+" full · "+data.counts.partial+" partial. "+stateText(data.processing));
      if(!data.matches.length){const p=document.createElement("p");p.className="small";p.textContent="No qualifying matches. Tracks without usable GPS and routes without usable length cannot match.";list.append(p);}
      for(const match of data.matches) {
        const card=document.createElement("div");card.className="route-match-card"+(c.chosen===match.route_id+":"+match.activity_id?" selected":"");
        const select=document.createElement("button");select.type="button";select.className="match-select";
        const title=document.createElement("strong");title.textContent=isTrack?match.name:date(match.start_time)+" · "+sportName(match.sport)+" · "+match.activity_id;
        const badge=document.createElement("span");badge.className="match-badge "+match.classification;badge.textContent=match.classification==="full"?"Full":"Partial";
        const summary=document.createElement("span");summary.className="small";summary.textContent=(100*match.route_coverage).toFixed(1)+"% of route · "+(100*match.track_coverage).toFixed(1)+"% of track · "+km(match.shared_m)+" shared · "+match.direction;
        select.append(title,badge,summary);select.onclick=()=>{c.chosen=match.route_id+":"+match.activity_id;render();draw();};
        const open=document.createElement("button");open.type="button";open.textContent=isTrack?"Open route":"Open track";
        open.onclick=()=>{
          if(isTrack){if(switchView("routes"))window.routesUI.openRoute(match.route_id);}
          else if(switchView("tracks"))showTrack(match.activity_id);
        };
        card.append(select,open);list.append(card);
      }
      q(".match-prev").disabled=c.page===0;q(".match-next").disabled=(c.page+1)*25>=data.counts.total;
      q(".match-page").textContent=data.counts.total?"Page "+(c.page+1)+" / "+Math.ceil(data.counts.total/25):"";
      q(".match-retry").hidden=data.processing.status!=="failed";
    }
    async function load() {
      clearTimeout(c.timer);const token=++c.version,id=c.id;if(!id||!c.visible)return;
      try {
        const path=isTrack?"track/"+encodeURIComponent(id)+"/route-matches":"routes/"+encodeURIComponent(id)+"/track-matches";
        const data=await api(path+"?page="+c.page);
        if(token!==c.version||!c.visible)return;
        if(c.page>0&&c.page*25>=data.counts.total){c.page=0;return load();}
        c.data=data;updateSettings(data.processing);render();await draw();
        c.timer=setTimeout(load,data.processing.status==="pending"||data.processing.status==="processing"?3000:15000);
      }catch(error){if(token===c.version&&c.visible){message(error.message);c.timer=setTimeout(load,10000);}}
    }
    toggle.onchange=draw;
    q(".match-prev").onclick=()=>{if(c.page>0){c.page--;load();}};
    q(".match-next").onclick=()=>{c.page++;load();};
    q(".match-zoom").onclick=()=>{if(c.layer?.getBounds().isValid())c.map.fitBounds(c.layer.getBounds(),{padding:[30,30],maxZoom:16});};
    q(".match-zoom").disabled=true;
    q(".match-retry").onclick=async()=>{
      try{const state=await api("route-matching");await api("route-matching/retry","POST",{revision:state.revision});load();}catch(error){message(error.message);}
    };
    q(".match-retry").hidden=true;
    Object.assign(c,{clear,load,show(id,map){if(c.id!==id){clear();c.page=0;message("Loading matches…");}c.id=id;c.map=map;c.visible=true;load();},hide(){c.visible=false;c.version++;clearTimeout(c.timer);clearLayer();}});
    controllers[kind]=c;return c;
  }
  settingsForm.oninput=()=>{settingsDirty=true;};
  settingsForm.onsubmit=async event=>{
    event.preventDefault();if(settingsBusy||!settingsState)return;
    const body={revision:settingsState.revision};for(const key of ["tolerance_m","partial_percent","full_percent"])body[key]=Number(settingsForm.elements[key].value);
    for(const key of ["partial_km","full_km"]) {
      const value=settingsForm.elements[key].value.trim();body[key]=value===""?null:Number(value);
    }
    settingsBusy=true;settingsForm.querySelectorAll("button").forEach(b=>b.disabled=true);
    try {
      const state=await api("route-matching","PUT",body);settingsDirty=false;settingsBusy=false;updateSettings(state);
      Object.values(controllers).filter(c=>c.visible).forEach(c=>c.load());
    }catch(error){settingsStatus.textContent=error.message;}
    finally{settingsBusy=false;settingsForm.querySelectorAll("button").forEach(b=>b.disabled=false);}
  };
  $("#route-matching-reload").onclick=async()=>{
    if(settingsDirty&&!confirm("Discard unsaved matching settings?"))return;
    try{settingsDirty=false;updateSettings(await api("route-matching"));}catch(error){settingsStatus.textContent=error.message;}
  };
  $(".route-matching-settings").ontoggle=async event=>{
    if(event.target.open)try{updateSettings(await api("route-matching"));}catch(error){settingsStatus.textContent=error.message;}
  };
  return {show(kind,id,map){controller(kind).show(id,map);},clear(kind){controllers[kind]?.clear();},
    hideExcept(view){for(const [kind,c] of Object.entries(controllers))if(view!==(kind==="track"?"tracks":"routes"))c.hide();}};
})();
