import {api} from './client.js';
export const nodesAPI={list:()=>api('/nodes'),save:(data,id)=>api('/nodes'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify(data)})};
export const scenariosAPI={list:()=>api('/scenarios'),create:data=>api('/scenarios',{method:'POST',body:JSON.stringify(data)})};
export const drillsAPI={list:()=>api('/drills'),get:id=>api('/drills/'+id),create:data=>api('/drills',{method:'POST',body:JSON.stringify(data)})};
export const recoveryAPI={list:()=>api('/recovery/sessions'),get:id=>api('/recovery/sessions/'+id),create:name=>api('/recovery/sessions',{method:'POST',body:JSON.stringify({name})}),step:(id,action,params={})=>api('/recovery/sessions/'+id+'/steps',{method:'POST',body:JSON.stringify({action,params})})};
