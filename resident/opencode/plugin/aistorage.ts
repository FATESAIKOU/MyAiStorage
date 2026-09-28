// AiStorage 住民 plugin（docs/design/agora-session-operations.md）。
//
// 這個檔案刻意**只做兩件事**（opencode 的 plugin API 是 TypeScript，邏輯不好寫，
// 也不好測）：
//   1. 從 `context.sessionID` 拿到目前的 Session id（1.7f）。
//   2. 把工具呼叫轉成 CLI：
//        agora_* → `python -m aistorage.agora_cli <cmd>`
//        aistorage_* → `python -m aistorage.skill <cmd>`
//
// 所有邏輯都在 Python 端（`src/aistorage/agora_cli/` 與 `src/aistorage/skill/`），
// 那裡才有單元測試。
//
// **Session id 由這裡傳，不接受模型提供的 id**——模型可能填錯或填別人的。
//
// 密鑰不經過這裡：plugin 只 spawn 一個本機的 python 子行程，憑證由
// entrypoint 以檔案引用方式放在 /secrets，python 自己讀。

import { spawn } from "node:child_process"
import { mkdirSync, writeFileSync } from "node:fs"

// 主 Session 限定的工具（宣告停止沒有提交流程那一道防線）
const MAIN_SESSION_ONLY = new Set(["aistorage_stop"])

// 目前 Session 的 parentID。opencode 的 plugin 在這裡拿得到 context，
// 所以第一次查之後就記著，不必為每個工具呼叫再問一次 API。
let parentOfCurrent: string | null | undefined
let currentSession: string | null = null

type ToolContext = { sessionID?: string; directory?: string; $?: unknown }

type ToolArgs = Record<string, unknown>

interface ToolDef {
  description: string
  /** 只在主 Session 可用（plugin 這一層是第一道防線） */
  mainOnly?: boolean
  /** 呼叫哪一個 CLI：`agora`（單一入口）或 `skill` */
  cli: "agora" | "skill"
  /** CLI 的子命令 */
  command: string
  /** 把模型給的參數轉換成 CLI 的位置／旗標 */
  args: (args: ToolArgs, ctx: ToolContext) => string[]
}

// 參數直接放進 argv。`spawn` **不經過 shell**，所以千萬不要加 shell 引號
// （review-g5-6 H2）：加了單引號，Python 收到的字串會把 `'` 當成內容的一部分，
// `handoff:01…` 就會變成 `'handoff:01…'` 而查不到交接單。
// 這裡只擋「不該出現在 argv 裡」的控制字元（NUL 無法存在於 argv；換行可以，
// 所以不擋換行——AI 寫的多行摘要本來就該原樣傳過去）。

function str(args: ToolArgs, key: string): string | undefined {
  const v = args[key]
  if (typeof v === "string") return v
  // 9.1 e2e 實測：模型有時把參數包成 `{"properties": {...}}`；單值參數也要找得到
  // （找不到就會變成「缺 --to」這種 argparse 錯誤）。
  const deep = deepFindKey(args, [key])
  return typeof deep === "string" ? deep : undefined
}

function deepFindKey(value: unknown, names: string[], depth = 0): unknown {
  // 9.1 e2e 實測：模型把參數包成 `{"properties": {"handoff_ids": {...}}}`
  // （像在填 JSON Schema），所以只在最外層找鍵會找不到。往下找幾層。
  if (depth > 4 || !value || typeof value !== "object") return undefined
  if (!Array.isArray(value)) {
    for (const name of names) {
      if (name in (value as Record<string, unknown>)) {
        return (value as Record<string, unknown>)[name]
      }
    }
  }
  const children: unknown[] = Array.isArray(value) ? value : Object.values(value)
  for (const child of children) {
    const found = deepFindKey(child, names, depth + 1)
    if (found !== undefined) return found
  }
  return undefined
}

function isTrue(args: ToolArgs, key: string): boolean {
  // 開關型參數：模型有時送布林、有時送字串 "true"／"yes"（9.1 e2e 實測）。
  const v = args[key]
  if (typeof v === "boolean") return v
  if (typeof v === "string") return ["true", "yes", "1"].includes(v.trim().toLowerCase())
  return false
}

