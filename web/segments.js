"use strict";
window.segmentUI=(()=>{
  let map, checkpointsLayer, routesLayer, highlightLayer;
  let saved=[], selected=null, editing=false, dirty=false, saving=false;
  let points=[], undo=[], version=0, page=0, poll=null, detail=null, loadedKey=null;
  const pageSize=50;
  const message=text=>$("#segment-message").textContent=text;
  const active=()=>!$("#segments").hidden;
  const elapsed=value=>{
    if(value==null) return "Untimed";
    const seconds=Number(value), h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), s=(seconds%60).toFixed(1).padStart(4,"0");
    return (h ? h+":"+String(m).padStart(2,"0") : String(m))+":"+s;
  };
  async function request(path,method="GET",body) {
    const response=await fetch("/api/segments"+path,{method,
      headers:body ? {"Content-Type":"application/json"} : {},
      body:body ? JSON.stringify(body) : undefined});
    let data;
    try {data=await response.json();} catch {throw new Error("Could not read segment data. Restart the app server and refresh.");}
    if(!response.ok) throw new Error(data.error || "Segment request failed (HTTP "+response.status+").");
    return data;
  }
  function setSports() {
    const old=$("#segment-sport").value;
    const sports=[...new Set(activities.map(a=>a.sport).filter(Boolean))].sort();
    $("#segment-sport").replaceChildren(...sports.map(s=>new Option(sportName(s),s)));
    if(sports.includes(old)) $("#segment-sport").value=old;
  }
  function initMap() {
    if(map) {map.invalidateSize(); return;}
    map=getMap("segment-map");
    checkpointsLayer=L.featureGroup().addTo(map);
    routesLayer=L.featureGroup().addTo(map);
    highlightLayer=L.featureGroup().addTo(map);
    map.on("click",event=>{
      if(!editing || saving) return;
      if(points.length>=100) {message("A segment can contain at most 100 checkpoints.");return;}
      snapshot(); points.push([event.latlng.lat,event.latlng.lng]); drawCheckpoints(); dirty=true;
    });
  }
  function snapshot() {undo.push(points.map(p=>[...p])); if(undo.length>100) undo.shift();}
  function drawCheckpoints(fit=false) {
    checkpointsLayer.clearLayers();
    const radius=Number($("#segment-radius").value) || 30;
    if(points.length>1) L.polyline(points,{color:"#d66c19",weight:3,dashArray:"7 5",interactive:false}).addTo(checkpointsLayer);
    points.forEach((point,index)=>{
      L.circle(point,{radius,color:"#d66c19",weight:1,fillOpacity:0.08,interactive:false}).addTo(checkpointsLayer);
      const icon=L.divIcon({className:"checkpoint-icon",html:String(index+1),iconSize:[28,28],iconAnchor:[14,14]});
      const marker=L.marker(point,{icon,draggable:editing && !saving}).addTo(checkpointsLayer);
      marker.on("dragstart",snapshot);
      marker.on("dragend",()=>{const p=marker.getLatLng();points[index]=[p.lat,p.lng];dirty=true;drawCheckpoints();});
    });
    $("#segment-checkpoints").replaceChildren();
    points.forEach((p,index)=>{
      const row=document.createElement("li"), button=document.createElement("button");
      const text=document.createElement("span");text.textContent=p.map(v=>v.toFixed(5)).join(", ");
      button.type="button";button.textContent="Remove";button.disabled=saving;
      button.setAttribute("aria-label","Remove checkpoint "+(index+1));
      button.onclick=()=>{snapshot();points.splice(index,1);dirty=true;drawCheckpoints();};
      row.append(text,button);$("#segment-checkpoints").append(row);
    });
    $("#segment-undo").disabled=!undo.length || saving;
    if(fit && points.length) map.fitBounds(checkpointsLayer.getBounds(),{padding:[35,35],maxZoom:16});
  }
  function renderList() {
    $("#segment-list").replaceChildren();
    if(!saved.length) $("#segment-list").textContent="No segments yet. Create one on the map.";
    for(const segment of saved) {
      const button=document.createElement("button");button.className="activity"+(selected===segment.id?" active":"");
      button.innerHTML="<strong>"+escape(segment.name)+"</strong><span class='small'>"+escape(sportName(segment.sport))+" · "+escape(segment.status==="completed" ? segment.traversal_count+" traversals" : segment.status)+"</span>";
      button.disabled=saving;
      button.onclick=()=>{if(discard()) selectSegment(segment.id);};
      $("#segment-list").append(button);
    }
  }
  function discard() {
    if(saving) return false;
    if(editing && dirty && !confirm("Discard your unsaved segment changes?")) return false;
    editing=false;dirty=false;$("#segment-editor").hidden=true;return true;
  }
  function schedulePoll() {
    clearTimeout(poll);
    if(active() && !editing && saved.some(s=>["pending","processing"].includes(s.status))) {
      poll=setTimeout(async()=>{
        await refreshList();
        const current=saved.find(s=>s.id===selected);
        if(current && !editing && (!detail || current.revision!==detail.revision ||
          current.status!==detail.status || ["pending","processing"].includes(current.status))) {
          await selectSegment(selected,false);
        }
      },2000);
    }
  }
  async function refreshList() {
    try {saved=await request("");renderList();schedulePoll();}
    catch(error) {message(error.message);}
  }
  function editSegment(segment=null) {
    if(!discard()) return;
    version++;clearTimeout(poll);editing=true;dirty=false;undo=[];
    detail=segment;selected=segment ? segment.id : null;
    points=segment ? segment.checkpoints.map(p=>[...p]) : [];
    setSports();$("#segment-name").value=segment ? segment.name : "";
    if(segment) $("#segment-sport").value=segment.sport;
    $("#segment-radius").value=segment ? segment.radius_m : 30;
    $("#segment-editor-title").textContent=segment ? "Edit segment" : "New segment";
    $("#segment-editor").hidden=false;$("#segment-detail").hidden=true;
    $("#segment-new").disabled=false;routesLayer.clearLayers();highlightLayer.clearLayers();loadedKey=null;
    map.doubleClickZoom.disable();drawCheckpoints(Boolean(segment));renderList();
    message("Place checkpoints in forward order. Sport must match the activity's stored sport.");
    $("#segment-name").focus();
  }
  async function selectSegment(id,fit=true) {
    const token=++version;
    try {
      const segment=await request("/"+id);
      if(token!==version || editing || !active()) return;
      selected=id;detail=segment;points=segment.checkpoints.map(p=>[...p]);
      $("#segment-radius").value=segment.radius_m;
      $("#segment-editor").hidden=true;$("#segment-detail").hidden=false;
      $("#segment-title").textContent=segment.name;
      $("#segment-info").textContent=sportName(segment.sport)+" · "+segment.radius_m+" m tolerance · "+points.length+" checkpoints";
      map.doubleClickZoom.enable();drawCheckpoints(fit);renderList();
      if(segment.status!=="completed") {
        routesLayer.clearLayers();highlightLayer.clearLayers();loadedKey=null;
        $("#segment-stats").textContent="";
        $("#segment-traversals").textContent="Results will appear when matching finishes.";
        $("#segment-prev").disabled=true;$("#segment-next").disabled=true;$("#segment-page").textContent="";
        message(segment.status==="failed" ? segment.error : segment.status==="processing" ?
          "Matching activities: "+segment.progress_done+" / "+segment.progress_total : "Queued for background matching…");
        return;
      }
      $("#segment-stats").innerHTML='<div class="segment-stat"><b>'+segment.traversal_count+'</b> traversals · <b>'+segment.activity_count+'</b> activities</div>'+
        (segment.fastest ? '<p>Fastest: <strong>'+elapsed(segment.fastest.duration_s)+'</strong></p>' :
        '<p class="small">'+(points.length===1 ? "Single-point segments count visits; no fastest time." : "No timed traversals available.")+'</p>');
      message(segment.traversal_count ? "Matching activity routes are teal. The selected traversal is orange." : "No matching traversals found. Check the sport, checkpoints, or tolerance.");
      const key=id+":"+segment.revision;
      if(loadedKey!==key) {
        page=0;routesLayer.clearLayers();highlightLayer.clearLayers();
        const routes=await request("/"+id+"/routes");
        if(token!==version || editing) return;
        for(const route of routes) {
          const line=L.polyline(route.segments,{color:"#087e8b",weight:2,opacity:0.3}).addTo(routesLayer);
          const label=document.createElement("span");label.textContent="Activity "+route.activity_id;line.bindTooltip(label);
        }
        loadedKey=key;
        if(segment.fastest) await highlight(segment.fastest.id,false);
      }
      await renderTraversals(token);
    } catch(error) {if(token===version) message(error.message);}
  }
  async function renderTraversals(token=version) {
    if(!selected || !detail || detail.status!=="completed") return;
    try {
      const result=await request("/"+selected+"/traversals?offset="+page*pageSize+"&limit="+pageSize);
      if(token!==version || editing) return;
      $("#segment-traversals").replaceChildren();
      for(const item of result.items) {
        const row=document.createElement("div");row.className="traversal-row";
        const button=document.createElement("button");button.className="traversal-select";
        const fastest=detail.fastest && item.id===detail.fastest.id;
        button.textContent=(fastest?"★ Fastest · ":"")+date(item.activity_start_time)+" · Pass "+item.ordinal+" · "+(points.length===1 ? "Visit" : elapsed(item.duration_s));
        button.onclick=()=>highlight(item.id,true);
        const open=document.createElement("button");open.className="small";open.textContent="Open activity "+item.activity_id;
        open.onclick=()=>{if(switchView("tracks")) showTrack(item.activity_id);};
        row.append(button,open);$("#segment-traversals").append(row);
      }
      $("#segment-page").textContent=result.total ? (result.offset+1)+"–"+(result.offset+result.items.length)+" of "+result.total : "No traversals";
      $("#segment-prev").disabled=page===0;$("#segment-next").disabled=(page+1)*pageSize>=result.total;
    } catch(error) {if(token===version) message(error.message);}
  }
  let highlightVersion=0;
  async function highlight(id,fit) {
    const token=version,h=++highlightVersion,sid=selected;
    try {
      const geometry=await request("/"+sid+"/traversals/"+id+"/geometry");
      if(token!==version || h!==highlightVersion || editing) return;
      highlightLayer.clearLayers();
      if(geometry.points.length===1) L.circleMarker(geometry.points[0],{radius:8,color:"#d66c19",fillOpacity:0.8}).addTo(highlightLayer);
      else L.polyline(geometry.points,{color:"#d66c19",weight:5,opacity:1}).addTo(highlightLayer);
      if(fit) map.fitBounds(highlightLayer.getBounds(),{padding:[35,35],maxZoom:17});
    } catch(error) {if(token===version) message(error.message);}
  }
  $("#segment-new").onclick=()=>editSegment();
  $("#segment-edit").onclick=()=>{if(detail) editSegment(detail);};
  $("#segment-cancel").onclick=()=>{if(!discard()) return;map.doubleClickZoom.enable();if(selected) selectSegment(selected);else {checkpointsLayer.clearLayers();message("Select a segment or create a new one.");}schedulePoll();};
  $("#segment-undo").onclick=()=>{if(undo.length){points=undo.pop();dirty=true;drawCheckpoints();}};
  ["#segment-name","#segment-sport","#segment-radius"].forEach(selector=>$(selector).oninput=()=>{dirty=true;if(selector==="#segment-radius") drawCheckpoints();});
  $("#segment-editor").onsubmit=async event=>{
    event.preventDefault();if(saving) return;
    if(!points.length) {message("Place at least one checkpoint on the map.");return;}
    saving=true;$("#segment-editor").querySelectorAll("input,select,button").forEach(x=>x.disabled=true);
    try {
      const payload={name:$("#segment-name").value,sport:$("#segment-sport").value,radius_m:Number($("#segment-radius").value),checkpoints:points};
      if(detail) payload.revision=detail.revision;
      const segment=await request(detail?"/"+detail.id:"",detail?"PUT":"POST",payload);
      editing=false;dirty=false;selected=segment.id;loadedKey=null;
      await refreshList();await selectSegment(selected);
    } catch(error) {message(error.message);}
    finally {saving=false;$("#segment-editor").querySelectorAll("input,select,button").forEach(x=>x.disabled=false);drawCheckpoints();renderList();}
  };
  $("#segment-delete").onclick=async()=>{
    if(!detail || saving || !confirm('Delete segment "'+detail.name+'" and its cached results?')) return;
    saving=true;
    try {
      await request("/"+detail.id,"DELETE",{revision:detail.revision});
      version++;selected=null;detail=null;loadedKey=null;$("#segment-detail").hidden=true;
      checkpointsLayer.clearLayers();routesLayer.clearLayers();highlightLayer.clearLayers();
      message("Segment deleted.");await refreshList();
    } catch(error) {message(error.message);}
    finally {saving=false;renderList();}
  };
  $("#segment-prev").onclick=()=>{page=Math.max(0,page-1);renderTraversals();};
  $("#segment-next").onclick=()=>{page++;renderTraversals();};
  return {
    setSports,
    async show(){try{initMap();setSports();await refreshList();if(!editing){if(selected) await selectSegment(selected,false);else message("Select a segment or create one by placing checkpoints on the map.");}}catch(error){message(error.message);}},
    allowNavigate(view){if(view==="segments")return true;if(!discard())return false;version++;clearTimeout(poll);return true;}
  };
})();
