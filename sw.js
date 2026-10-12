// 版本號：每次更新網頁就改這裡，iPhone 才會抓新版
const CACHE="etf-v1";
const SHELL=["./","./index.html","./manifest.webmanifest","./icons/apple-touch-icon.png","./icons/icon-192.png"];
self.addEventListener("install",e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)));self.skipWaiting()});
self.addEventListener("activate",e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));self.clients.claim()});
// 先抓網路（拿最新資料），失敗才用快取（離線時看上次的）
self.addEventListener("fetch",e=>{
  if(e.request.method!=="GET")return;
  e.respondWith(fetch(e.request).then(r=>{
    if(r.ok&&new URL(e.request.url).origin===location.origin){const c=r.clone();caches.open(CACHE).then(x=>x.put(e.request,c))}
    return r;
  }).catch(()=>caches.match(e.request).then(m=>m||caches.match("./index.html"))));
});
