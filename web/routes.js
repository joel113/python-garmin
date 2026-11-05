"use strict";
window.routesUI=(()=>{
  let map,overviewLayer,selectedLayer,collections=[],rows=[],collection=null,collectionId="";
  let selectedId=null,detail=null,dirty=false,busy=false,version=0,selectionVersion=0,mapVersion=0,focusVersion=0,dragId=null,mapLoading=false;
  const cache=new Map(),palette=["#087e8b","#7b61a8","#c56b24","#3276b5","#548532","#b34267"];
  const status=text=>$("#routes-status").textContent=text;
  const active=()=>!$("#routes").hidden;
  async function request(path,method="GET",body) {
    const response=await fetch("/api/"+path,{method,headers:body?{"Content-Type":"application/json"}:{},body:body?JSON.stringify(body):undefined});
    let data;try{data=await response.json();}catch{throw Error("Could not read route data. Restart the app and refresh.");}
    if(!response.ok){const error=Error(data.error||"Route request failed (HTTP "+response.status+").");error.status=response.status;throw error;}
    return data;
  }
  function initMap() {
    if(!map){map=getMap("routes-map");map.createPane("planned-route-selection").style.zIndex="450";overviewLayer=L.featureGroup().addTo(map);selectedLayer=L.featureGroup().addTo(map);}
    map.invalidateSize();
  }
  function allowChange() {
    if(busy)return false;
    if(dirty) {
      if(!confirm("Discard your unsaved route notes and edits?"))return false;
      if(detail){$("#routes-name").value=detail.name;$("#routes-sport").value=detail.sport;$("#routes-notes").value=detail.notes;}
    }
    dirty=false;return true;
  }
  function updateControls() {
    $("#routes-rename-collection").disabled=busy||!collection;
    $("#routes-delete-collection").disabled=busy||!collection;
    $("#routes-show-collection").disabled=busy||!collection;
    $("#routes-fit-all").disabled=busy||!collection||mapLoading||!overviewLayer?.getLayers().length;
    $("#routes-add-form button").disabled=busy||!$("#routes-add-collection").value;
  }
  function setBusy(value) {
    busy=value;
    $("#routes").querySelectorAll("button,input,select,textarea").forEach(e=>e.disabled=value);
    if(!value){updateControls();renderList();}
  }
  function renderCollections() {
    $("#routes-collection").replaceChildren(new Option("All routes",""),...collections.map(c=>new Option(c.name+" ("+c.route_count+")",c.id)));
    $("#routes-collection").value=collectionId;
    updateControls();
  }
  function renderList() {
    $("#routes-list-help").textContent=collection?
      collection.name+" · "+rows.length+" routes · "+km(collection.distance_m)+". Drag to reorder, or use the arrows.":
      "All planned routes. Select a collection to set its order. Reuse a route through Add to collection below.";
    $("#routes-list").replaceChildren();
    if(!rows.length){$("#routes-list").textContent="No routes here yet. Upload GPX files to get started.";return;}
    rows.forEach((route,index)=>{
      const item=document.createElement("div");item.className="planned-route-item"+(route.id===selectedId?" active":"");
      item.draggable=Boolean(collection)&&!busy;
      const button=document.createElement("button");button.className="planned-route-select";button.disabled=busy;
      const heading=document.createElement("strong");heading.textContent=(collection?(index+1)+". ":"")+route.name;
      const meta=document.createElement("span");meta.className="small";meta.textContent=sportName(route.sport)+" · "+km(route.distance_m);
      const swatch=document.createElement("span");swatch.className="route-swatch";swatch.style.background=palette[index%palette.length];
      heading.prepend(swatch);button.append(heading,meta);button.onclick=()=>{if(selectedId===route.id||allowChange())selectRoute(route.id,true);};
      item.append(button);
      if(collection) {
        const controls=document.createElement("div");controls.className="route-order-buttons";
        for(const [symbol,delta,label] of [["↑",-1,"Move up"],["↓",1,"Move down"]]) {
          const move=document.createElement("button");move.textContent=symbol;move.title=label;move.setAttribute("aria-label",label+": "+route.name);
          move.disabled=busy||index+delta<0||index+delta>=rows.length;
          move.onclick=()=>moveRoute(route.id,index+delta);controls.append(move);
        }
        item.append(controls);
        item.ondragstart=event=>{if(busy)return event.preventDefault();dragId=route.id;event.dataTransfer.setData("text/plain",route.id);event.dataTransfer.effectAllowed="move";};
        item.ondragover=event=>{if(dragId&&dragId!==route.id){event.preventDefault();item.classList.add("drag-over");}};
        item.ondragleave=()=>item.classList.remove("drag-over");
        item.ondrop=event=>{event.preventDefault();item.classList.remove("drag-over");if(dragId&&rows.some(r=>r.id===dragId))moveRoute(dragId,index);dragId=null;};
        item.ondragend=()=>{dragId=null;document.querySelectorAll(".drag-over").forEach(e=>e.classList.remove("drag-over"));};
      }
      $("#routes-list").append(item);
    });
  }
  async function refresh(fit=false) {
    const token=++version;
    try {
      const result=await Promise.all([request("route-collections"),request("routes"+(collectionId?"?collection_id="+encodeURIComponent(collectionId):""))]);
      if(token!==version||!active())return;
      collections=result[0];rows=result[1].routes;collection=result[1].collection;
      renderCollections();renderList();
      if(selectedId&&!rows.some(r=>r.id===selectedId)&&!dirty){selectedId=null;detail=null;selectionVersion++;selectedLayer.clearLayers();$("#routes-detail").hidden=true;$(".routes-sidebar").classList.remove("has-selection");}
      if(!selectedId&&rows.length&&!dirty)selectedId=rows[0].id;
      status(rows.length+" planned route"+(rows.length===1?"":"s")+(collection?" in "+collection.name:" in the library")+".");
      drawOverview(fit);
      if(selectedId)await selectRoute(selectedId,fit&&!collection,true);
    } catch(error) {
      if(token!==version)return;
      if(error.status===404&&collectionId){collectionId="";await refresh(fit);return;}
      status(error.message);
    }
  }
  async function geometry(id,preview) {
    const key=id+":"+preview;
    if(cache.has(key)){const value=cache.get(key);cache.delete(key);cache.set(key,value);return value;}
    const data=await request("routes/"+encodeURIComponent(id)+"/geometry"+(preview?"?preview=1":""));
    cache.set(key,data);while(cache.size>40)cache.delete(cache.keys().next().value);
    return data;
  }
  async function drawOverview(fit=false) {
    const token=++mapVersion,focus=focusVersion;overviewLayer.clearLayers();
    if(!collection||!$("#routes-show-collection").checked){mapLoading=false;$("#routes-map-status").textContent="";updateControls();return;}
    const source=[...rows];let next=0,done=0,failed=0;mapLoading=true;updateControls();
    $("#routes-map-status").textContent="Loading collection routes…";
    const worker=async()=>{
      while(next<source.length&&token===mapVersion) {
        const index=next++,route=source[index];
        try {
          const data=await geometry(route.id,true);
          if(token!==mapVersion||!active())return;
          const color=palette[index%palette.length],line=L.polyline(data.parts,{color,weight:3,opacity:.5}).addTo(overviewLayer);
          const label=document.createElement("span");label.textContent=(index+1)+". "+route.name;line.bindTooltip(label);
          line.on("click",()=>{if(selectedId===route.id||allowChange())selectRoute(route.id,true);});
          const start=data.parts.find(part=>part.length)?.[0];
          if(start) {
            const icon=L.divIcon({className:"route-stage-icon",html:String(index+1),iconSize:[26,26],iconAnchor:[13,13]});
            const marker=L.marker(start,{icon}).addTo(overviewLayer);marker.bindTooltip(label.cloneNode(true));
            marker.on("click",()=>{if(selectedId===route.id||allowChange())selectRoute(route.id,true);});
          }
        } catch{failed++;}
        done++;if(token===mapVersion)$("#routes-map-status").textContent="Loading routes "+done+" / "+source.length;
      }
    };
    await Promise.all(Array.from({length:Math.min(4,source.length)},worker));
    if(token!==mapVersion)return;
    mapLoading=false;$("#routes-map-status").textContent=failed?failed+" route previews could not load.":"Stages are numbered in collection order.";
    updateControls();
    if(fit&&focus===focusVersion&&overviewLayer.getBounds().isValid())map.fitBounds(overviewLayer.getBounds(),{padding:[30,30],maxZoom:14});
  }
  function renderMemberships() {
    $("#routes-memberships").replaceChildren();
    for(const member of detail.collections) {
      const row=document.createElement("div");row.className="route-membership";
      const text=document.createElement("span");text.textContent=member.name+" · Position "+member.position;
      const remove=document.createElement("button");remove.textContent="Remove";remove.disabled=busy;
      remove.setAttribute("aria-label","Remove from "+member.name);
      remove.onclick=()=>removeMembership(member.id);row.append(text,remove);$("#routes-memberships").append(row);
    }
    if(!detail.collections.length)$("#routes-memberships").textContent="Not in a collection.";
    const available=collections.filter(c=>!detail.collections.some(m=>m.id===c.id));
    $("#routes-add-collection").replaceChildren(...available.map(c=>new Option(c.name,c.id)));
    if(!available.length)$("#routes-add-collection").add(new Option("No other collections",""));
    updateControls();
  }
  async function selectRoute(id,fit=false,preserve=false) {
    if(busy&&!preserve)return;
    const token=++selectionVersion;
    if(fit)focusVersion++;
    selectedId=id;renderList();
    try {
      const [route,data]=await Promise.all([request("routes/"+encodeURIComponent(id)),geometry(id,false)]);
      if(token!==selectionVersion||!active())return;
      const keep=dirty&&detail?.id===id;
      const oldRevision=detail?.revision;
      detail=route;if(keep)detail.revision=oldRevision;
      if(!keep) {
        $("#routes-name").value=route.name;$("#routes-sport").value=route.sport;$("#routes-notes").value=route.notes;dirty=false;
      }
      $("#routes-detail").hidden=false;$(".routes-sidebar").classList.add("has-selection");
      $("#routes-stats").textContent=km(route.distance_m)+" · "+route.point_count.toLocaleString()+" points"+
        (route.ascent_m==null?" · No elevation data":" · "+Math.round(route.ascent_m)+" m ascent"+(route.elevation_coverage<1?" (partial elevation)":""))+" · "+route.original_filename;
      $("#routes-download").href="/api/routes/"+encodeURIComponent(id)+"/download";
      $("#routes-notes-download").href="/api/routes/"+encodeURIComponent(id)+"/notes";
      renderMemberships();selectedLayer.clearLayers();
      const index=rows.findIndex(r=>r.id===id),color=palette[Math.max(0,index)%palette.length];
      L.polyline(data.parts,{pane:"planned-route-selection",color:"#fff",weight:8,opacity:1,interactive:false}).addTo(selectedLayer);
      const line=L.polyline(data.parts,{pane:"planned-route-selection",color,weight:5,opacity:1}).addTo(selectedLayer);
      const tooltip=document.createElement("span");tooltip.textContent=route.name;line.bindTooltip(tooltip);
      for(const point of data.waypoints) {
        const marker=L.circleMarker([point.lat,point.lon],{pane:"planned-route-selection",radius:5,color:"#c56b24",fillOpacity:.9}).addTo(selectedLayer);
        const label=document.createElement("span");label.textContent=[point.name,point.description].filter(Boolean).join(" · ")||"Waypoint";marker.bindTooltip(label);
      }
      requestAnimationFrame(()=>map.invalidateSize({pan:false}));
      if(fit&&selectedLayer.getBounds().isValid())map.fitBounds(selectedLayer.getBounds(),{padding:[30,30],maxZoom:16});
    } catch(error){if(token===selectionVersion)status(error.message);}
  }
  async function moveRoute(id,index) {
    if(!collection||busy)return;
    const ids=rows.map(r=>r.id),from=ids.indexOf(id);
    if(from<0||index<0||index>=ids.length||from===index)return;
    ids.splice(from,1);ids.splice(index,0,id);
    setBusy(true);
    try {await request("route-collections/"+collectionId+"/order","PUT",{revision:collection.revision,route_ids:ids});await refresh(false);}
    catch(error){await refresh(false);status(error.message);}
    finally{setBusy(false);}
  }
  async function removeMembership(cid) {
    if(!detail||!allowChange())return;
    setBusy(true);
    try {
      const current=await request("route-collections/"+cid);
      await request("route-collections/"+cid+"/members/"+detail.id,"DELETE",{revision:current.revision});
      await refresh(false);
    }catch(error){status(error.message);}finally{setBusy(false);}
  }
  $("#routes-collection").onchange=()=>{const next=$("#routes-collection").value;if(!allowChange()){$("#routes-collection").value=collectionId;return;}collectionId=next;refresh(true);};
  $("#routes-new-collection").onclick=async()=>{
    if(!allowChange())return;const name=prompt("Collection name");if(!name?.trim())return;setBusy(true);
    try{const result=await request("route-collections","POST",{name});collectionId=result.id;await refresh(true);}catch(error){status(error.message);}finally{setBusy(false);}
  };
  $("#routes-rename-collection").onclick=async()=>{
    if(!collection||busy)return;const name=prompt("Collection name",collection.name);if(!name?.trim())return;setBusy(true);
    try{await request("route-collections/"+collectionId,"PUT",{name,revision:collection.revision});await refresh(false);}catch(error){status(error.message);}finally{setBusy(false);}
  };
  $("#routes-delete-collection").onclick=async()=>{
    if(!collection||!allowChange()||!confirm('Delete collection "'+collection.name+'"? Routes and their notes stay in the library.'))return;setBusy(true);
    try{await request("route-collections/"+collectionId,"DELETE",{revision:collection.revision});collectionId="";await refresh(true);}catch(error){status(error.message);}finally{setBusy(false);}
  };
  $("#routes-upload").onclick=()=>$("#routes-files").click();
  $("#routes-files").onchange=async()=>{
    const files=[...$("#routes-files").files];if(!files.length)return;
    if(!allowChange()){$("#routes-files").value="";return;}
    const target=collectionId;setBusy(true);$("#routes-upload-results").hidden=false;$("#routes-upload-results").replaceChildren();
    let uploaded=null;
    for(const file of files) {
      const item=document.createElement("li");item.textContent=file.name+": uploading…";$("#routes-upload-results").append(item);
      try {
        if(file.size>25*1024*1024||!file.size)throw Error("File must be nonempty and no larger than 25 MiB.");
        const response=await fetch("/api/routes/upload?filename="+encodeURIComponent(file.name)+(target?"&collection_id="+encodeURIComponent(target):""),{method:"POST",headers:{"Content-Type":"application/gpx+xml"},body:file});
        const result=await response.json();if(!response.ok)throw Error(result.error||"Upload failed.");
        uploaded=result.route.id;item.textContent=file.name+": "+(result.duplicate?"already in library":"uploaded")+(result.attached?"; added to collection":"");item.className="upload-success";
      }catch(error){item.textContent=file.name+": "+error.message;item.className="upload-error";}
    }
    $("#routes-files").value="";selectedId=uploaded||selectedId;
    try{await refresh(true);}finally{setBusy(false);}
  };
  $("#routes-edit-form").oninput=()=>{dirty=true;};
  $("#routes-reload-route").onclick=()=>{if(detail&&allowChange())selectRoute(detail.id,false);};
  $("#routes-edit-form").onsubmit=async event=>{
    event.preventDefault();if(!detail||busy)return;setBusy(true);
    try {
      detail=await request("routes/"+detail.id,"PUT",{revision:detail.revision,name:$("#routes-name").value,sport:$("#routes-sport").value,notes:$("#routes-notes").value});
      dirty=false;await refresh(false);status("Route and notes saved.");
    }catch(error){status(error.message);}finally{setBusy(false);}
  };
  $("#routes-add-form").onsubmit=async event=>{
    event.preventDefault();const cid=$("#routes-add-collection").value;if(!detail||!cid||busy)return;setBusy(true);
    try{const current=await request("route-collections/"+cid);await request("route-collections/"+cid+"/members","POST",{revision:current.revision,route_id:detail.id});await refresh(false);}
    catch(error){status(error.message);}finally{setBusy(false);}
  };
  $("#routes-delete-route").onclick=async()=>{
    if(!detail||!allowChange()||!confirm('Delete "'+detail.name+'" from the library, including its original file, notes, and all collection memberships?'))return;setBusy(true);
    try {await request("routes/"+detail.id,"DELETE",{revision:detail.revision});selectedId=null;detail=null;$("#routes-detail").hidden=true;selectedLayer.clearLayers();$(".routes-sidebar").classList.remove("has-selection");await refresh(false);}
    catch(error){status(error.message);}finally{setBusy(false);}
  };
  $("#routes-focus").onclick=()=>{if(map&&selectedLayer.getBounds().isValid())map.fitBounds(selectedLayer.getBounds(),{padding:[30,30],maxZoom:16});};
  $("#routes-show-collection").onchange=()=>drawOverview(true);
  $("#routes-fit-all").onclick=()=>{if(overviewLayer.getBounds().isValid())map.fitBounds(overviewLayer.getBounds(),{padding:[30,30],maxZoom:14});};
  return {
    async show(){try{initMap();await refresh(true);}catch(error){status(error.message);}},
    hide(){version++;selectionVersion++;mapVersion++;},
    allowNavigate(view){return view==="routes"||allowChange();}
  };
})();
