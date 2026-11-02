"use strict";
window.destinationUI=(()=>{
  let map,placesLayer,endpointsLayer,places=[],selectedId=null,poll=null,version=0,detailVersion=0;
  let page=0,settingsDirty=false,nameDirty=false,lastPublication=null,busy=false;
  const active=()=>!$("#destinations").hidden;
  const status=text=>$("#destination-status").textContent=text;
  const name=place=>place.name || place.id;
  const sport=()=>$("#destination-sport").value;
  const sportQuery=()=>"?sport="+encodeURIComponent(sport());
  async function request(path="",method="GET",body) {
    const response=await fetch("/api/destinations"+path,{method,
      headers:body?{"Content-Type":"application/json"}:{},body:body?JSON.stringify(body):undefined});
    let data;
    try {data=await response.json();} catch {throw Error("Could not read destination data. Restart the server and refresh.");}
    if(!response.ok) throw Error(data.error || "Destination request failed (HTTP "+response.status+").");
    return data;
  }
  function setSports() {
    const selected=sport();
    const sports=[...new Set(activities.map(a=>a.sport).filter(Boolean))].sort();
    $("#destination-sport").replaceChildren(new Option("All sports","all"),...sports.map(s=>new Option(sportName(s),s)));
    if(sports.includes(selected)) $("#destination-sport").value=selected;
  }
  function initMap() {
    if(!map) {
      map=getMap("destination-map");
      placesLayer=L.featureGroup().addTo(map);
      endpointsLayer=L.featureGroup().addTo(map);
    }
    map.invalidateSize();
  }
  function renderPlaces(fit) {
    $("#destination-list").replaceChildren();
    placesLayer.clearLayers();
    if(!places.length) $("#destination-list").textContent="No destinations meet the clustering settings for this sport yet.";
    places.forEach((place,index)=>{
      const button=document.createElement("button");button.className="activity"+(place.id===selectedId?" active":"");
      button.innerHTML="<strong>"+(index+1)+". "+escape(name(place))+"</strong><span class='small'>"+place.trip_count+" trips · "+place.arrival_count+" arrivals · "+place.departure_count+" departures</span>";
      button.onclick=()=>selectDestination(place.id,true);
      $("#destination-list").append(button);
      const marker=L.circleMarker([place.centroid_lat,place.centroid_lon],{
        radius:Math.min(18,6+Math.log2(place.trip_count+1)),weight:2,
        color:place.id===selectedId?"#d66c19":"#087e8b",fillOpacity:0.65}).addTo(placesLayer);
      const label=document.createElement("span");label.textContent=name(place)+" · "+place.trip_count+" trips";
      marker.bindTooltip(label);marker.on("click",()=>selectDestination(place.id,true));
    });
    if(fit && places.length) map.fitBounds(placesLayer.getBounds(),{padding:[35,35],maxZoom:14});
  }
  function renderProcessing(data) {
    const state=data.processing;
    if(!settingsDirty) {
      $("#destination-epsilon").value=state.epsilon_m;
      $("#destination-min-trips").value=state.min_trips;
    }
    let message;
    if(state.status==="completed") message=places.length+" destinations found.";
    else if(state.status==="failed") message=state.error+" Use Recluster destinations to retry.";
    else if(state.status==="processing") message=state.phase==="endpoints"
      ? "Preparing trip endpoints: "+state.progress_done+" / "+state.progress_total
      : "Clustering trip endpoints…";
    else message="Destination processing is queued…";
    if(state.status!=="completed" && state.published_revision>0) message+=" Previous results remain visible while updating.";
    status(message);
    $("#destination-count").textContent=places.length+" destinations · "+data.unassigned_endpoint_count+" unassigned endpoints · "+data.excluded_activity_count+" activities without usable trip endpoints";
  }
  async function refresh(fit=false) {
    const token=++version;
    clearTimeout(poll);
    try {
      const data=await request(sportQuery());
      if(token!==version || !active()) return;
      const publicationChanged=lastPublication!==data.processing.published_revision;
      lastPublication=data.processing.published_revision;
      places=data.destinations;
      if(selectedId && !places.some(p=>p.id===selectedId)) {
        selectedId=null;detailVersion++;nameDirty=false;endpointsLayer.clearLayers();$("#destination-detail").hidden=true;
        $(".destination-sidebar").classList.remove("has-selection");
      }
      renderPlaces(fit);renderProcessing(data);
      if(selectedId && publicationChanged) {page=0;await selectDestination(selectedId,false);}
      if(["pending","processing"].includes(data.processing.status)) poll=setTimeout(()=>refresh(false),2000);
    } catch(error) {
      if(token===version) {
        status(error.message);
        if(active()) poll=setTimeout(()=>refresh(false),5000);
      }
    }
  }
  async function selectDestination(id,fit=false) {
    if(id!==selectedId) {page=0;nameDirty=false;}
    selectedId=id;
    const token=++detailVersion;
    renderPlaces(false);
    $("#destination-detail").hidden=false;
    $(".destination-sidebar").classList.add("has-selection");
    $("#destination-activities").textContent="Loading trips…";
    try {
      const result=await request("/"+encodeURIComponent(id)+sportQuery());
      if(token!==detailVersion || !active()) return;
      const place=result.destination;
      $("#destination-title").textContent=name(place);
      $("#destination-id").textContent=place.id+" · "+place.centroid_lat.toFixed(5)+", "+place.centroid_lon.toFixed(5);
      if(!nameDirty) $("#destination-name").value=place.name || "";
      $("#destination-summary").textContent=place.trip_count+" distinct trips · "+place.departure_count+" departures · "+place.arrival_count+" arrivals · Latest visit: "+date(place.latest_visit);
      endpointsLayer.clearLayers();
      for(const point of result.endpoints) {
        const dot=L.circleMarker([point.lat,point.lon],{radius:4,weight:1,color:point.role==="start"?"#278154":"#d66c19",fillOpacity:0.55}).addTo(endpointsLayer);
        const label=document.createElement("span");label.textContent=(point.role==="start"?"Departure":"Arrival")+" · Activity "+point.activity_id;
        dot.bindTooltip(label);
        dot.on("click",()=>{if(switchView("tracks")) showTrack(point.activity_id);});
      }
      if(fit && result.endpoints.length) map.fitBounds(endpointsLayer.getBounds(),{padding:[40,40],maxZoom:17});
      await renderTrips(token);
    } catch(error) {if(token===detailVersion) status(error.message);}
  }
  async function renderTrips(token=detailVersion) {
    if(!selectedId) return;
    try {
      const data=await request("/"+encodeURIComponent(selectedId)+"/activities"+sportQuery()+"&offset="+(page*50)+"&limit=50");
      if(token!==detailVersion || !active()) return;
      $("#destination-activities").replaceChildren();
      for(const activity of data.items) {
        const button=document.createElement("button");button.className="activity";
        button.innerHTML="<strong>"+escape(date(activity.start_time))+" · "+escape(sportName(activity.sport))+"</strong><span class='small'>"+
          escape(km(activity.distance_m))+" · "+(activity.departed && activity.arrived?"Round trip":activity.arrived?"Arrival":"Departure")+" · "+escape(activity.id)+"</span>";
        button.onclick=()=>{if(switchView("tracks")) showTrack(activity.id);};
        $("#destination-activities").append(button);
      }
      $("#destination-prev").disabled=page===0;
      $("#destination-next").disabled=(page+1)*50>=data.total;
      $("#destination-page").textContent=data.total?(data.offset+1)+"–"+(data.offset+data.items.length)+" of "+data.total:"No trips";
    } catch(error) {if(token===detailVersion) status(error.message);}
  }
  $("#destination-sport").onchange=()=>{detailVersion++;selectedId=null;nameDirty=false;endpointsLayer.clearLayers();$("#destination-detail").hidden=true;$(".destination-sidebar").classList.remove("has-selection");refresh(true);};
  $("#destination-settings-form").oninput=()=>{settingsDirty=true;};
  $("#destination-name").oninput=()=>{nameDirty=true;};
  $("#destination-settings-form").onsubmit=async event=>{
    event.preventDefault();if(busy) return;
    busy=true;const button=event.submitter || $("#destination-settings-form button[type=submit]");button.disabled=true;
    try {
      await request("/settings","PUT",{epsilon_m:Number($("#destination-epsilon").value),min_trips:Number($("#destination-min-trips").value)});
      settingsDirty=false;await refresh(false);
    } catch(error) {status(error.message);} finally {busy=false;button.disabled=false;}
  };
  $("#destination-name-form").onsubmit=async event=>{
    event.preventDefault();if(!selectedId || busy)return;
    busy=true;const id=selectedId,button=event.submitter || $("#destination-name-form button[type=submit]");button.disabled=true;
    try {
      await request("/"+encodeURIComponent(id),"PUT",{name:$("#destination-name").value});
      nameDirty=false;await refresh(false);if(selectedId===id)await selectDestination(id,false);
    } catch(error) {status(error.message);} finally {busy=false;button.disabled=false;}
  };
  $("#destination-prev").onclick=()=>{page=Math.max(0,page-1);detailVersion++;renderTrips();};
  $("#destination-next").onclick=()=>{page++;detailVersion++;renderTrips();};
  return {
    setSports,
    async show(){try{initMap();setSports();await refresh(!places.length);if(selectedId)await selectDestination(selectedId,false);}catch(error){status(error.message);}},
    hide(){clearTimeout(poll);version++;detailVersion++;}
  };
})();
