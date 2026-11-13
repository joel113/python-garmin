"use strict";
window.coverageUI=(()=>{
  let map,layer,boundaries,state=null,timer=null,visible=false,version=0,activeKey=null,selectedSection=null,page=0;
  let regions=[],chosen=[],catalogLoaded=false,sectionVersion=0,busy=false,preview=null,previewLayer=null;
  const trackMode=()=>$("#coverage-area-mode").value==="tracks";
  const text=(id,value)=>$(id).textContent=value;
  const sport=()=>$("#coverage-sport").value;
  const status=message=>text("#coverage-status",message);
  const sizes=new Map();
  async function api(path="",method="GET",body) {
    const response=await fetch("/api/network-coverage"+path,{method,headers:body?{"Content-Type":"application/json"}:{},body:body?JSON.stringify(body):undefined});
    const data=await response.json();if(!response.ok){const error=Error(data.error||"Coverage request failed.");error.status=response.status;throw error;}return data;
  }
  const bytes=value=>value?((value/1024/1024/1024)>=1?(value/1024/1024/1024).toFixed(2)+" GiB":(value/1024/1024).toFixed(1)+" MiB"):"size unavailable";
  function setBusy(value){busy=value;$("#coverage-build").disabled=value||(trackMode()?!preview:!chosen.length)||Boolean(state?.staging_generation);$("#coverage-preview").disabled=value||Boolean(state?.staging_generation);$("#coverage-buffer").disabled=value;$("#coverage-area-mode").disabled=value;$("#coverage-retry").disabled=value;$("#coverage-cancel").disabled=value;}
  function renderRegions() {
    const query=$("#coverage-region-search").value.trim().toLowerCase();
    const matches=regions.filter(r=>(r.name+" "+r.id).toLowerCase().includes(query)&&!chosen.includes(r.id)).slice(0,150);
    $("#coverage-regions").replaceChildren(...matches.map(r=>new Option(r.id+" · "+r.name,r.id)));
    $("#coverage-selected-regions").replaceChildren();
    for(const id of chosen){
      const row=document.createElement("li"),label=document.createElement("span"),remove=document.createElement("button");
      const size=sizes.get(id);label.textContent=id+(size?" · download "+bytes(size.bytes)+" · reserve at least "+bytes(size.reserve_bytes):" · checking size…");
      remove.textContent="Remove";remove.onclick=()=>{chosen=chosen.filter(r=>r!==id);renderRegions();};row.append(label,remove);$("#coverage-selected-regions").append(row);
    }
    setBusy(busy);
  }
  async function loadCatalog() {
    if(catalogLoaded)return;
    text("#coverage-setup-status","Loading regional download catalog…");
    try{regions=await api("/regions");catalogLoaded=true;const active=state?.generations.find(g=>g.id===state.active_generation);if(!chosen.length&&active)chosen=active.regions.map(r=>r.id);for(const id of chosen){api("/region-size?id="+encodeURIComponent(id)).then(size=>{sizes.set(id,size);renderRegions();}).catch(()=>{sizes.set(id,{bytes:0,reserve_bytes:0});renderRegions();});}renderRegions();text("#coverage-setup-status","Choose all regions to include in the next graph. Existing coverage stays visible while it builds.");}
    catch(error){text("#coverage-setup-status",error.message);}
  }
  function style(properties) {
    if(!properties.visited)return {color:"#929da3",weight:1,opacity:.5};
    const scale=state?.scales?.[sport()]||{},reference=Math.max(1,scale.max_count||1),count=properties.count||1;
    const weight=2+8*Math.min(1,Math.log(count)/Math.log(Math.max(2,reference)));
    const color=sport()==="all"?"#087e8b":properties.speed==null?"#786898":"hsl("+(240-240*Math.min(1,properties.speed/Math.max(1,scale.speed_max||50)))+",90%,45%)";
    return {color,weight,opacity:.95};
  }
  function renderLayer() {
    if(!visible||!map)return;
    const key=[state?.active_generation,state?.data_revision,sport(),$("#coverage-unvisited").checked].join(":");
    if(key===activeKey)return;activeKey=key;
    if(layer){layer.remove();layer=null;}
    if(!state?.active_generation)return;
    if(!L.vectorGrid){status("Vector map renderer failed to load. Refresh the page when online.");return;}
    const url="/api/network-coverage/tiles/{z}/{x}/{y}.pbf?sport="+sport()+"&unvisited="+($("#coverage-unvisited").checked?1:0)+"&revision="+state.data_revision+"&generation="+state.active_generation;
    layer=L.vectorGrid.protobuf(url,{vectorTileLayerStyles:{coverage:style},interactive:true,attribution:"Street/path coverage © OpenStreetMap contributors",maxNativeZoom:20,getFeatureId:feature=>feature.properties.section_id}).addTo(map);
    layer.on("tileerror",()=>text("#heat-status","Could not load street/path tiles. Refresh status or reload the page to retry."));
    layer.on("mouseover",event=>{
      const p=event.layer.properties,node=document.createElement("span");
      node.textContent=p.name+" · "+(p.visited?(p.count+" activities · "+(sport()==="all"?"combined frequency":p.speed==null?"speed unavailable":Number(p.speed).toFixed(1)+" km/h typical · "+p.speed_samples+" speed samples")):"No recorded visit for this sport");
      map._coverageTooltip?.remove();map._coverageTooltip=L.tooltip().setLatLng(event.latlng).setContent(node).addTo(map);
    });
    layer.on("mouseout",()=>{map._coverageTooltip?.remove();map._coverageTooltip=null;});
    layer.on("click",event=>{selectedSection=event.layer.properties.section_id;page=0;loadSection();});
  }
  function renderBoundary(fit=false) {
    if(boundaries)boundaries.remove();
    const generation=state.generations.find(g=>g.id===state.active_generation);
    boundaries=L.featureGroup().addTo(map);
    if(generation)for(const region of (generation.footprint?[{geometry:generation.footprint}]:generation.regions))L.geoJSON(region.geometry,{style:{color:"#64748b",weight:1,fillOpacity:0,interactive:false}}).addTo(boundaries);
    if(fit&&boundaries.getBounds().isValid())map.fitBounds(boundaries.getBounds(),{padding:[24,24],maxZoom:13});
  }
  function pointInsideRing(point,ring) {
    let inside=false;for(let i=0,j=ring.length-1;i<ring.length;j=i++){
      const a=ring[i],b=ring[j];if(((a[1]>point[1])!==(b[1]>point[1]))&&(point[0]<(b[0]-a[0])*(point[1]-a[1])/(b[1]-a[1])+a[0]))inside=!inside;
    }return inside;
  }
  function inCoverage(point) {
    const generation=state?.generations.find(g=>g.id===state.active_generation);
    return (generation?.footprint?[{geometry:generation.footprint}]:generation?.regions)?.some(r=>{
      const polygons=r.geometry.type==="Polygon"?[r.geometry.coordinates]:r.geometry.coordinates;
      return polygons.some(p=>pointInsideRing(point,p[0])&&!p.slice(1).some(hole=>pointInsideRing(point,hole)));
    });
  }
  function locationNote() {
    if(!visible||!state?.active_generation)return;
    const center=map.getCenter();
    text("#heat-status",!inCoverage([center.lng,center.lat])?"Outside downloaded coverage at map center · pan into a selected region.":map.getZoom()<15?"Overview is simplified. Zoom in to level 15+ for complete street/path detail; gray paths appear from level 12.":"Colored = recorded coverage · gray = no recorded visit · purple = visited without speed data.");
  }
  async function refresh() {
    clearTimeout(timer);const token=++version;if(!visible)return;
    try{
      const next=await api();if(token!==version||!visible)return;
      const changed=state?.active_generation!==next.active_generation;state=next;
      const staged=state.generations.find(g=>g.id===state.staging_generation),active=state.generations.find(g=>g.id===state.active_generation);
      const jobs=staged?.jobs||active?.jobs||{},progress=state.phase==="downloading"?bytes(state.progress_done)+" / "+bytes(state.progress_total):state.phase==="building graph"?"building local routing graph":state.phase==="importing network"?state.progress_done+" sections imported":state.progress_done+" / "+state.progress_total,jobSummary=Object.entries(jobs).map(([key,value])=>value+" "+key).join(" · ");
      status(state.status==="unconfigured"?"Choose regions in Street/path map setup to enable this layer.":(state.phase||state.status)+" · "+(jobSummary||progress)+(state.error?" · "+state.error:""));
      text("#coverage-setup-status",staged?"Staged regions: "+staged.regions.map(r=>r.id).join(", ")+" · "+state.phase+" · "+progress:active?"Published regions: "+active.regions.map(r=>r.id).join(", "):"No regional maps installed.");
      $("#coverage-retry").hidden=state.status!=="failed";$("#coverage-cancel").hidden=!state.staging_generation;
      const scale=state.scales[sport()]||{};
      text("#coverage-legend",sport()==="all"?"Neutral color · width: 1–"+(scale.max_count||1)+" distinct activities":"Blue 0 → red "+(scale.speed_max?scale.speed_max.toFixed(1):"—")+" km/h · median elapsed speed · width: 1–"+(scale.max_count||1)+" distinct activities");
      if(active?.mode==="tracks"){text("#coverage-setup-status",$("#coverage-setup-status").textContent+" · "+active.buffer_km+" km track buffer · "+bytes(active.cropped_bytes)+" cropped inputs"+(active.retained_bytes!=null?" · "+bytes(active.retained_bytes)+" retained routing files":""));}
      status($("#coverage-status").textContent+" · "+state.excluded_activities+" other-sport activities excluded · "+(active?.unmatched_points||0)+" unmatched samples · "+(active?.unmatched_activities||0)+" activities without recorded network coverage"+(active?.outside_coverage_activities?" · "+active.outside_coverage_activities+" activities extend outside this map; preview and rebuild to expand.":""));
      renderLayer();if(changed||!boundaries)renderBoundary(changed);if(!state.active_generation)text("#heat-status","Coverage is not published yet. Existing heatmap modes remain available.");locationNote();setBusy(busy);
      timer=setTimeout(refresh,state.status==="processing"?3000:15000);
    }catch(error){if(token===version){status(error.message);timer=setTimeout(refresh,10000);}}
  }
  async function loadSection() {
    const token=++sectionVersion;
    try{
      const data=await api("/sections/"+selectedSection+"?sport="+sport()+"&page="+page);if(token!==sectionVersion||!visible)return;
      $("#coverage-section").hidden=false;$("#coverage-section").open=true;
      text("#coverage-section-title",data.section.name);
      text("#coverage-section-summary",(data.stats?.activity_count||0)+" recorded activities · "+(data.stats?.speed_kmh==null?"speed unavailable":data.stats.speed_kmh.toFixed(1)+" km/h median elapsed speed"));
      $("#coverage-activities").replaceChildren(...data.activities.map(a=>{const button=document.createElement("button");button.textContent=date(a.start_time)+" · "+sportName(a.sport)+" · "+a.id;button.onclick=()=>{if(switchView("tracks"))showTrack(a.id);};return button;}));
      $("#coverage-prev").disabled=page===0;$("#coverage-next").disabled=(page+1)*25>=(data.stats?.activity_count||0);text("#coverage-page","Page "+(page+1));
    }catch(error){status(error.message);}
  }
  $("#coverage-setup").ontoggle=event=>{if(event.target.open&&!trackMode())loadCatalog();};
  $("#coverage-region-search").oninput=renderRegions;
  $("#coverage-add-region").onclick=async()=>{
    const id=$("#coverage-regions").value;if(!id||chosen.includes(id))return;chosen.push(id);renderRegions();
    try{sizes.set(id,await api("/region-size?id="+encodeURIComponent(id)));}catch{sizes.set(id,{bytes:0,reserve_bytes:0});}renderRegions();
  };
  function clearPreview(){preview=null;if(previewLayer){previewLayer.remove();previewLayer=null;}$("#coverage-fit-preview").disabled=true;setBusy(busy);}
  $("#coverage-buffer").oninput=()=>{clearPreview();text("#coverage-preview-summary","Calculate a new preview after changing the buffer.");};
  $("#coverage-area-mode").onchange=()=>{$("#coverage-track-settings").hidden=!trackMode();$("#coverage-manual-regions").hidden=trackMode();$("#coverage-build").textContent=trackMode()?"Build cropped coverage":"Build selected regions";clearPreview();if(!trackMode())loadCatalog();};
  $("#coverage-preview").onclick=async()=>{
    if(busy)return;setBusy(true);text("#coverage-preview-summary","Calculating local track corridors and checking smaller regional downloads…");
    try{
      const result=await api("/preview","POST",{buffer_km:Number($("#coverage-buffer").value)});
      clearPreview();preview=result;
      const description=result.activity_count+" activities · "+result.area_km2.toLocaleString()+" km² · "+result.regions.length+" smaller extracts · "+bytes(result.download_bytes)+" total download"+(result.unknown_sizes?" (some sizes unavailable)":"")+" · temporary source workspace ≥ "+bytes(result.temporary_reserve_bytes)+", plus cropped graph build space. Sources are downloaded, cropped, and removed one at a time; final graph size is measured after building.";
      text("#coverage-preview-summary",description);
      if(result.geometry){previewLayer=L.geoJSON(result.geometry,{style:{color:"#7548ac",weight:2,fillOpacity:.08}}).addTo(map);$("#coverage-fit-preview").disabled=false;}
    }catch(error){text("#coverage-preview-summary",error.message);}finally{setBusy(false);}
  };
  $("#coverage-fit-preview").onclick=()=>{if(previewLayer?.getBounds().isValid())map.fitBounds(previewLayer.getBounds(),{padding:[24,24],maxZoom:13});};
  $("#coverage-build").onclick=async()=>{
    if(busy||!state||(trackMode()?!preview:!chosen.length))return;
    if(!confirm(trackMode()?"Download the proposed smaller extracts and build coverage around your tracks? Sources are cropped and removed one at a time; published coverage remains available.":"Download and build these whole regional maps locally? This uses considerably more disk space."))return;
    const body=trackMode()?{mode:"tracks",preview_id:preview.id,revision:state.revision}:{mode:"regions",regions:chosen,revision:state.revision};
    setBusy(true);try{await api("/setup","POST",body);clearPreview();await refresh();}catch(error){status(error.message);if(error.status===409)clearPreview();}finally{setBusy(false);}
  };
  $("#coverage-retry").onclick=async()=>{if(!state||busy)return;setBusy(true);try{await api("/retry","POST",{revision:state.revision});await refresh();}catch(error){status(error.message);}finally{setBusy(false);}};
  $("#coverage-cancel").onclick=async()=>{if(!state||busy||!confirm("Cancel the staged setup? Published coverage will remain available."))return;setBusy(true);try{await api("/cancel","POST",{revision:state.revision});await refresh();}catch(error){status(error.message);}finally{setBusy(false);}};
  $("#coverage-refresh").onclick=refresh;
  $("#coverage-sport").onchange=()=>{activeKey=null;renderLayer();if(selectedSection)loadSection();refresh();};
  $("#coverage-unvisited").onchange=()=>{activeKey=null;renderLayer();};
  $("#coverage-prev").onclick=()=>{if(page>0){page--;loadSection();}};$("#coverage-next").onclick=()=>{page++;loadSection();};
  return {
    show(nextMap){map=nextMap;visible=true;$("#coverage-panel").hidden=false;$("#sport-filter").closest("label").hidden=true;$("#heat-color").closest("label").hidden=true;text("#heat-description","Unified recorded cycling and running on streets and paths.");map.off("moveend",locationNote).on("moveend",locationNote);activeKey=null;refresh();},
    hide(){visible=false;version++;sectionVersion++;clearTimeout(timer);if(layer){layer.remove();layer=null;}if(boundaries){boundaries.remove();boundaries=null;}if(previewLayer){previewLayer.remove();previewLayer=null;}if(map){map.off("moveend",locationNote);map._coverageTooltip?.remove();}activeKey=null;$("#coverage-panel").hidden=true;$("#sport-filter").closest("label").hidden=false;$("#heat-color").closest("label").hidden=false;}
  };
})();
