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
  $("#compare-sport").replaceChildren(new Option("All sports","all"));
  [...sports.keys()].sort().forEach(key=>$("#compare-sport").add(new Option(sportName(key),key)));
  populateComparisonPeriods();
}
// Use the recorded calendar date consistently for FIT and GPX timestamps.
// UTC arithmetic avoids browser timezone shifts and daylight-saving changes.
function comparisonDate(value) {
  const match=/^(\d{4})-(\d{2})-(\d{2})/.exec(value || "");
  if (!match) return null;
  const [year,month,day]=match.slice(1).map(Number);
  const result=new Date(Date.UTC(year,month-1,day));
  return result.getUTCFullYear()===year && result.getUTCMonth()===month-1 && result.getUTCDate()===day ? result : null;
}
function periodStart(date,unit) {
  const result=new Date(date);
  if (unit==="year") return new Date(Date.UTC(result.getUTCFullYear(),0,1));
  if (unit==="month") return new Date(Date.UTC(result.getUTCFullYear(),result.getUTCMonth(),1));
  result.setUTCDate(result.getUTCDate()-(result.getUTCDay()+6)%7);
  return result;
}
function nextPeriod(date,unit) {
  const result=new Date(date);
  if (unit==="year") result.setUTCFullYear(result.getUTCFullYear()+1);
  else if (unit==="month") result.setUTCMonth(result.getUTCMonth()+1);
  else result.setUTCDate(result.getUTCDate()+7);
  return result;
}
function periodLabel(start,unit) {
  if (unit==="year") return String(start.getUTCFullYear());
  if (unit==="month") return start.toLocaleDateString(undefined,{month:"long",year:"numeric",timeZone:"UTC"});
  const thursday=new Date(start); thursday.setUTCDate(thursday.getUTCDate()+3);
  const year=thursday.getUTCFullYear();
  const firstMonday=periodStart(new Date(Date.UTC(year,0,4)),"week");
  const week=1+Math.round((start-firstMonday)/604800000);
  const end=new Date(start); end.setUTCDate(end.getUTCDate()+6);
  const format=d=>d.toLocaleDateString(undefined,{month:"short",day:"numeric",timeZone:"UTC"});
  return year+" · Week "+String(week).padStart(2,"0")+" ("+format(start)+" – "+format(end)+")";
}
function populateComparisonPeriods() {
  const unit=$("#compare-unit").value;
  const dates=activities.map(a=>comparisonDate(a.start_time)).filter(Boolean).sort((a,b)=>a-b);
  const selects=[$("#compare-a"),$("#compare-b")];
  selects.forEach(select=>{select.replaceChildren(); select.disabled=!dates.length;});
  if (dates.length) {
    // Include the preceding period even when the archive contains just one.
    let first=periodStart(dates[0],unit);
    const last=periodStart(dates.at(-1),unit);
    if (+first===+last) {
      first=new Date(first);
      if (unit==="year") first.setUTCFullYear(first.getUTCFullYear()-1);
      else if (unit==="month") first.setUTCMonth(first.getUTCMonth()-1);
      else first.setUTCDate(first.getUTCDate()-7);
    }
    const periods=[];
    for (let cursor=first;cursor<=last;cursor=nextPeriod(cursor,unit)) periods.push(cursor);
    periods.reverse().forEach(start=>selects.forEach(select=>select.add(new Option(periodLabel(start,unit),start.toISOString().slice(0,10)))));
    selects[1].selectedIndex=Math.min(1,selects[1].options.length-1);
  }
  renderComparison();
}
function renderComparison() {
  const unit=$("#compare-unit").value, sport=$("#compare-sport").value;
  const keys=[$("#compare-a").value,$("#compare-b").value];
  if (!keys[0] || !keys[1]) {
    $("#comparison-results").textContent="No dated activities available to compare.";
    $("#comparison-note").textContent="";
    return;
  }
  const totals=keys.map(()=>({count:0,distance:0,duration:0}));
  let undated=0;
  for (const activity of activities) {
    if (sport!=="all" && (activity.sport || "Unspecified")!==sport) continue;
    const date=comparisonDate(activity.start_time);
    if (!date) { undated++; continue; }
    const key=periodStart(date,unit).toISOString().slice(0,10);
    keys.forEach((selected,index)=>{
      if (key!==selected) return;
      totals[index].count++; totals[index].distance+=number(activity.distance_m); totals[index].duration+=number(activity.duration_s);
    });
  }
  const metrics=[["Activities","count",value=>value.toLocaleString()],["Distance","distance",km],["Recorded time","duration",hours]];
  const labels=[$("#compare-a").selectedOptions[0].text,$("#compare-b").selectedOptions[0].text];
  let html='<table class="comparison-table"><caption class="small">Period A compared with period B</caption><thead><tr><th scope="col">Metric</th><th scope="col">A · '+escape(labels[0])+'</th><th scope="col">B · '+escape(labels[1])+'</th><th scope="col">Change A vs B</th></tr></thead><tbody>';
  for (const [label,key,format] of metrics) {
    const a=totals[0][key],b=totals[1][key],max=Math.max(a,b);
    const delta=a-b;
    const percent=b ? " ("+(delta>0?"+":"")+(100*delta/b).toLocaleString(undefined,{maximumFractionDigits:1})+"%)" : (a ? " (no baseline)" : "");
    const change=(delta>0?"+":delta<0?"−":"")+format(Math.abs(delta))+percent;
    html+='<tr><th scope="row">'+label+'</th>'+[a,b].map((value,index)=>'<td><strong>'+format(value)+'</strong><div class="compare-bar"><span class="period-'+index+'" style="width:'+(max?100*value/max:0)+'%"></span></div></td>').join("")+'<td>'+escape(change)+'</td></tr>';
  }
  $("#comparison-results").innerHTML=html+"</tbody></table>";
  $("#comparison-note").textContent="Weeks run Monday–Sunday (ISO week numbers). Totals use recorded calendar dates; ongoing or partially imported periods may be incomplete. Missing distance or duration contributes zero."+ (undated ? " "+undated+" activities without a valid date are excluded." : "");
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
$("#compare-unit").onchange=populateComparisonPeriods;
["#compare-sport","#compare-a","#compare-b"].forEach(selector=>$(selector).onchange=renderComparison);
getJSON("/api/activities").then(data=>{activities=data; summary(); renderList();}).catch(error=>{ $("#date-range").textContent="Activity data could not be loaded. Restart the server and refresh."; report(error); });
