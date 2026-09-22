/* 设置页「工具 / 外部工具」那段前端代码，不进浏览器也能验一遍。
 *
 * 为什么不进浏览器：这台机器前台有活，起浏览器（含无头）会撞上去 —— 见 user-habits。
 * 那就换个不吃进程的验法：把 index.html 里那几个**纯拼字符串**的函数抠出来，
 * 在 node 里喂真实的后端数据、断言拼出来的 HTML 对不对。
 * 剩下那些真要 DOM 才能验的（点击、滚动联动、测试钮改文案），留给用户自己点。
 *
 * 跑：node _syscheck/verify_tools_ui.js
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

/* 从整个 <script> 里抠出一个具名函数（按大括号配平，够用且不引 dependon） */
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

const SRC = ["esc", "setCtl", "setItem", "cmdText", "mcpInputs", "mcpStat", "mcpRow",
             "extCard", "cardGrid", "modelRow"]
  .map(grab).join("\n");
const mod = new Function(SRC +
  "\nreturn {esc,setCtl,setItem,cmdText,mcpInputs,mcpStat,mcpRow,extCard,cardGrid,modelRow};")();
const { esc, setCtl, setItem, cmdText, mcpInputs, mcpStat, mcpRow, extCard, cardGrid, modelRow } = mod;

/* ---------- 1. 拼出来的行：像不像后端给的那份数据 ---------- */
console.log("=== 1. 一条外部工具服务的行 ===");

// 后端 /api/tools 真实给出来的形状（照着 webapp.tools_payload() 与 settings.mcp_servers()）
const stdioSrv = {
  name: "我的工具", transport: "stdio", enabled: true,
  command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "D:\\some-folder"],
  cwd: "D:\\some-folder", env: {}, headers: {}, tools: null,
};
let row = mcpRow(stdioSrv);
ok(row.indexOf('data-f="name"') >= 0 && row.indexOf('value="我的工具"') >= 0, "名字填进去了");
ok(row.indexOf("server-filesystem") >= 0 && row.indexOf("D:\\some-folder") >= 0,
   "命令整条拼回一个框里（有空格也不拆）", cmdText(stdioSrv.command));
ok(row.indexOf('data-f="url"') < 0, "本地命令那种不显示地址框");
ok(row.indexOf('data-f="cwd"') >= 0, "另有「在哪跑」一个框");
ok(row.indexOf('data-f="enabled"') >= 0 && row.indexOf(" checked") >= 0, "开着的服务，勾是勾上的");
ok(row.indexOf("还没测过") >= 0, "没测过就写「还没测过」——不编一个数");
ok(row.indexOf('data-act="test"') >= 0 && row.indexOf('data-act="del"') >= 0, "有「测试」和「删除」");

