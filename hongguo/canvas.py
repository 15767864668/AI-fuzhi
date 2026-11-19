"""Local-only infinite canvas for storyboard planning. No third-party dependencies."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BYTES = 2_000_000
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>无限画布</title><style>
*{box-sizing:border-box}body{margin:0;background:#101318;color:#e9edf4;font:14px system-ui,'Microsoft YaHei',sans-serif;overflow:hidden}
header{height:56px;display:flex;align-items:center;gap:8px;padding:0 16px;background:#191e27;border-bottom:1px solid #343a46}
button,input,textarea{font:inherit}button{background:#303847;color:#f5f5f5;border:1px solid #485161;border-radius:7px;padding:8px 12px;cursor:pointer}button:hover{background:#414d60}button:disabled{opacity:.4}
strong{margin-right:16px;font-size:17px}.accent{background:#f97015;border-color:#f97015;color:#171717}
#hint{margin-left:auto;color:#9ba8b8;font-size:12px}#viewport{position:absolute;top:56px;bottom:0;width:100%;overflow:hidden;cursor:grab;background-color:#11151b;background-image:radial-gradient(#353c49 1px,transparent 1px);background-size:24px 24px;background-position:0 0}
#viewport.panning{cursor:grabbing}#world{position:absolute;left:0;top:0;transform-origin:0 0;width:0;height:0}
#edges{position:absolute;left:0;top:0;overflow:visible;pointer-events:none}
.node{position:absolute;width:260px;min-height:135px;background:#242c38;border:1px solid #4a5668;border-radius:12px;box-shadow:0 8px 25px #0005;cursor:move;user-select:none}
.node.selected{border:2px solid #f97015}.node-head{padding:12px;background:#303a49;border-radius:11px 11px 0 0;display:flex;justify-content:space-between;gap:8px;font-weight:600}
.node-body{padding:12px;color:#bac6d5;white-space:pre-wrap;overflow-wrap:anywhere;max-height:150px;overflow:auto}
.port{width:14px;height:14px;border-radius:50%;background:#f97015;position:absolute;top:55px;border:2px solid #f7a268;cursor:crosshair}.port.in{left:-8px}.port.out{right:-8px}
#editor{position:absolute;right:18px;top:75px;width:300px;background:#1c2430;border:1px solid #465163;border-radius:12px;padding:16px;box-shadow:0 12px 40px #0009}
#editor[hidden]{display:none}#editor label{display:block;margin:12px 0 5px;color:#aeb9c9}#editor input,#editor textarea{width:100%;background:#111923;color:#fff;border:1px solid #485366;border-radius:6px;padding:8px}#editor textarea{height:120px;resize:vertical}
#notice{position:absolute;left:15px;bottom:15px;background:#242d3a;padding:9px 14px;border-radius:7px;color:#aeb9c9}svg path{stroke:#8d9db1;stroke-width:2.5;fill:none}svg path.active{stroke:#f97015;stroke-width:3.5}
@media(max-width:850px){#hint{display:none}header{overflow-x:auto}#editor{width:260px}}
</style></head><body>
<header><strong>∞ 无限画布</strong><button class="accent" id="add">＋ 分镜节点</button><button id="undo">撤销</button><button id="redo">重做</button><button id="fit">适应视图</button><button id="zin">＋</button><button id="zout">－</button><button id="save">保存</button><span id="hint">拖动空白平移 · 滚轮缩放 · 拖节点移动 · 从右侧橙点连到左侧橙点 · Delete 删除</span></header>
<div id="viewport"><div id="world"><svg id="edges" width="1" height="1"></svg><div id="nodes"></div></div></div>
<aside id="editor" hidden><b>编辑节点</b><label>标题</label><input id="nodeTitle" maxlength="120"><label>提示词 / 台词 / 备注</label><textarea id="nodeText" maxlength="10000"></textarea><p><button id="remove">删除节点</button><button id="close">关闭</button></p></aside>
<div id="notice">正在加载画布…</div>
<script>
(()=>{'use strict';
const $=id=>document.getElementById(id),vp=$('viewport'),world=$('world'),nodes=$('nodes'),svg=$('edges');
let data={nodes:[],edges:[],view:{x:Math.round(innerWidth/2-130),y:120,scale:1}},selected=null,connecting=null,drag=null,history=[],future=[],dirty=false,saveTimer=null;
const uid=()=>crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+Math.random().toString(36).slice(2);
const clamp=(v,a,b)=>Math.min(b,Math.max(a,v));
const message=t=>$('notice').textContent=t;
const snapshot=()=>JSON.stringify(data);
function record(){history.push(snapshot());if(history.length>80)history.shift();future=[]}
function change(){dirty=true;clearTimeout(saveTimer);saveTimer=setTimeout(save,900);render()}
function restore(s){data=JSON.parse(s);selected=null;connecting=null;change()}
function undo(){if(!history.length)return;future.push(snapshot());restore(history.pop())}
function redo(){if(!future.length)return;history.push(snapshot());restore(future.pop())}
function view(){world.style.transform='translate('+data.view.x+'px,'+data.view.y+'px) scale('+data.view.scale+')';vp.style.backgroundPosition=data.view.x+'px '+data.view.y+'px';vp.style.backgroundSize=(24*data.view.scale)+'px '+(24*data.view.scale)+'px'}
function edgePath(a,b){const x1=a.x+260,y1=a.y+55,x2=b.x,y2=b.y+55,k=Math.max(70,Math.abs(x2-x1)*.5);return 'M '+x1+' '+y1+' C '+(x1+k)+' '+y1+' '+(x2-k)+' '+y2+' '+x2+' '+y2}
function render(){view();nodes.replaceChildren();svg.replaceChildren();for(const e of data.edges){const a=data.nodes.find(n=>n.id===e.from),b=data.nodes.find(n=>n.id===e.to);if(!a||!b)continue;const p=document.createElementNS('http://www.w3.org/2000/svg','path');p.setAttribute('d',edgePath(a,b));svg.append(p)}
for(const n of data.nodes){const el=document.createElement('div');el.className='node'+(n.id===selected?' selected':'');el.style.left=n.x+'px';el.style.top=n.y+'px';el.dataset.id=n.id;
const h=document.createElement('div');h.className='node-head';const name=document.createElement('span');name.textContent=n.title;const edit=document.createElement('button');edit.textContent='编辑';edit.style.padding='2px 8px';edit.onclick=ev=>{ev.stopPropagation();select(n.id)};h.append(name,edit);
const body=document.createElement('div');body.className='node-body';body.textContent=n.text||'点击编辑，填写镜头描述…';
const pin=document.createElement('div');pin.className='port in';pin.title='连接终点';pin.onclick=ev=>{ev.stopPropagation();if(connecting&&connecting!==n.id&&!data.edges.some(e=>e.from===connecting&&e.to===n.id)){record();data.edges.push({from:connecting,to:n.id});change()}connecting=null;message('已更新连接')};
const pout=document.createElement('div');pout.className='port out';pout.title='连接起点';pout.onclick=ev=>{ev.stopPropagation();connecting=n.id;message('请点击目标节点左侧橙点完成连接')};
el.append(h,body,pin,pout);el.addEventListener('pointerdown',ev=>{if(ev.target.closest('button,.port'))return;ev.stopPropagation();selected=n.id;drag={kind:'node',id:n.id,startX:ev.clientX,startY:ev.clientY,x:n.x,y:n.y,moved:false};el.setPointerCapture(ev.pointerId);renderSelection();});
el.addEventListener('pointermove',ev=>{if(!drag||drag.kind!=='node'||drag.id!==n.id)return;const dx=(ev.clientX-drag.startX)/data.view.scale,dy=(ev.clientY-drag.startY)/data.view.scale;if(!drag.moved&&(Math.abs(dx)+Math.abs(dy)>2)){record();drag.moved=true}if(drag.moved){n.x=drag.x+dx;n.y=drag.y+dy;el.style.left=n.x+'px';el.style.top=n.y+'px';drawEdges()}});
el.addEventListener('pointerup',()=>{if(drag?.moved)change();drag=null});el.ondblclick=()=>select(n.id);nodes.append(el)}
renderSelection()}
function drawEdges(){svg.replaceChildren();for(const e of data.edges){const a=data.nodes.find(n=>n.id===e.from),b=data.nodes.find(n=>n.id===e.to);if(a&&b){const p=document.createElementNS('http://www.w3.org/2000/svg','path');p.setAttribute('d',edgePath(a,b));svg.append(p)}}}
function renderSelection(){nodes.querySelectorAll('.node').forEach(e=>e.classList.toggle('selected',e.dataset.id===selected));const n=data.nodes.find(n=>n.id===selected);$('editor').hidden=!n;if(n){if(document.activeElement!==$('nodeTitle'))$('nodeTitle').value=n.title;if(document.activeElement!==$('nodeText'))$('nodeText').value=n.text}}
function select(id){selected=id;renderSelection()}
function add(){record();const id=uid(),n={id,x:Math.round((vp.clientWidth/2-data.view.x)/data.view.scale-130),y:Math.round((vp.clientHeight/2-data.view.y)/data.view.scale-65),title:'分镜 '+(data.nodes.length+1),text:''};data.nodes.push(n);selected=id;change()}
function remove(){if(!selected)return;record();data.nodes=data.nodes.filter(n=>n.id!==selected);data.edges=data.edges.filter(e=>e.from!==selected&&e.to!==selected);selected=null;change()}
function zoom(f,cx=vp.clientWidth/2,cy=vp.clientHeight/2){const v=data.view,s=clamp(v.scale*f,.15,3);v.x=cx-(cx-v.x)*s/v.scale;v.y=cy-(cy-v.y)*s/v.scale;v.scale=s;change()}
vp.addEventListener('wheel',ev=>{ev.preventDefault();const r=vp.getBoundingClientRect();zoom(ev.deltaY<0?1.12:1/1.12,ev.clientX-r.left,ev.clientY-r.top)},{passive:false});
vp.addEventListener('pointerdown',ev=>{if(ev.target!==vp&&ev.target!==world&&ev.target!==nodes&&ev.target!==svg)return;drag={kind:'pan',x:ev.clientX,y:ev.clientY,vx:data.view.x,vy:data.view.y};vp.classList.add('panning');vp.setPointerCapture(ev.pointerId);selected=null;renderSelection()});
vp.addEventListener('pointermove',ev=>{if(drag?.kind!=='pan')return;data.view.x=drag.vx+ev.clientX-drag.x;data.view.y=drag.vy+ev.clientY-drag.y;view()});
vp.addEventListener('pointerup',()=>{if(drag?.kind==='pan')change();drag=null;vp.classList.remove('panning')});
$('add').onclick=add;$('remove').onclick=remove;$('close').onclick=()=>{selected=null;renderSelection()};
$('undo').onclick=undo;$('redo').onclick=redo;$('zin').onclick=()=>zoom(1.2);$('zout').onclick=()=>zoom(1/1.2);
$('fit').onclick=()=>{if(!data.nodes.length){data.view={x:vp.clientWidth/2-130,y:100,scale:1}}else{const xs=data.nodes.map(n=>n.x),ys=data.nodes.map(n=>n.y),minX=Math.min(...xs),maxX=Math.max(...xs)+260,minY=Math.min(...ys),maxY=Math.max(...ys)+170,s=clamp(Math.min((vp.clientWidth-100)/(maxX-minX),(vp.clientHeight-100)/(maxY-minY)),.15,1.5);data.view={scale:s,x:(vp.clientWidth-(minX+maxX)*s)/2,y:(vp.clientHeight-(minY+maxY)*s)/2}}change()};
$('nodeTitle').oninput=ev=>{const n=data.nodes.find(n=>n.id===selected);if(n){n.title=ev.target.value;change()}};
$('nodeText').oninput=ev=>{const n=data.nodes.find(n=>n.id===selected);if(n){n.text=ev.target.value;change()}};
document.addEventListener('keydown',ev=>{if(['INPUT','TEXTAREA'].includes(document.activeElement.tagName))return;if(ev.key==='Delete'||ev.key==='Backspace')remove();if((ev.ctrlKey||ev.metaKey)&&ev.key.toLowerCase()==='z'){ev.preventDefault();ev.shiftKey?redo():undo()}});
async function save(){try{const res=await fetch('/api/canvas',{method:'PUT',headers:{'Content-Type':'application/json'},body:snapshot()});if(!res.ok)throw Error('HTTP '+res.status);dirty=false;message('画布已自动保存')}catch(e){message('保存失败：'+e.message)}}$('save').onclick=save;
async function load(){try{const r=await fetch('/api/canvas');if(!r.ok)throw Error('HTTP '+r.status);const obj=await r.json();if(obj&&Array.isArray(obj.nodes)&&Array.isArray(obj.edges)&&obj.view)data=obj;render();message('就绪 · 节点 '+data.nodes.length+' 个')}catch(e){message('加载失败：'+e.message)}}load();
window.addEventListener('beforeunload',ev=>{if(dirty){ev.preventDefault();ev.returnValue=''}})
})();
</script></body></html>"""


