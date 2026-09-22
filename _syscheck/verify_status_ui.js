/* 爬虫状态页那两个新态(冷却中 / 正在使用), 前端这一半不进浏览器也验一遍。
 *
 * 为什么不进浏览器: 这台机器前台有活, 起浏览器(含无头)会撞上去 —— 见 user-habits。
 * 那就换个不吃进程的验法: 状态判定那段本来就要能离线喂数据(故写成了纯函数 crawlerState,
 * 不碰 DOM), 抠出来在 node 里断言"什么数据 -> 画什么灯"; 真正要 DOM 的(倒计时每秒走字、
 * 点开详情窗)交给用户自己点。
 *
 * 跑：node _syscheck/verify_status_ui.js
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "web", "index.html"), "utf8");

let FAIL = 0;
function ok(cond, label, extra) {
  console.log("  [" + (cond ? "ok" : "FAIL") + "] " + label + (extra ? "  " + extra : ""));
  if (!cond) FAIL++;
}

/* 从整个 <script> 里抠出一个具名函数(按大括号配平, 够用且不引 dependon) */
function grab(name) {
  const at = html.indexOf("function " + name + "(");
  if (at < 0) throw new Error("找不到函数 " + name);
  let i = html.indexOf("{", at), depth = 0, j = i;
  for (; j < html.length; j++) {
    if (html[j] === "{") depth++;
    else if (html[j] === "}") { depth--; if (depth === 0) { j++; break; } }
  }
  return html.slice(at, j);
}

const SRC = ["lampCls", "lampText", "worst", "lampBrief", "coolText", "crawlerState"]
  .map(grab).join("\n");
const mod = new Function(SRC + "\nreturn {lampCls,lampText,worst,lampBrief,coolText,crawlerState};")();
const { coolText, crawlerState } = mod;

/* 后端 /api/status 真实给出来的形状(照着 webapp.status_payload() 那份) */
function crawler(over) {
  return Object.assign({ key: "bili", name: "bilibili 爬虫", ok: true, login: true,
                         busy: false, queries_done: 7, error: null, cool_left: 0,
                         running: true }, over || {});
}

console.log("=== 1. 老四态一个都没动 ===");
let st = crawlerState(crawler());
ok(st.w === "ok" && st.brief === "正常", "都好 -> 绿灯「正常」");
st = crawlerState(crawler({ running: false }));
ok(st.w === "off" && st.brief === "未启动", "爬虫没起 -> 灰灯「未启动」(静止态, 不是故障)");
st = crawlerState(crawler({ login: false }));
ok(st.w === "bad" && st.brief === "登录态失效", "登录态坏了 -> 红灯「登录态失效」");
st = crawlerState(crawler({ ok: false }));
ok(st.w === "bad" && st.brief === "服务不可达", "服务连不上 -> 红灯「服务不可达」");
st = crawlerState(crawler({ login: "unknown" }));
ok(st.w === "warn" && st.brief === "待校验", "登录态没测过 -> 琥珀「待校验」");

console.log("=== 2. 新态一：正在使用(有题在爬它) ===");
st = crawlerState(crawler({ busy: true }));
ok(st.w === "busy" && st.brief === "正在使用", "busy=true -> 蓝灯「正在使用」");
ok(st.busy === true, "busy 透出来(详情窗要拿它决定按钮)");
st = crawlerState(crawler({ busy: true, login: false }));
ok(st.w === "busy", "正在爬的时候, 登录态那盏红灯先让位(它此刻正被用着, 报「不可用」是错的)");
st = crawlerState(crawler({ busy: null }));
ok(st.w === "ok", "busy=null(没数据)不当作在用");

