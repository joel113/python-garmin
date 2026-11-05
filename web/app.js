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
let trackSegmentData=[], trackSegmentPoll=null, trackSegmentRequest=0;
let trackCoordinates=[],trackSpeedData=null,trackTemperatureData=null,trackSpeedRequest=0;
const trackColorMode=()=>$("#track-color-temperature").checked?"temperature":$("#track-color-speed").checked?"speed":null;
const segmentTime=value=>{
  if(value==null) return "Untimed";
  const tenths=Math.round(Number(value)*10), h=Math.floor(tenths/36000), m=Math.floor(tenths%36000/600), s=(tenths%600/10).toFixed(1).padStart(4,"0");
  return (h ? h+":"+String(m).padStart(2,"0") : String(m))+":"+s;
};
const medalInfo=rank=>({
  1:{image:"/assets/medals/gold-abstract.png",label:"Gold · 1st",color:"#b8860b",className:"gold"},
  2:{image:"/assets/medals/silver-abstract.png",label:"Silver · 2nd",color:"#62788b",className:"silver"},
  3:{image:"/assets/medals/bronze-abstract.png",label:"Bronze · 3rd",color:"#ad622e",className:"bronze"}
}[rank]);
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
  if (window.segmentUI) window.segmentUI.setSports();
  if (window.destinationUI) window.destinationUI.setSports();
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
    const medals=document.createElement("span");medals.className="activity-medals";
    for(const rank of [1,2,3]) {
      const awards=(a.medals || []).filter(m=>m.rank===rank);
      if(!awards.length) continue;
      const medal=medalInfo(rank),badge=document.createElement("span");badge.className="activity-medal medal-"+medal.className;
      const image=document.createElement("img");image.src=medal.image;image.alt=medal.label;image.width=24;image.height=28;image.loading="lazy";
      badge.title=awards.map(m=>m.name).join("\n");
      badge.setAttribute("aria-label",medal.label+": "+awards.length+" segment"+(awards.length===1?"":"s"));
      const count=document.createElement("span");count.textContent=awards.length+" x";
      badge.append(count,image);
      medals.append(badge);
    }
    if(medals.childElementCount) button.append(medals);
    button.onclick=()=>showTrack(a.id); fragment.append(button);
  }
  if (!filtered.length) { const empty=document.createElement("p"); empty.className="empty"; empty.textContent="No activities found."; fragment.append(empty); }
  $("#activities").replaceChildren(fragment);
}
async function showTrack(id) {
  const version=++trackVersion; selected=id; renderList();
  trackSpeedRequest++;trackCoordinates=[];trackSpeedData=null;trackTemperatureData=null;
  $("#track-speed-legend").hidden=true;
  clearTimeout(trackSegmentPoll);
  trackSegmentData=[];
  if(layers.trackSegments) layers.trackSegments.clearLayers();
  if(layers.trackSegmentFocus) layers.trackSegmentFocus.clearLayers();
  $("#track-details").hidden=false;
  const activity=activities.find(a=>a.id===id);
  $("#track-overview").textContent=activity ? date(activity.start_time)+" · "+sportName(activity.sport)+" · "+km(activity.distance_m)+" · "+hours(activity.duration_s)+" · Activity "+id : "Activity "+id;
  $("#track-segment-results").replaceChildren();
  $("#track-segment-status").textContent="Loading segment results…";
  $("#track-status").textContent="Loading route…";
  try {
    const map=getMap("map");
    ensureTrackSegmentPanes();
    if(window.routeMatchUI) window.routeMatchUI.show("track",id,map);
    loadTrackSegments(id,version);
    if (layers.track) { map.removeLayer(layers.track); layers.track=null; }
    const points=await getJSON("/api/track/"+encodeURIComponent(id));
    if (version!==trackVersion) return;
    if (!points.length) { $("#track-status").textContent="This activity has no GPS track."; return; }
    trackCoordinates=points;
    renderTrackRoute();
    map.invalidateSize(); map.fitBounds(layers.track.getBounds(),{padding:[24,24],maxZoom:16});
    if(trackColorMode()) loadTrackSpeed();
    const a=activities.find(a=>a.id===id);
    $("#track-status").textContent=a ? date(a.start_time)+" · "+sportName(a.sport)+" · "+km(a.distance_m)+" · "+hours(a.duration_s) : id;
  } catch(error) { $("#track-status").textContent="Could not load this route."; report(error); }
}
function renderTrackRoute() {
  if(!maps.map || !trackCoordinates.length) return;
  if(layers.track) maps.map.removeLayer(layers.track);
  const mode=trackColorMode(),temperature=mode==="temperature";
  const data=temperature?trackTemperatureData:mode==="speed"?trackSpeedData:null;
  const coloring=data && (!temperature || data.known_edge_count>0);
  $("#track-speed-legend").hidden=!data;
  $("#track-color-low").textContent=temperature?"Cold · blue":"Slow · blue";
  $("#track-color-high").textContent=temperature?"Warm · red":"Fast · red";
  $("#track-color-note").textContent=temperature?
    "Recorded sensor temperature in °C. Gray sections have missing readings. Segment overlays keep their medal colors.":
    "Speed estimated from GPS distance and timestamps. Gray means unavailable; red starts at this track's 95th-percentile speed. Segment overlays keep their medal colors.";
  if(!coloring) {
    layers.track=L.polyline(trackCoordinates,{color:"#087e8b",weight:4}).addTo(maps.map);
    layers.track.bringToBack();
    if(temperature && data) $("#track-speed-range").textContent=data.message || "No usable recorded temperature data for this track.";
    requestAnimationFrame(()=>maps.map.invalidateSize({pan:false}));
    return;
  }
  layers.track=L.featureGroup().addTo(maps.map);
  const floor=temperature?data.scale_min_c:0,ceiling=temperature?data.scale_max_c:data.scale_max_kmh;
  for(const segment of data.segments) {
    const samples=temperature?segment.temperatures_c:segment.speeds_kmh;
    let chunk=[],values=[],previousBin=null;
    const finish=()=>{
      if(chunk.length<2) return;
      const color=previousBin===-1?"#94a3b8":"hsl("+(240-240*previousBin/19)+",90%,45%)";
      const line=L.polyline(chunk,{color,weight:4.5,opacity:1}).addTo(layers.track);
      const tooltip=document.createElement("span");
      const minimum=values.reduce((a,b)=>Math.min(a,b),Infinity),maximum=values.reduce((a,b)=>Math.max(a,b),-Infinity);
      tooltip.textContent=values.length ? (temperature?"Recorded temperature: ":"Estimated speed: ")+minimum.toFixed(1)+"–"+maximum.toFixed(1)+(temperature?" °C":" km/h") :
        temperature?"Temperature unavailable":"Speed unavailable";
      line.bindTooltip(tooltip);
    };
    for(let index=0;index<samples.length;index++) {
      const speed=samples[index];
      const ratio=temperature?(ceiling>floor?(speed-floor)/(ceiling-floor):0.5):(ceiling>0?speed/ceiling:0);
      const bin=speed==null?-1:Math.max(0,Math.min(19,Math.floor(ratio*19)));
      if(bin!==previousBin) {
        finish();chunk=[segment.points[index]];values=[];previousBin=bin;
      }
      chunk.push(segment.points[index+1]);
      if(speed!=null) values.push(speed);
    }
    finish();
  }
  layers.track.bringToBack();
  $("#track-speed-range").textContent=temperature ?
    (floor===ceiling?"Constant temperature · "+floor.toFixed(1)+" °C":floor.toFixed(1)+" °C → "+ceiling.toFixed(1)+" °C") :
    ceiling==null ? "No usable timestamps: the route is shown in gray." :
    ceiling===0 ? "Stationary track · 0 km/h" : "0 km/h → "+ceiling.toFixed(1)+" km/h and faster";
  requestAnimationFrame(()=>maps.map.invalidateSize({pan:false}));
}
async function loadTrackSpeed() {
  const token=++trackSpeedRequest,version=trackVersion,id=selected,mode=trackColorMode();
  if(!id || !trackCoordinates.length) return;
  if(!mode) {renderTrackRoute();return;}
  if(mode==="speed"?trackSpeedData:trackTemperatureData) {renderTrackRoute();return;}
  $("#track-speed-legend").hidden=false;
  $("#track-speed-range").textContent=mode==="temperature"?"Loading recorded temperature…":"Loading speed data…";
  try {
    const data=await getJSON("/api/track/"+encodeURIComponent(id)+"/"+mode);
    if(token!==trackSpeedRequest || version!==trackVersion || trackColorMode()!==mode) return;
    if(mode==="temperature") trackTemperatureData=data;else trackSpeedData=data;
    renderTrackRoute();
  } catch(error) {
    if(token===trackSpeedRequest && version===trackVersion) $("#track-speed-range").textContent="Track coloring could not be loaded. "+error.message;
  }
}
async function loadTrackSegments(id,version) {
  const request=++trackSegmentRequest;
  clearTimeout(trackSegmentPoll);
  try {
    const result=await getJSON("/api/track/"+encodeURIComponent(id)+"/segments");
    if(version!==trackVersion || request!==trackSegmentRequest || $("#tracks").hidden) return;
    trackSegmentData=result.segments;
    const activity=activities.find(a=>a.id===id);
    if(activity) {
      activity.medals=result.segments.filter(s=>s.rank!=null && s.rank<=3).map(s=>({segment_id:s.id,name:s.name,rank:s.rank}));
      renderList();
    }
    renderTrackSegments();
    drawTrackSegments();
    let text=trackSegmentData.length ? "Medals compare each activity's best timed pass; ties share a rank. Checkpoints and traversed portions appear on the map." : "No completed segment matches for this activity.";
    if(result.pending_count) text+=" "+result.pending_count+" segment(s) are being processed; results refresh automatically.";
    if(result.failed_count) text+=" "+result.failed_count+" segment(s) failed matching; open Segments to retry.";
    $("#track-segment-status").textContent=text;
    requestAnimationFrame(()=>{if(maps.map) maps.map.invalidateSize({pan:false});});
    if(result.pending_count && !$("#tracks").hidden) trackSegmentPoll=setTimeout(()=>loadTrackSegments(id,version),2000);
  } catch(error) {
    if(version===trackVersion && request===trackSegmentRequest) $("#track-segment-status").textContent="Could not load segment results. "+error.message;
  }
}
function renderTrackSegments() {
  $("#track-segment-results").replaceChildren();
  for(const segment of trackSegmentData) {
    const card=document.createElement("article");card.className="track-segment-card";
    const medal=medalInfo(segment.rank), heading=document.createElement("div");heading.className="segment-result-heading";
    const name=document.createElement("button");name.className="segment-name";name.textContent=segment.name;
    name.onclick=()=>focusTrackSegment(segment.id);
    heading.append(name);
    if(medal) {
      const badge=document.createElement("span");badge.className="medal medal-"+medal.className;
      const artwork=document.createElement("img");artwork.src=medal.image;artwork.className="medal-art";artwork.alt="";artwork.width=38;artwork.height=44;
      badge.append(artwork,document.createTextNode(medal.label));badge.setAttribute("aria-label",medal.label+" place");
      heading.append(badge);
    }
    const info=document.createElement("p");info.className="small";
    const visits=segment.checkpoints.length===1;
    info.textContent=segment.traversals.length+(visits ? " visits" : " traversals")+(segment.rank!=null ?
      " · Best "+segmentTime(segment.best_duration_s)+" · Rank "+segment.rank+" of "+segment.ranked_activity_count+" activities" :
      visits ? " · Visits are not timed or ranked" : " · No timed pass available");
    const passes=document.createElement("details"), summary=document.createElement("summary");
    summary.textContent=visits ? "Show visits" : "Show traversal times";passes.append(summary);
    for(const traversal of segment.traversals) {
      const button=document.createElement("button");button.className="track-pass";
      button.textContent=(visits ? "Visit " : "Pass ")+traversal.ordinal+(visits ? "" : " · "+segmentTime(traversal.duration_s));
      button.onclick=()=>focusTrackSegment(segment.id,traversal.id);
      passes.append(button);
    }
    const zoom=document.createElement("button");zoom.className="segment-zoom";
    zoom.textContent="Show segment on map";zoom.onclick=()=>focusTrackSegment(segment.id);
    card.append(heading,info,zoom,passes);$("#track-segment-results").append(card);
  }
}
function ensureTrackSegmentPanes() {
  if(!maps.map) return;
  // Dedicated panes make ordering independent of which request completes first.
  for(const [name,zIndex] of [["track-segments",450],["track-segment-focus",460]]) {
    if(!maps.map.getPane(name)) maps.map.createPane(name).style.zIndex=String(zIndex);
  }
}
function drawTrackSegments() {
  if(!maps.map) return;
  ensureTrackSegmentPanes();
  if(!layers.trackSegments) layers.trackSegments=L.featureGroup().addTo(maps.map);
  if(!layers.trackSegmentFocus) layers.trackSegmentFocus=L.featureGroup().addTo(maps.map);
  layers.trackSegments.clearLayers();layers.trackSegmentFocus.clearLayers();
  if(!$("#track-show-segments").checked) return;
  for(const segment of trackSegmentData) {
    const medal=medalInfo(segment.rank),color=medal ? medal.color : "#d66c19";
    const label=()=>{const span=document.createElement("span");span.textContent=segment.name+(medal?" · "+medal.label:"");return span;};
    for(const checkpoint of segment.checkpoints) {
      L.circle(checkpoint,{pane:"track-segments",radius:segment.radius_m,color,weight:2,fillOpacity:0.1,interactive:false}).addTo(layers.trackSegments);
    }
    const icon=L.divIcon({
      className:medal ? "medal-map-icon" : "track-segment-icon",
      html:medal ? '<img src="'+medal.image+'" alt="'+medal.label+'" width="26" height="30">' : "S",
      iconSize:medal ? [26,30] : [28,28],iconAnchor:medal ? [13,25] : [14,14]
    });
    const marker=L.marker(segment.checkpoints[0],{icon}).addTo(layers.trackSegments);
    marker.bindTooltip(label(),{permanent:true,direction:"top",offset:[0,medal ? -25 : -14],className:"track-segment-label"});
    marker.on("click",()=>focusTrackSegment(segment.id));
    if(segment.checkpoints.length>1) {
      const endIcon=L.divIcon({className:"track-segment-icon track-segment-end",html:"F",iconSize:[24,24],iconAnchor:[12,12]});
      const end=L.marker(segment.checkpoints.at(-1),{icon:endIcon}).addTo(layers.trackSegments);
      end.bindTooltip(label());end.on("click",()=>focusTrackSegment(segment.id));
    }
    for(const traversal of segment.traversals) {
      if(traversal.points.length>1) L.polyline(traversal.points,{pane:"track-segments",color:"#fff",weight:10,opacity:0.9,interactive:false}).addTo(layers.trackSegments);
      const line=traversal.points.length===1 ? L.circleMarker(traversal.points[0],{pane:"track-segments",radius:7,color,fillOpacity:0.9}) :
        L.polyline(traversal.points,{pane:"track-segments",color,weight:6,opacity:1});
      line.addTo(layers.trackSegments).bindTooltip(label());
      line.on("click",()=>focusTrackSegment(segment.id,traversal.id));
    }
  }
}
function focusTrackSegment(id,traversalId=null) {
  const segment=trackSegmentData.find(s=>s.id===id);
  if(!segment || !maps.map) return;
  $("#track-show-segments").checked=true;drawTrackSegments();
  const traversals=traversalId ? segment.traversals.filter(t=>t.id===traversalId) : segment.traversals;
  const medal=medalInfo(segment.rank), color=medal ? medal.color : "#d66c19";
  for(const traversal of traversals) {
    if(traversal.points.length===1) L.circleMarker(traversal.points[0],{pane:"track-segment-focus",radius:10,color,fillOpacity:1}).addTo(layers.trackSegmentFocus);
    else {
      L.polyline(traversal.points,{pane:"track-segment-focus",color:"#fff",weight:12,opacity:1,interactive:false}).addTo(layers.trackSegmentFocus);
      L.polyline(traversal.points,{pane:"track-segment-focus",color,weight:8,opacity:1}).addTo(layers.trackSegmentFocus);
    }
  }
  const bounds=layers.trackSegmentFocus.getBounds();
  if(bounds.isValid()) {
    maps.map.invalidateSize();
    maps.map.fitBounds(bounds,{padding:[45,45],maxZoom:17});
    $("#map").scrollIntoView({block:"nearest",behavior:"smooth"});
  }
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
  if(window.routesUI && !window.routesUI.allowNavigate(view)) return false;
  if (window.segmentUI && !window.segmentUI.allowNavigate(view)) return false;
  if(window.routesUI && view!=="routes") window.routesUI.hide();
  if(window.destinationUI && view!=="destinations") window.destinationUI.hide();
  clearTimeout(trackSegmentPoll);
  if(window.routeMatchUI) window.routeMatchUI.hideExcept(view);
  document.querySelectorAll(".view").forEach(section=>section.hidden=section.id!==view);
  document.querySelectorAll("[data-view]").forEach(button=>{if(button.dataset.view===view) button.setAttribute("aria-current","page"); else button.removeAttribute("aria-current");});
  requestAnimationFrame(()=>{
    try {
      if (view==="heat") showHeat();
      if (view==="segments") window.segmentUI.show();
      if (view==="destinations") window.destinationUI.show();
      if (view==="routes") window.routesUI.show();
      if (view==="tracks") {
        getMap("map");
        if (!selected) { const first=activities.find(a=>a.has_track); if (first) showTrack(first.id); }
        else {
          loadTrackSegments(selected,trackVersion);
          if(window.routeMatchUI) window.routeMatchUI.show("track",selected,getMap("map"));
        }
      }
    } catch(error) { report(error); }
  });
  return true;
}
document.querySelectorAll("[data-view]").forEach(button=>button.onclick=()=>switchView(button.dataset.view));
$("#sport-filter").onchange=showHeat; $("#search").oninput=renderList;
$("#heat-mode").onchange=updateHeatStyle;
$("#heat-color").oninput=updateHeatStyle;
$("#track-show-segments").onchange=drawTrackSegments;
$("#track-color-speed").onchange=()=>{if($("#track-color-speed").checked) $("#track-color-temperature").checked=false;loadTrackSpeed();};
$("#track-color-temperature").onchange=()=>{if($("#track-color-temperature").checked) $("#track-color-speed").checked=false;loadTrackSpeed();};
$("#compare-unit").onchange=populateComparisonPeriods;
["#compare-sport","#compare-a","#compare-b"].forEach(selector=>$(selector).onchange=renderComparison);
getJSON("/api/activities").then(data=>{activities=data; summary(); renderList();}).catch(error=>{ $("#date-range").textContent="Activity data could not be loaded. Restart the server and refresh."; report(error); });