def start_canvas_server(data_dir: Path, port: int = 8766) -> ThreadingHTTPServer:
    """Bind to loopback only; raises OSError if port is occupied."""
    root = Path(data_dir).resolve() / "canvas"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "default.json"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                payload = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif self.path == "/api/canvas":
                payload = path.read_bytes() if path.exists() else b'{"nodes":[],"edges":[],"view":{"x":320,"y":120,"scale":1}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            else:
                self.send_error(404)
                return
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_PUT(self):
            if self.path != "/api/canvas":
                self.send_error(404)
                return
            # Reject cross-origin writes and excessively large requests.
            origin = self.headers.get("Origin", "")
            if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > MAX_BYTES:
                    self.send_error(413)
                    return
                obj = json.loads(self.rfile.read(length))
                if not isinstance(obj, dict) or not isinstance(obj.get("nodes"), list) or not isinstance(obj.get("edges"), list) or not isinstance(obj.get("view"), dict):
                    raise ValueError("invalid canvas document")
                if len(obj["nodes"]) > 1500 or len(obj["edges"]) > 5000:
                    raise ValueError("canvas limit exceeded")
                ids = set()
                for n in obj["nodes"]:
                    if not isinstance(n, dict) or not isinstance(n.get("id"), str) or not SAFE_ID.fullmatch(n["id"]) or n["id"] in ids:
                        raise ValueError("invalid node id")
                    ids.add(n["id"])
                    if not isinstance(n.get("title"), str) or len(n["title"]) > 120 or not isinstance(n.get("text"), str) or len(n["text"]) > 10000:
                        raise ValueError("invalid node content")
                    for k in ("x", "y"):
                        if not isinstance(n.get(k), (int, float)) or abs(n[k]) > 1e7:
                            raise ValueError("invalid node position")
                for e in obj["edges"]:
                    if not isinstance(e, dict) or e.get("from") not in ids or e.get("to") not in ids:
                        raise ValueError("invalid edge")
                for k in ("x", "y", "scale"):
                    v = obj["view"].get(k)
                    if not isinstance(v, (int, float)) or not -1e7 <= v <= 1e7:
                        raise ValueError("invalid view")
                if not .15 <= obj["view"]["scale"] <= 3:
                    raise ValueError("invalid zoom")
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
                temp.replace(path)
            except (ValueError, TypeError, json.JSONDecodeError):
                self.send_error(400)
                return
            self.send_response(204)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="infinite-canvas").start()
    return server
