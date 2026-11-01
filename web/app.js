"use strict";
const $ = selector => document.querySelector(selector);
const number = value => Number(value) || 0;
const km = value => (number(value)/1000).toLocaleString(undefined,{maximumFractionDigits:1})+" km";
const hours = value => (number(value)/3600).toLocaleString(undefined,{maximumFractionDigits:1})+" h";
const date = value => value ? new Date(value).toLocaleDateString() : "Unknown date";
const sportName = value => (value || "Unspecified").replaceAll("_"," ");
const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let activities=[], selected=null, heatVersion=0, trackVersion=0;
const maps={}, layers={};
async function getJSON(url) {
  const response=await fetch(url);
  if (!response.ok) throw new Error("Could not load activity data (HTTP "+response.status+").");
  return response.json();
}
function report(error) { $("#error").textContent=error.message; $("#error").hidden=false; }
function getMap(id) {
  if (!window.L) throw new Error("The map library could not load. Check your internet connection and refresh.");
  if (!maps[id]) {
    maps[id]=L.map(id,{preferCanvas:true}).setView([48.1351,11.582],11);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"© OpenStreetMap contributors"}).addTo(maps[id]);
  }
  maps[id].invalidateSize();
  return maps[id];
}
function summary() {
  const distance=activities.reduce((s,a)=>s+number(a.distance_m),0);
  const duration=activities.reduce((s,a)=>s+number(a.duration_s),0);
  const withGPS=activities.filter(a=>a.has_track).length;
  $("#cards").innerHTML=[["Activities",activities.length.toLocaleString()],["Total distance",km(distance)],["Recorded time",hours(duration)],["Activities with GPS",withGPS.toLocaleString()]].map(([label,value])=>'<div class="card"><div class="card-label">'+label+'</div><div class="card-value">'+value+'</div></div>').join("");
  const dates=activities.map(a=>a.start_time).filter(Boolean).sort();
  $("#date-range").textContent=dates.length ? date(dates[0])+" – "+date(dates.at(-1)) : "Import activities to see your statistics.";
  const sports=new Map();
  for (const a of activities) {
    const key=a.sport || "Unspecified", s=sports.get(key) || {count:0,distance:0};
    s.count++; s.distance+=number(a.distance_m); sports.set(key,s);
  }
  $("#sports").innerHTML=[...sports].sort((a,b)=>b[1].count-a[1].count).map(([key,s])=>'<div class="sport-row"><div class="row-heading"><strong>'+escape(sportName(key))+'</strong><span>'+s.count+' · '+km(s.distance)+'</span></div><div class="bar"><span style="width:'+(100*s.count/activities.length)+'%"></span></div></div>').join("") || '<p class="muted">No activities yet.</p>';
  $("#recent").replaceChildren();
  for (const a of activities.slice(0,8)) {
    const button=document.createElement("button"); button.className="recent-row";
    button.textContent=date(a.start_time)+" · "+sportName(a.sport)+" · "+km(a.distance_m);
    button.onclick=()=>{switchView("tracks"); showTrack(a.id);};
    $("#recent").append(button);
  }
  $("#sport-filter").replaceChildren(new Option("All sports","all"));
  [...sports.keys()].sort().forEach(key=>$("#sport-filter").add(new Option(sportName(key),key)));
}
function renderList() {
  const query=$("#search").value.toLowerCase();
  const filtered=activities.filter(a=>(a.start_time+" "+a.sport+" "+a.id).toLowerCase().includes(query));
  const fragment=document.createDocumentFragment();
  for (const a of filtered) {
    const button=document.createElement("button");
    button.className="activity"+(a.id===selected ? " active":"");
    button.innerHTML="<strong>"+escape(date(a.start_time))+" · "+escape(sportName(a.sport))+"</strong><span class='small'>"+escape(km(a.distance_m))+" · "+escape(a.id)+(a.has_track ? "" : " · No GPS")+"</span>";
    button.onclick=()=>showTrack(a.id); fragment.append(button);
  }
  if (!filtered.length) { const empty=document.createElement("p"); empty.className="empty"; empty.textContent="No activities found."; fragment.append(empty); }
  $("#activities").replaceChildren(fragment);
}
async function showTrack(id) {
  const version=++trackVersion; selected=id; renderList();
  $("#track-status").textContent="Loading route…";
  try {
    const map=getMap("map");
    if (layers.track) { map.removeLayer(layers.track); layers.track=null; }
    const points=await getJSON("/api/track/"+encodeURIComponent(id));
    if (version!==trackVersion) return;
    if (!points.length) { $("#track-status").textContent="This activity has no GPS track."; return; }
    layers.track=L.polyline(points,{color:"#087e8b",weight:4}).addTo(map);
    map.invalidateSize(); map.fitBounds(layers.track.getBounds(),{padding:[24,24],maxZoom:16});
    const a=activities.find(a=>a.id===id);
    $("#track-status").textContent=a ? date(a.start_time)+" · "+sportName(a.sport)+" · "+km(a.distance_m)+" · "+hours(a.duration_s) : id;
  } catch(error) { $("#track-status").textContent="Could not load this route."; report(error); }
}
function heatStyle() {
  const individual=$("#heat-mode").value==="individual";
  return {color:$("#heat-color").value, weight:individual ? 3 : 2, opacity:individual ? 0.8 : 0.18};
}
function updateHeatStyle() {
  if (layers.heat) layers.heat.eachLayer(line=>line.setStyle(heatStyle()));
  $("#heat-description").textContent=$("#heat-mode").value==="individual"
    ? "Hover over a route to highlight it. Click a route to open its full track."
    : "All routes together. Brighter paths show repeated visits.";
}
async function showHeat() {
  const version=++heatVersion; $("#heat-status").textContent="Loading routes…";
  try {
    const map=getMap("heatmap");
    const routes=await getJSON("/api/heatmap?sport="+encodeURIComponent($("#sport-filter").value));
    if (version!==heatVersion) return;
    if (layers.heat) map.removeLayer(layers.heat);
    layers.heat=L.featureGroup().addTo(map);
    for (const route of routes) {
      if (!route.segments.length) continue;
      const line=L.polyline(route.segments,{...heatStyle(),smoothFactor:0.25}).addTo(layers.heat);
      const label=document.createElement("span"); label.textContent=sportName(route.sport)+" · "+route.id;
      line.bindTooltip(label);
      line.on("mouseover",()=>{
        if ($("#heat-mode").value==="individual") {
          line.setStyle({weight:6,opacity:1}); line.bringToFront();
        }
      });
      line.on("mouseout",()=>line.setStyle(heatStyle()));
      line.on("click",()=>{switchView("tracks"); showTrack(route.id);});
    }
    const bounds=layers.heat.getBounds();
    map.invalidateSize(); if (bounds.isValid()) map.fitBounds(bounds,{padding:[24,24],maxZoom:15});
    $("#heat-status").textContent=bounds.isValid() ? routes.length.toLocaleString()+" routes · Zoom in to explore individual paths" : "No GPS tracks for this sport.";
  } catch(error) { $("#heat-status").textContent="Could not load routes."; report(error); }
}
function switchView(view) {
  document.querySelectorAll(".view").forEach(section=>section.hidden=section.id!==view);
  document.querySelectorAll("[data-view]").forEach(button=>{if(button.dataset.view===view) button.setAttribute("aria-current","page"); else button.removeAttribute("aria-current");});
  requestAnimationFrame(()=>{
    try {
      if (view==="heat") showHeat();
      if (view==="tracks") {
        getMap("map");
        if (!selected) { const first=activities.find(a=>a.has_track); if (first) showTrack(first.id); }
      }
    } catch(error) { report(error); }
  });
}
document.querySelectorAll("[data-view]").forEach(button=>button.onclick=()=>switchView(button.dataset.view));
$("#sport-filter").onchange=showHeat; $("#search").oninput=renderList;
$("#heat-mode").onchange=updateHeatStyle;
$("#heat-color").oninput=updateHeatStyle;
getJSON("/api/activities").then(data=>{activities=data; summary(); renderList();}).catch(error=>{ $("#date-range").textContent="Activity data could not be loaded. Restart the server and refresh."; report(error); });