console.log("=== 3. 新态二：冷却中(风控退避, 带倒计时) ===");
st = crawlerState(crawler({ key: "bili", cool_left: 125 }));
ok(st.w === "cool" && st.brief === "冷却中", "cool_left>0 -> 琥珀「冷却中」");
ok(st.cool === 125, "秒数透出来(页面拿它走倒计时)", String(st.cool));
ok(coolText(125) === "2:05", "倒计时写法 m:ss", coolText(125));
ok(coolText(60) === "1:00", "整分也对", coolText(60));
ok(coolText(9) === "0:09", "个位数补零", coolText(9));
ok(coolText(0) === "0:00", "归零就是 0:00");
ok(coolText(0.4) === "0:01", "小数进一位: 没到点不写 0:00(免得人以为已经好了)", coolText(0.4));
st = crawlerState(crawler({ cool_left: 0 }));
ok(st.w === "ok" && st.cool === 0, "cool_left=0 -> 不画冷却(不报「冷却中 0:00」那种废话)");
st = crawlerState(crawler({ cool_left: null }));
ok(st.cool === 0, "cool_left=null(老后端不给这字段) -> 退化成没有冷却, 不炸");

console.log("=== 4. 冷却盖过别的态(它信息最多: 带倒计时) ===");
st = crawlerState(crawler({ cool_left: 300, busy: true }));
ok(st.w === "cool", "又冷却又在用 -> 报冷却");
st = crawlerState(crawler({ cool_left: 300, login: false }));
ok(st.w === "cool" && st.brief === "冷却中", "又冷却又登录态坏 -> 先报冷却");
st = crawlerState(crawler({ cool_left: 300, running: false }));
ok(st.w === "cool", "服务没起但账上还在冷却 -> 照报冷却(冷却是引擎的账, 与服务在不在跑无关)");

console.log("=== 5. 页面上那几处接线还在 ===");
ok(/class="dot '\+w\+'"/.test(html) || html.indexOf('class="dot "+w') >= 0
   || html.indexOf("dot '+w+'") >= 0, "侧栏那行照旧按判定结果画点");
ok(html.indexOf('data-cool="') >= 0, "倒计时那个数挂着 data-cool(每秒走字就找它)");
ok(html.indexOf("coolAt[") >= 0, "记了本地的冷却截止时刻(倒计时自己 tick, 不吃后端)");
ok(/setInterval\(function\(\)\{[\s\S]{0,400}data-cool/.test(html), "有每秒走倒计时的定时器");
ok(/if\(stale\) refreshStatus\(\)/.test(html), "倒计时走完不是自己判定「好了」, 而是去问后端要真值");
ok(/stale=true; delete coolAt\[k\]/.test(html),
   "走完就把这条账销掉 —— 否则后端连不上时, 那盏灯会每秒空问一次后端");
ok(/if\(!\(k in coolAt\)\) return;/.test(html), "销过账的灯不再重复倒计时(不会一路跌成负数)");
ok(html.indexOf("/api/status?session=") >= 0, "刷状态带上了会话号(冷却是按会话的)");
ok(html.indexOf("refreshStatus();          // 每题跑着的时候") >= 0
   || /pollJob[\s\S]{0,900}refreshStatus\(\)/.test(html), "每题轮询时顺手刷灯(「正在使用」才看得见)");
ok(html.indexOf(".dot.busy{") >= 0 && html.indexOf(".dot.cool{") >= 0, "两种新态各有自己的灯色");
ok(html.indexOf("正在使用，等它空下来") >= 0, "正在爬的时候不给「去修复」按钮(点下去只会回 429)");
ok(html.indexOf("var btn = st.cool") >= 0 && html.indexOf("后可用") > html.indexOf("var btn = st.cool"),
   "冷却中**也不给**「去修复」按钮 —— 给的是「还要等多久」(强制, 不是只挂个牌子)");
ok(html.indexOf('body:JSON.stringify({session:SID})') >= 0,
   "扫码那条也带上会话号 —— 后端才判得出是不是这个会话在冷却");
ok(html.indexOf('$("#qrState").textContent=r.error||') >= 0
   && html.indexOf('$("#ngaQrState").textContent=r.error||') >= 0,
   "被后端挡回来的话**原样摆出来**(它已经是人话, 不套一句「取码失败：」)");
ok(html.indexOf("冷却期间那一路是") >= 0, "状态窗里把「真走不通」说清楚了, 不只是画个灯");

console.log("");
if (FAIL) { console.log("汇总: " + FAIL + " 项没过"); process.exit(1); }
console.log("汇总: 全部通过");