const httpSrv = { name: "别人家的", transport: "http", enabled: false, url: "http://127.0.0.1:8121/mcp", tools: [] };
row = mcpRow(httpSrv);
ok(row.indexOf('data-f="url"') >= 0 && row.indexOf("8121") >= 0, "网址那种显示地址框");
ok(row.indexOf('data-f="command"') < 0, "网址那种不显示命令框");
ok(row.indexOf(" checked") < 0, "关着的服务，勾是空的");
ok(row.indexOf("连上了，但它一件工具都没有") >= 0,
   "「测过、一件工具都没有」（[]）与「还没测过」（null）分得开: " + (row.match(/<span class="mcpstat[^"]*">[^<]*/) || [""])[0]);

ok(mcpStat({ tools: [{ name: "a" }, { name: "b" }] }).indexOf("提供 2 件工具：a、b") >= 0,
   "测过且列到了工具，就说清有几件、叫什么");
ok(mcpStat({ tools: null }).indexOf("mcpstat") >= 0
   && mcpStat({ tools: null }).indexOf("ok") < 0, "还没测过的不标成绿的");

console.log("=== 2. 该转义的地方转义了 ===");
const evil = mcpRow({ name: '<img src=x onerror=alert(1)>', transport: "stdio",
                      command: ['"><script>bad()</script>'], tools: null });
ok(evil.indexOf("<img") < 0 && evil.indexOf("&lt;img") >= 0, "服务名里的尖括号被转义");
ok(evil.indexOf("<script>") < 0, "命令里的脚本标签被转义（不往外吐原始 HTML）");

console.log("=== 3. 「外部工具」那张卡 ===");
const payload = {                     // 后端 /api/tools 的真实形状
  builtin: [
    { name: "crawl_nga", label: "NGA 爬虫", enabled: true, optional: false },
    { name: "nlp_score", label: "情感打分", enabled: false, optional: true },
  ],
  servers: [stdioSrv, httpSrv],
  mcp: { command: "python", args: ["-m", "harness.mcp"], cwd: "C:\\path\\to\\harness-dist" },
};
let sec = extCard(payload);
ok(sec.indexOf('data-card="外部工具"') >= 0 && sec.indexOf('class="tcard"') >= 0,
   "是一张叫「外部工具」的卡（和别的工具并排摆，不再是单独一栏）");
ok(sec.indexOf("harness.mcp") >= 0 && sec.indexOf("C:\\path\\to\\harness-dist") >= 0,
   "写着「别人怎么连我们」那条命令 + 工作目录");
ok(sec.indexOf("自带工具现在开着的") < 0 && sec.indexOf("sethint") < 0,
   "不再摆「自带工具现在开着的」那行汇总（开关就在各自那张卡上）");
ok(sec.indexOf('data-loaded="1"') >= 0, "读到数了 -> 打上 data-loaded（保存时才敢存这一栏）");
ok((sec.match(/class="mcprow"/g) || []).length === 2, "两个服务各一行");

console.log("=== 3b. 工具卡片: 一件工具一张卡, 点开就地配 ===");
function item(o) {                    // 后端 /api/settings 给的一项（照着 settings.payload()）
  return Object.assign({ key: "", label: "", kind: "bool", value: "1", card: "", source: "默认值",
                         restart: false, help: "", note: null, lo: null, hi: null, choices: null,
                         sensitive: false, configured: null, writable: false }, o);
}
const grp = { name: "工具", layout: "cards", items: [
  item({ key: "TOOL_NGA", label: "NGA 爬虫", card: "NGA 爬虫" }),
  item({ key: "TOOL_NLP", label: "情感打分", value: "0", card: "情感打分" }),
  item({ key: "NLP_TOOL_URL", label: "打分服务地址", kind: "str",
         value: "http://127.0.0.1:8772", help: "本机小模型打分服务的地址。", card: "情感打分" }),
] };
const grid = cardGrid(grp, payload);
ok((grid.match(/class="tcard"/g) || []).length === 3,
   "两张工具卡 + 一张「外部工具」卡（共 3 张）");
ok(grid.indexOf('data-card="NGA 爬虫"') >= 0 && grid.indexOf('data-card="情感打分"') >= 0
   && grid.indexOf('data-card="外部工具"') >= 0, "卡是按后端给的 card 名字分的");
ok(grid.indexOf('<span class="tstate on">开</span>') >= 0, "卡头常显开关状态（收起来也看得见哪件开着）");
ok(grid.indexOf('<span class="tstate">关</span>') >= 0, "关着的照实写「关」");
ok(grid.indexOf("打分服务地址") >= 0 && grid.indexOf("本机小模型打分服务的地址") >= 0,
   "点开那张卡就是这件工具自己的参数");
ok((grid.match(/>NGA 爬虫</g) || []).length === 1,
   "卡里那行开关不重复写名字（卡头已经写着工具名了）");
ok(grid.indexOf('data-k="TOOL_NGA"') >= 0 && grid.indexOf('data-k="NLP_TOOL_URL"') >= 0,
   "开关和参数仍带 data-k（「保存」照旧收得到）");

console.log("=== 3c. 「当前模型」只读行（模型名不用用户填） ===");
ok(modelRow({ model: "deepseek-chat", auto: true }).indexOf("deepseek-chat") >= 0,
   "探到了就写模型名");
ok(modelRow({ model: "", auto: true }).indexOf("还没探测过") >= 0,
   "还没探过就照实说，不编一个名字");
ok(modelRow({ model: "deepseek-chat", auto: true, error: "密钥不对或还没填（HTTP 401）" })
   .indexOf("warnnote") >= 0, "探不到就把原因挂出来（不静默）");
ok(modelRow({ model: "x", auto: false }).indexOf("config.bat 指定") >= 0,
   "config.bat 里指定过的，标明是手动指定的");

console.log("=== 4. 那一栏没读到时不假装读过（否则保存会把清单存空） ===");
const bad = extCard(null);
ok(bad.indexOf("data-loaded") < 0, "没读到 -> 不打 data-loaded");
ok(bad.indexOf("mcprow") < 0, "没有任何服务行（没有可存的）");

console.log("=== 5. 换接法时那一行要换框 ===");
ok(mcpInputs(true).indexOf("data-f=url") >= 0 || mcpInputs(true).indexOf('data-f="url"') >= 0,
   "切到「网址」-> 出地址框");
ok(mcpInputs(false).indexOf('data-f="command"') >= 0, "切到「本地命令」-> 出命令框");
ok(mcpInputs(true, { url: "http://a/mcp" }).indexOf("http://a/mcp") >= 0, "换过去还在编辑的那行，地址不丢");

console.log("=== 6. 页面上那几处接线还在 ===");
ok(/\.cardgrid\s*\{[^}]*grid-template-columns:\s*1fr\s+1fr/.test(html),
   "卡片是两列并排往下滚的菜单");
ok(/\.tcard\.open\s*\{[^}]*grid-column:\s*1\s*\/\s*-1/.test(html),
   "点开的那张占满整行（里面的参数才摊得开）");
ok(html.indexOf("setnote") < 0, "页脚那段「密钥存 secrets.json…」的说明已撤");
ok(html.indexOf("/api/model") >= 0, "「重新探测」打的是真接口 /api/model");
ok(html.indexOf("/api/tools") >= 0, "loadSettings 会去读 /api/tools");
ok(html.indexOf('{action:"save", servers:collectMcp()}') >= 0
   || html.indexOf('action:"save"') >= 0, "「保存」会把外部工具清单一起发回去");
ok(html.indexOf("hasAttribute(\"data-loaded\")") >= 0,
   "保存前先看 data-loaded —— 那一栏没读出来就不碰它");
ok(html.indexOf('id="mcpAdd"') >= 0 || html.indexOf('"＋ 加一个"') >= 0, "有「＋ 加一个」");

console.log("");
if (FAIL) { console.log("汇总: " + FAIL + " 项没过"); process.exit(1); }
console.log("汇总: 全部通过");