function strList(args: ToolArgs, key: string): string[] {
  // 小模型很常換名字或換形狀（9.1 e2e 實測：claim 送過 `handoff_id` 單數、
  // `{"handoffs": {"item": [...]}}`、以及整包 `{"properties": {...}}`，
  // 結果 CLI 收到 0 個 id）。這裡吸收掉常見的換法：別的鍵名、任何層級的鍵、
  // 單一字串、JSON 字串、只含一個清單的包裝物件。
  const names = [key, ...singular(key), "items", "ids"]
  const cands: unknown[] = [args[key]]
  const deep = deepFindKey(args, names)
  if (deep !== undefined) cands.push(deep)
  for (const alt of singular(key) + ["items", "ids"]) {
    if (alt in args) cands.push(args[alt])
  }
  const out: string[] = []
  const walk = (v: unknown): void => {
    if (typeof v === "string") {
      const s = v.trim()
      if (s.startsWith("[")) {
        try {
          walk(JSON.parse(s))
        } catch {
          out.push(v)
        }
      } else if (s) {
        out.push(v)
      }
      return
    }
    if (Array.isArray(v)) {
      v.forEach(walk)
      return
    }
    if (v && typeof v === "object") {
      Object.values(v as Record<string, unknown>).forEach(walk)
    }
  }
  cands.forEach(walk)
  return [...new Set(out)]
}

function singular(key: string): string[] {
  // 換名字的各種可能：`handoff_ids` → handoff_id／handoffs／handoff
  if (key.endsWith("_ids")) {
    const one = key.slice(0, -1) // handoff_id
    const word = one.endsWith("_id") ? one.slice(0, -3) : one // handoff
    return [one, `${word}s`, word]
  }
  if (key.endsWith("s")) return [key.slice(0, -1), `${key}s`]
  return [key + "_ids", key + "s", key]
}

const TOOLS: Record<string, ToolDef> = {
  // ---------------------------------------------------------------- agora CLI
  agora_find: {
    description:
      "找 Session，或列出等人接的交接單（waiting: true）。" +
      "回傳附帶快照時間與新鮮度；新鮮度有警告時要照實轉述給使用者。",
    cli: "agora",
    command: "find",
    args: (a) => {
      const out: string[] = []
      const query = str(a, "query")
      if (query) out.push(query)
      const c = str(a, "case_id")
      if (c) out.push("--case", c)
      if (isTrue(a, "waiting")) out.push("--waiting")
      return out
    },
  },
  agora_show: {
    description: "看一個 Session 的 metadata、前後 Session Link 與交接單。",
    cli: "agora",
    command: "show",
    args: (a) => {
      const out: string[] = []
      const sid = str(a, "session_id")
      if (sid) out.push(sid)
      return out
    },
  },
  agora_read: {
    description:
      "讀一個 Session 最新已提交的內容（回傳附帶快照時間與新鮮度；警告要明說" +
      "「可能不是最新的」）。這就是參考別人工作的入口。",
    cli: "agora",
    command: "read",
    args: (a, ctx) => {
      const out: string[] = []
      const sid = str(a, "session_id")
      if (sid) out.push(sid)
      // 「是誰讀的」由 context 帶入，不給模型填（模型會填錯或填別人的）。
      // 有了它這次讀取就會留下一條參考 Link。
      if (ctx?.sessionID) out.push("--from", ctx.sessionID)
      return out
    },
  },
  agora_handoff: {
    description:
      "交出工作：同步自己，為每一個 task 各寫一張交接單，一起提交並等到可見。" +
      "（1→n 分工用多個 task；n→1 交出末端用一個。）" +
      "tasks: [{title, summary, next_steps}]。",
    cli: "agora",
    command: "handoff",
    args: (a, ctx) => {
      const out: string[] = []
      // 位置參數是「要交出的 Session」，由 context 帶入（不給模型填）。
      if (ctx?.sessionID) out.push(ctx.sessionID)
      // ★ 每個 task 物件（title／summary／next_steps）**各成一張交接單**
      //   （review-73dbf2c H3）。以前用 strList 把它們摊平成多個字串，於是
      //   一個有 title+summary+next_steps 的 task 變成三張交接單，9.1 的
      //   「剛好兩張」永遠對不上。每個 task 物件在 argv 裡是一個 --
      //   tasks-file（JSON 清單），由 CLI 端一對一組單。
      const file = writeTasksFile(a)
      if (file) out.push("--tasks-file", file)
      return out
    },
  },
  agora_checkout: {
    description:
      "產出一個起點包（start point package）：把起點之前的原始紀錄原封不動放進" +
      "一個目錄，交給 agora-opencode load 變成新的 session。" +
      "startpoints: ['handoff:<id>' 或 '<session>[@<訊息>]']（多個＝n→1 統合）。" +
      "起點是交接單時會一併登記認領，被拒就不產出。" +
      "認領逾時或被中斷時**不要自己重跑**：本機留有這次認領的記錄，" +
      "重跑必須沿用同一個預留的 session id，工具沒有那個參數，請把逾時" +
      "原樣回報使用者，由使用者決定怎麼接續。" +
      "產出的目錄在回傳的 package 欄位。",
    cli: "agora",
    command: "checkout",
    args: (a, ctx) => {
      const out: string[] = []
      for (const sp of strList(a, "startpoints")) out.push(sp)
      const task = str(a, "task")
      if (task) out.push("--task", task)
      // 輸出目錄：預設放在 /work 底下（不要寫進 repo 的版本控制裡）。
      // ★ **每次呼叫都要是新的目錄**（review-73dbf2c H1）：固定目錄會讓第二次
      //   checkout 直接撞上「目錄已經有東西」——而那時認領可能已經送出去了。
      const outDir = str(a, "out_dir") ?? uniqueOutDir()
      out.push("-o", outDir)
      if (isTrue(a, "resume")) out.push("--resume")
      if (ctx?.directory) out.push("--max-lag", "15m")
      return out
    },
  },

  // -------------------------------------------------------------- skill CLI
  aistorage_whoami: {
    description: "我在哪個 Session：{session_id, parent_id, is_main}。",
    cli: "skill",
    command: "whoami",
    args: () => [],
  },
  aistorage_split: {
    description:
      "分裂（1→n）：同步自己，為每一份工作各寫一張交接單，一起提交並等到全部可見。" +
      "parts: [{title, summary, next_steps}]。",
    // parts 交給 CLI 從檔案讀（--parts <file>），避免在 argv 裡塞大段文字
    cli: "skill",
    command: "split",
    args: () => [],
  },
  aistorage_handoff_end: {
    description: "交出末端：同步自己，寫一張交接單，提交並等到可見。",
    cli: "skill",
    command: "handoff-end",
    args: (a) => {
      const out: string[] = []
      const summary = str(a, "summary")
      if (summary) out.push("--summary", summary)
      const next = str(a, "next_steps")
      if (next) out.push("--next-steps", next)
      return out
    },
  },
  aistorage_reference: {
    description:
      "留下參考 Link：已經讀過某個 Session、而且這次的成果有賴於它時呼叫。" +
      "只上傳不觸發提交（下一輪提交流程收進去）。",
    cli: "skill",
    command: "reference",
    args: (a) => {
      const out: string[] = []
      const to = str(a, "session_id")
      if (to) out.push("--to", to)
      const at = str(a, "read_snapshot_at")
      if (at) out.push("--read-snapshot-at", at)
      return out
    },
  },
  aistorage_list_handoffs: {
    description: "列出還沒被認領的交接單（等同 agora_find 的 waiting）。",
    cli: "skill",
    command: "list-handoffs",
    args: (a) => {
      const out: string[] = []
      const c = str(a, "case_id")
      if (c) out.push("--case", c)
      return out
    },
  },
  aistorage_stop: {
    description: "宣告這個 Session 停止中（設定 time.archived，然後同步並提交）。",
    mainOnly: true,
    cli: "skill",
    command: "stop",
    args: () => [],
  },
}

