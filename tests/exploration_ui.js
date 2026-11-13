// Exercise asynchronous track switching and overlay controls without network or a browser.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const nodes=new Map(),requests=[],panes=new Map(),overlays=[];
const node=selector=>{
  if(!nodes.has(selector))nodes.set(selector,{textContent:'',checked:false,hidden:false,replaceChildren(){}});
  return nodes.get(selector);
};
const map={getPane:name=>panes.get(name),createPane(name){const pane={style:{}};panes.set(name,pane);return pane;}};
const context=vm.createContext({console,
  document:{querySelector:node,querySelectorAll:()=>[]},window:{},
  fetch:url=>new Promise(resolve=>requests.push({url,resolve})),
  L:{featureGroup(){const group={parts:[],removed:false,addTo(){overlays.push(group);return group;},remove(){group.removed=true;}};return group;},
    geoJSON(geometry,options){return {addTo(group){group.parts.push({geometry,options});}};}},
});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8'),context);
vm.runInContext('maps.map=testMap',Object.assign(context,{testMap:map}));
const geometry={type:'FeatureCollection',features:[{type:'Feature',properties:{},geometry:{type:'LineString',coordinates:[[11,48],[11.001,48]]}}]};
const result=(distance,includeGeometry=false)=>({status:'available',message:'Estimated paths.',new_m:distance,
  matched_unique_m:1000,new_percent:distance/10,history_complete:true,coverage_complete:true,
  ...(includeGeometry?{geometry}:{}),missing_history_matches:0,partial_history_activities:0,
  undated_history_activities:0,outside_coverage:false,unmatched_points:0});
function respond(fragment,data){
  const index=requests.findIndex(request=>request.url.includes(fragment));
  assert.notEqual(index,-1,'Expected a local exploration request');
  requests.splice(index,1)[0].resolve({ok:true,json:async()=>data});
}
async function main(){
  // A slow old track must not replace a newer selection's statistics.
  vm.runInContext("selected='old';trackVersion=1",context);
  const old=vm.runInContext("loadTrackExploration('old',1)",context);
  vm.runInContext("selected='new';trackVersion=2",context);
  const current=vm.runInContext("loadTrackExploration('new',2)",context);
  respond('/new/',result(500));await current;
  const summary=node('#track-exploration-summary').textContent;
  respond('/old/',result(900));await old;
  assert.equal(node('#track-exploration-summary').textContent,summary);
  assert.match(summary,/50%/);
  assert.equal(overlays.length,0,'Highlight starts off');

  // Geometry is requested only when enabled; toggling leaves other map layers alone.
  node('#track-show-new-paths').checked=true;
  const highlighted=vm.runInContext("loadTrackExploration('new',2)",context);
  assert.ok(requests.some(request=>request.url.includes('geometry=1')));
  respond('/new/',result(500,true));await highlighted;
  assert.equal(overlays.length,1);
  assert.equal(overlays[0].parts.length,2,'White outline and magenta path');
  assert.equal(overlays[0].parts[1].options.style.color,'#d000c8');
  assert.equal(panes.get('track-exploration').style.zIndex,430,'Below the segment panes');
  node('#track-show-new-paths').checked=false;
  node('#track-show-new-paths').onchange();
  assert.equal(overlays[0].removed,true);

  // A geometry response arriving after the toggle is disabled must stay hidden.
  node('#track-show-new-paths').checked=true;
  const delayed=vm.runInContext("loadTrackExploration('new',2)",context);
  node('#track-show-new-paths').checked=false;
  node('#track-show-new-paths').onchange();
  respond('/new/',result(500,true));await delayed;
  assert.equal(overlays.length,1);
  console.log('Passed: exploration selection races, lazy geometry, overlay layering, and toggle races.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
