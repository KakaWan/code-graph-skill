#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
code-graph verify_layout.py - static-layout verification harness.

Injects a probe after `switchView("modules");` in the generated HTML, runs
headless Edge --dump-dom, and reports per-view layout statistics from the
document title (the probe writes __VRS__...__VRE__ there):
  nodes / minD (closest pair distance) / ov (pairs < 26px) /
  in1 (sole-referrer nodes) / within15 (of those, aligned to their referrer
  within 15 degrees, origin-angle approximation).

Usage: python3 verify_layout.py --html <path> [--edge <path>]
"""
import argparse
import re
import subprocess
import sys
import tempfile
import os

PROBE = """
switchView("modules");
try{var __har=function(){
  var VRS="__VRS__\\n";
  var views=["modules","files","classes","functions"];
  var dist2=function(a,b){var dx=a.x-b.x,dy=a.y-b.y;return dx*dx+dy*dy;};
  // Recompute islands the same way initPositions does: connected components
  // (undirected) of linked nodes + hub stars peeled from the main component.
  // Must run per-view (state.links differs per view), so it's a function.
  var recalcIslands=function(){
    var pos=state.poses[state.view];
    var inD2={},outD2={},adjm={},compOf={},comps=[],cn=0;
    state.links.forEach(function(l){inD2[l.t]=(inD2[l.t]||0)+1;outD2[l.s]=(outD2[l.s]||0)+1;});
    state.nodes.forEach(function(nd){adjm[nd.id]=[];});
    state.links.forEach(function(l){
      if(adjm[l.s]){ adjm[l.s].push(l.t); if(adjm[l.t])adjm[l.t].push(l.s); }
    });
    state.nodes.forEach(function(nd){
      var id=nd.id;
      if(!((inD2[id]||0)+(outD2[id]||0))||compOf[id]!==undefined)return;
      var st=[id],comp=[]; compOf[id]=cn;
      while(st.length){
        var x=st.pop(); comp.push(x);
        (adjm[x]||[]).forEach(function(y){
          if(compOf[y]===undefined&&((inD2[y]||0)+(outD2[y]||0))){ compOf[y]=cn; st.push(y); }
        });
      }
      comps.push(comp); cn++;
    });
    var mainIdx=0;
    comps.forEach(function(c,i){ if(c.length>comps[mainIdx].length)mainIdx=i; });
    var udegf=function(x){ return (adjm[x]||[]).length; };
    var stars=[],starSet={};
    (comps[mainIdx]||[]).forEach(function(h){
      if(udegf(h)<4)return;
      var leaves=(adjm[h]||[]).filter(function(l){
        return udegf(l)<=2&&(adjm[l]||[]).every(function(x){return x===h||udegf(x)<=2;});
      });
      if(leaves.length>=3&&udegf(h)-leaves.length<=3)stars.push({hub:h,leaves:leaves});
    });
    stars.forEach(function(s){ starSet[s.hub]=1; s.leaves.forEach(function(l){starSet[l]=1;}); });
    var islandN=stars.length;
    comps.forEach(function(c,i){ if(i!==mainIdx&&c.length>=2)islandN++; });
    var isoN2=0, leafN=0, leafDmax=0, leafProbs=[];
    window.__leafSet={}; window.__isoSet={}; window.__islandSet={}; window.__starSet={};
    state.nodes.forEach(function(nd){
      var id=nd.id;
      var d0=(inD2[id]||0)+(outD2[id]||0);
      if(!d0){ isoN2++; window.__isoSet[id]=1; return; }
      if(d0!==1)return;
      var nbrs=adjm[id]||[];
      if(nbrs.length!==1)return;
      leafN++; window.__leafSet[id]=1;
      var a=pos.get(id), b=pos.get(nbrs[0]);
      if(a&&b){
        var dx=a.x-b.x, dy=a.y-b.y;
        var d=Math.sqrt(dx*dx+dy*dy);
        if(d>leafDmax)leafDmax=d;
        if(d<10)leafProbs.push(id+"@"+nbrs[0]);
      } else {
        leafProbs.push(id+"@NO-POS");
      }
    });
    var mainComp0=(comps[mainIdx]||[]).forEach(function(m){ if(!starSet[m])window.__islandSet[m]=1; });
    stars.forEach(function(s){ window.__starSet[s.hub]=1; s.leaves.forEach(function(l){window.__starSet[l]=1;}); });
    comps.forEach(function(c,i){ if(i!==mainIdx) c.forEach(function(x){window.__islandSet[x]=1;}); });
    window.__iso=isoN2; window.__island=islandN; window.__stars=stars.length;
    window.__hubN=stars.length?stars.map(function(s){return s.hub;}).join("|"):"-";
    window.__leafN=leafN; window.__leafD=Math.round(leafDmax);
    window.__leafProbs=leafProbs.join(",");
  };
  var catOf=function(id){
    if(window.__isoSet[id])return"iso";
    if(window.__leafSet[id])return"leaf";
    if(window.__starSet[id])return"star";
    if(window.__islandSet[id])return"island";
    return"ring";
  };
  views.forEach(function(v){
    switchView(v);
    recalcIslands();
    // Run the physics pass synchronously to convergence (the static seed
    // may still have crowding; hardSep converges it), then measure.
    while (state.frame < maxFrame() && state.running) { step(); state.frame++; }
    state.running=false;
    var pos=state.poses[v];
    var ids=state.nodes.map(function(nd){return nd.id;});
    var minD=1e18,ov=0,close5=[];
    var ovCat={"leaf-leaf":0,"leaf-ring":0,"leaf-island":0,"leaf-star":0,
               "ring-ring":0,"ring-island":0,"ring-star":0,"ring-iso":0,
               "island-island":0,"star-star":0,"star-island":0,"iso-*":0};
    var pushClose=function(d,ia,ib){
      var e=[Math.round(d),catOf(ia)+":"+(ia.length>26?ia.slice(-20):ia),
             catOf(ib)+":"+(ib.length>26?ib.slice(-20):ib)];
      close5.push(e); close5.sort(function(x,y){return x[0]-y[0];});
      if(close5.length>5)close5.length=5;
    };
    for(var i=0;i<ids.length;i++)for(var j=i+1;j<ids.length;j++){
      var a=pos.get(ids[i]),b=pos.get(ids[j]);
      if(!a||!b)continue;
      var d2=dist2(a,b);
      if(d2<minD){ minD=d2; }
      if(d2<26*26){
        ov++;
        var ca=catOf(ids[i]),cb=catOf(ids[j]);
        var k=(ca===cb)?(ca+"-"+ca):(ca<cb?ca+'-'+cb:cb+'-'+ca);
        if(ovCat[k]===undefined)k=(ca==='iso'||cb==='iso')?"iso-*":("?"+k);
        ovCat[k]=(ovCat[k]||0)+1;
        if(close5.length<5||d2<close5[4][0]*close5[4][0])pushClose(Math.sqrt(d2),ids[i],ids[j]);
      }
    }
    var ovStr="";
    Object.keys(ovCat).forEach(function(k){ if(ovCat[k])ovStr+=k+"="+ovCat[k]+" "; });
    var inDeg={},outDeg={},refers={};
    state.links.forEach(function(l){
      inDeg[l.t]=(inDeg[l.t]||0)+1;
      outDeg[l.s]=(outDeg[l.s]||0)+1;
      if(inDeg[l.t]===1)refers[l.t]=l.s;
    });
    // iso correctness: every no-link node must sit in the pile BELOW the
    // ring (max ring y < min pile y); every linked node stays in the ring.
    var ringMaxY=-1e9, pileMinY=1e9, noLinkN=0;
    ids.forEach(function(id){
      var p=pos.get(id); if(!p)return;
      var d0=(inDeg[id]||0)+(outDeg[id]||0);
      if(d0>0){ if(p.y>ringMaxY)ringMaxY=p.y; }
      else { noLinkN++; if(p.y<pileMinY)pileMinY=p.y; }
    });
    var separated=(noLinkN===0||pileMinY>ringMaxY)?"yes":"NO";
    var in1=0,within15=0;
    Object.keys(refers).forEach(function(id){
      var a=pos.get(id),b=pos.get(refers[id]);
      if(!a||!b)return;
      in1++;
      var da=Math.atan2(a.y,a.x),db=Math.atan2(b.y,b.x);
      var d=Math.abs(da-db);if(d>Math.PI)d=2*Math.PI-d;
      if(d<=15*Math.PI/180)within15++;
    });
    VRS+="view="+v+" nodes="+state.nodes.length+" minD="+(Math.sqrt(minD)).toFixed(1)+" ov="+ov
      +" in1="+in1+" within15="+within15+" noLink="+noLinkN+" sep="+separated
      +" iso="+window.__iso+" islands="+window.__island+" stars="+window.__stars
      +" hubs="+window.__hubN+" leaf="+window.__leafN+" leafD="+window.__leafD
      +" leafProbs=["+window.__leafProbs+"] ovByCat=("+ovStr+")"
      +" close5="+close5.map(function(e){return e[0]+":"+e[1]+"~"+e[2];}).join("|")+"\\n";
  });
  // Direction-alignment test: after 40 spring frames, is each node's
  // displacement roughly parallel to one of its edges? (The "没有沿着线"
  // complaint — springs must tighten ALONG the edges, not collapse clusters
  // radially.) An edge direction is taken from the pre-spring snapshot.
  var stPose=state.poses["functions"];
  var ids2=state.nodes.map(function(nd){return nd.id;});
  var p0=new Map();
  ids2.forEach(function(id){var q=stPose.get(id); p0.set(id,q?{x:q.x,y:q.y}:null);});
  state.springs=true; state.running=true; state.frame=0;
  var fr=0; while(fr<40 && state.running){step(); state.frame++; fr++;}
  var moved=0, along=0;
  ids2.forEach(function(id){
    var a=stPose.get(id), s=p0.get(id);
    if(!a||!s)return;
    var dx=a.x-s.x, dy=a.y-s.y;
    var dl=Math.sqrt(dx*dx+dy*dy);
    if(dl<3)return;   // barely moved — ignore
    moved++;
    var best=0;
    state.links.forEach(function(l){
      var nb = l.s===id ? l.t : (l.t===id ? l.s : null);
      if(!nb)return;
      var bp=p0.get(nb); if(!bp)return;
      var ex=bp.x-s.x, ey=bp.y-s.y;
      var el=Math.sqrt(ex*ex+ey*ey); if(!el)return;
      var c=(dx*ex+dy*ey)/(dl*el);
      if(c>best)best=c;
    });
    if(best>0.7)along++;
  });
  var dirPct = moved ? Math.round(100*along/moved) : 0;
  // Region-local spring test: full run to convergence, then measure.
  while(fr<maxFrame() && state.running){step(); state.frame++; fr++;}
  state.running=false; state.springs=false;
  var minD2=1e18,ov2=0,dispT=0,ovCat2={};
  for(var i2=0;i2<ids2.length;i2++)for(var j2=i2+1;j2<ids2.length;j2++){
    var a2=stPose.get(ids2[i2]),b2=stPose.get(ids2[j2]);
    if(!a2||!b2)continue;
    var d22=dist2(a2,b2);
    if(d22<minD2)minD2=d22;
    if(d22<26*26){
      ov2++;
      var ca2=catOf(ids2[i2]),cb2=catOf(ids2[j2]);
      var k2=(ca2===cb2)?ca2+"-"+ca2:(ca2<cb2?ca2+'-'+cb2:cb2+'-'+ca2);
      ovCat2[k2]=(ovCat2[k2]||0)+1;
    }
  }
  var ovStr2="";
  Object.keys(ovCat2).forEach(function(k){ if(ovCat2[k])ovStr2+=k+"="+ovCat2[k]+" "; });
  // node displacement vs the pre-spring snapshot: bounded means regions
  // stayed put (springs only tighten inside regions, no cross-region yank).
  ids2.forEach(function(id){
    var a=stPose.get(id),b=p0.get(id);
    if(!a||!b)return;
    var dx=a.x-b.x,dy=a.y-b.y; dispT+=Math.sqrt(dx*dx+dy*dy);
  });
  VRS+="springsTest frames="+fr+" ov="+ov2+" minD="+(Math.sqrt(minD2)).toFixed(1)
    +" avgDisp="+(dispT/ids2.length).toFixed(1)+" ovByCat2=("+ovStr2+")"
    +" dirTest moved="+moved+" along="+along+" ("+dirPct+"%)"+"\\n";
  VRS+="__VRE__";
  document.title=VRS;
  switchView("classes");
}();}catch(e){document.title="__VRS__ERR:"+e.message+"__VRE__";}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", required=True)
    ap.add_argument("--edge", default=None)
    ap.add_argument("--shot", default=None,
                    help="optional PNG path: also capture a headless screenshot "
                         "(the probe leaves the last view, functions, on screen)")
    args = ap.parse_args()

    with open(args.html, "r", encoding="utf-8") as f:
        html = f.read()

    marker = 'switchView("modules");'
    if marker not in html:
        print("error: marker not found in html", file=sys.stderr)
        sys.exit(2)
    html = html.replace(marker, marker + PROBE)

    fd, tmp = tempfile.mkstemp(suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)

    edge = args.edge
    if not edge:
        for cand in (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ):
            if os.path.exists(cand):
                edge = cand
                break
    if not edge:
        print("error: edge not found; pass --edge", file=sys.stderr)
        sys.exit(2)

    url = "file:///" + tmp.replace("\\", "/")
    cmd = [edge, "--headless", "--disable-gpu", "--dump-dom",
           "--window-size=2200,1600", "--virtual-time-budget=9000"]
    if args.shot:
        cmd += ["--screenshot=" + os.path.abspath(args.shot).replace("\\", "/")]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    os.unlink(tmp)
    dom = proc.stdout.decode("utf-8", errors="replace")
    m = re.search(r"__VRS__(.*?)__VRE__", dom, re.S)
    if not m:
        m2 = re.search(r"<title>(.*?)</title>", dom, re.S)
        print("no probe output; page title:", m2.group(1) if m2 else "(none)")
        sys.exit(1)
    print(m.group(1).strip())


if __name__ == "__main__":
    sys.exit(main())