/**
 * 這個 Session 是不是主 Session。
 *
 * **fail-closed（review-g5-6 H1）**：只有「查得到自己、而且自己沒有 parentID」
 * 才算主 Session。查詢失敗、或清單裡找不到自己，一律 throw 拒絕，
 * 而且**不把失敗寫進快取**（否則 API 恢復之後仍會用錯的結果）。
 * 宣告停止沒有提交流程那一道防線，這裡放行就等於沒有檢查。
 */
async function resolveParent(sessionId: string): Promise<string | null> {
  if (currentSession === sessionId && parentOfCurrent !== null) {
    return parentOfCurrent
  }
  const base =
    (process.env.AISTORAGE_OPENCODE_URL as string | undefined) ??
    "http://127.0.0.1:4096"
  let sessions: Array<Record<string, unknown>>
  try {
    const resp = await fetch(`${base}/session`)
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`)
    }
    sessions = (await resp.json()) as Array<Record<string, unknown>>
  } catch (e) {
    // 不要快取失敗：下一次呼叫要重新問
    currentSession = null
    parentOfCurrent = null
    throw new Error(
      `無法確認目前是不是主 Session（問不到 opencode 的 Session 清單：${String(
        (e as Error)?.message ?? e,
      )}）。為了安全，拒絕執行。`,
    )
  }
  const me = sessions.find((s) => s?.id === sessionId)
  if (!me) {
    currentSession = null
    parentOfCurrent = null
    throw new Error(
      "opencode 的 Session 清單裡找不到自己（可能剛被刪除或 id 不對）。" +
        "為了安全，拒絕執行。",
    )
  }
  const parent = me.parentID ?? me.parentId
  parentOfCurrent = typeof parent === "string" && parent ? parent : null
  currentSession = sessionId
  return parentOfCurrent
}

function runCli(
  cli: "agora" | "skill",
  command: string,
  args: string[],
  sessionId: string,
  parts?: unknown,
): Promise<string> {
  // parts 走暫存檔（argv 裡不放大段文字；JSON 用檔案傳）
  // `agora` 沒有 --session：它的位置參數由 args() 從 context 帶進去。
  const argv = cli === "agora"
    ? ["-m", "aistorage.agora_cli", command, ...args]
    : ["-m", "aistorage.skill", command, "--session", sessionId, ...args]
  if (parts !== undefined) {
    argv.push("--parts", writeTempParts(parts))
  }
  return new Promise((resolve, reject) => {
    const child = spawn("python", argv, {
      stdio: ["ignore", "pipe", "pipe"],
      env: process.env,
    })
    let out = ""
    let err = ""
    child.stdout.on("data", (d) => (out += d.toString()))
    child.stderr.on("data", (d) => (err += d.toString()))
    child.on("error", reject)
    child.on("close", (code) => {
      if (code === 0) resolve(out.trim())
      else reject(new Error(err.trim() || `${cli === "agora" ? "agora" : "aistorage.skill"} ${command} 失敗 (rc=${code})`))
    })
  })
}

function writeTempParts(parts: unknown): string {
  // 用 opencode 給的目錄（/work）下的 .aistorage/tmp；沒有就用系統暫存目錄
  const dir = (process.env.AISTORAGE_TMP_DIR as string | undefined) || "/tmp/aistorage"
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  const file = `${dir}/split-parts-${Date.now()}-${Math.random().toString(36).slice(2)}.json`
  // 原樣寫，不要包成 `[parts]`：模型有時把清單包成 `{"item": [...]}`，
  // 包一層會變成 `[{"item": [...]}]`，正規化就認不出來了
  // （9.1 e2e 實測：space-bunny-free 每次都這樣送）。
  // 形狀由 CLI 端的 normalize_parts 吸收。
  writeFileSync(file, JSON.stringify(parts), { mode: 0o600 })
  return file
}

// 輸出目錄的前綴（底下每次呼叫再補一個唯一子目錄）
const PACKAGE_DIR = "/work/.agora-packages"

function uniqueOutDir(): string {
  // ★ 每次呼叫都要是新的目錄（review-73dbf2c H1）。固定成 /work/.agora-packages
  //   的話，第二次 checkout 一定撞上「目錄已經有東西」；而那時（改了流程之後）
  //   認領可能已經送出去了，交接單就卡在沒有人接手的狀態。
  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  return `${PACKAGE_DIR}/pkg-${stamp}`
}

function writeTasksFile(a: ToolArgs): string | undefined {
  // 把模型給的 tasks **原樣**寫成 JSON 檔，CLI 端一個物件一張交接單。
  // 為什麼走檔案：title／summary／next_steps 是三段不同的內容，攤平到 argv 裡
  // 就分不出哪個字串屬於哪一張單（review-73dbf2c H3）。
  const tasks = taskObjects(a)
  if (!tasks.length) return undefined
  const dir = (process.env.AISTORAGE_TMP_DIR as string | undefined) || "/tmp/aistorage"
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  const file = `${dir}/handoff-tasks-${Date.now()}-${Math.random().toString(36).slice(2)}.json`
  writeFileSync(file, JSON.stringify(tasks), { mode: 0o600 })
  return file
}

function taskObjects(a: ToolArgs): Array<Record<string, unknown>> {
  // 找出模型給的 task 清單，**保持每個物件一筆**。
  // 形狀很多（`[{...}]`、`{"item": [...]}`、`{"properties": {...}}`、JSON 字串），
  // 那些由 CLI 端的 normalize_parts 吸收；這裡只負責把「清單」找出來，
  // 千萬不要把物件裡的字串值攤平（那正是 H3 的 bug）。
  const raw = deepFindKey(a, ["tasks", "task", "parts", "items"])
  const list = asTaskList(raw)
  if (list) return list
  const single = deepFindKey(a, ["title"])
  if (typeof single === "string" && single.trim()) {
    return [{ title: single.trim() }]
  }
  return []
}

function asTaskList(value: unknown): Array<Record<string, unknown>> | null {
  if (Array.isArray(value)) {
    const out: Array<Record<string, unknown>> = []
    for (const item of value) {
      if (item && typeof item === "object" && !Array.isArray(item)) {
        out.push(item as Record<string, unknown>)
      } else if (typeof item === "string" && item.trim()) {
        out.push({ title: item.trim() })
      }
    }
    return out.length ? out : null
  }
  if (typeof value === "string") {
    const text = value.trim()
    if (text.startsWith("[")) {
      try {
        return asTaskList(JSON.parse(text))
      } catch {
        return null
      }
    }
    return text ? [{ title: text }] : null
  }
  if (value && typeof value === "object") {
    const obj = value as Record<string, unknown>
    if (typeof obj.title === "string" || typeof obj.summary === "string") {
      return [obj]
    }
    for (const nested of Object.values(obj)) {
      const found = asTaskList(nested)
      if (found) return found
    }
  }
  return null
}

export const AistoragePlugin = async () => {
  return {
    tool: {
      // plugin 提供的是「工具的實作」；宣告（名稱、說明、參數 schema）在
      // 同一個 key 陣列裡，opencode 會把它們一起註冊。
      ...Object.fromEntries(
        Object.entries(TOOLS).map(([name, def]) => [
          name,
          {
            description: def.description,
            // Session id 不在參數 schema 裡：由 context 帶入，模型不能指定
            args: {
              type: "object" as const,
              properties: {
                ...(name === "aistorage_split"
                  ? {
                      parts: {
                        type: "array",
                        description: "要切出去的工作",
                        items: {
                          type: "object",
                          properties: {
                            title: { type: "string" },
                            summary: { type: "string" },
                            next_steps: { type: "string" },
                          },
                          required: ["title"],
                        },
                      },
                    }
                  : {}),
                ...(name === "agora_find"
                  ? { query: { type: "string" }, case_id: { type: "string" },
                      waiting: { type: "boolean" } }
                  : {}),
                ...(name === "agora_show" || name === "agora_read"
                  ? { session_id: { type: "string" } }
                  : {}),
                ...(name === "agora_handoff"
                  ? {
                      tasks: {
                        type: "array",
                        description: "要交出去的每一份工作（多個＝分工）",
                        items: {
                          type: "object",
                          properties: {
                            title: { type: "string" },
                            summary: { type: "string" },
                            next_steps: { type: "string" },
                          },
                          required: ["title"],
                        },
                      },
                    }
                  : {}),
                ...(name === "agora_checkout"
                  ? {
                      startpoints: {
                        type: "array",
                        description:
                          "起點：handoff:<交接單 id> 或 <session>[@<訊息>]；多個＝統合",
                        items: { type: "string" },
                      },
                      task: { type: "string", description: "要交代給接手者的任務" },
                      out_dir: {
                        type: "string",
                        description:
                          "起點包的輸出目錄（預設每次呼叫一個新的）",
                      },
                      resume: {
                        type: "boolean",
                        description:
                          "沿用本機記錄裡同一個認領重試。**一般情況不要用**：" +
                          "逾時或中斷後預設就會自動沿用本機記錄；" +
                          "需要指定時才帶，帶了也請把結果回報使用者。",
                      },
                    }
                  : {}),
                ...(name === "aistorage_reference"
                  ? { session_id: { type: "string" }, read_snapshot_at: { type: "string" } }
                  : {}),
                ...(name === "aistorage_list_handoffs" ? { case_id: { type: "string" } } : {}),
                ...(name === "aistorage_handoff_end"
                  ? { summary: { type: "string" }, next_steps: { type: "string" } }
                  : {}),
              },
            },
            async execute(args: ToolArgs, ctx: ToolContext) {
              const sessionId = ctx?.sessionID
              if (!sessionId) {
                throw new Error("拿不到目前的 Session id（plugin 的 context 沒有 sessionID）")
              }
              // 第一層：主 Session 限定
              if (MAIN_SESSION_ONLY.has(name) || def.mainOnly) {
                const parent = await resolveParent(sessionId)
                if (parent) {
                  throw new Error(
                    `${name} 只能在主 Session 做（目前是子 Session，parent=${parent}）`,
                  )
                }
              }
              const parts = (args as ToolArgs).parts
              // `--parts` 只有 `aistorage_split` 吃（它要 `{title, summary,
              // next_steps}` 的清單）。其他命令沒有這個參數，送過去 argparse
              // 會讓整個工具失敗。
              const useParts = name === "aistorage_split" ? parts : undefined
              return await runCli(
                def.cli, def.command, def.args(args as ToolArgs, ctx), sessionId, useParts,
              )
            },
          },
        ]),
      ),
    },
  }
}

export default AistoragePlugin
