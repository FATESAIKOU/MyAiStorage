// AiStorage 住民 plugin（tasks 5.4；docs/impl/group5-7 第 4 節）。
//
// 這個檔案刻意**只做兩件事**（opencode 的 plugin API 是 TypeScript，邏輯不好寫，
// 也不好測）：
//   1. 從 `context.sessionID` 拿到目前的 Session id（1.7f），並查出它是不是
//      子 Session（有 parentID）。
//   2. 把工具呼叫轉成 `python -m aistorage.skill <cmd> --session <ctx.sessionID> …`。
//
// 所有邏輯（同步、組項目、同步並提交、讀取、主 Session 的第二道檢查）都在
// Python 端 `src/aistorage/skill/`，那裡才有單元測試。
//
// **Session id 由這裡傳，不接受模型提供的 id**——模型可能填錯或填別人的。
//
// 密鑰不經過這裡：plugin 只 spawn 一個本機的 python 子行程，憑證由
// entrypoint 以檔案引用方式放在 /secrets，python 自己讀。

import { spawn } from "node:child_process"
import { mkdirSync, writeFileSync } from "node:fs"

// 主 Session 限定的工具（第三層：提交流程的 apply_claim 還會再擋一次）
const MAIN_SESSION_ONLY = new Set(["claim", "stop"])

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
  /** 把模型給的參數轉換成 CLI 的位置／旗標 */
  args: (args: ToolArgs) => string[]
}

// 參數直接放進 argv。`spawn` **不經過 shell**，所以千萬不要加 shell 引號
// （review-g5-6 H2）：加了單引號，Python 收到的字串會把 `'` 當成內容的一部分，
// `handoff:01…` 就會變成 `'handoff:01…'` 而查不到交接單。
// 這裡只擋「不該出現在 argv 裡」的控制字元（NUL 無法存在於 argv；換行可以，
// 所以不擋換行——AI 寫的多行摘要本來就該原樣傳過去）。

function str(args: ToolArgs, key: string): string | undefined {
  const v = args[key]
  if (typeof v !== "string") return undefined
  return v
}

function strList(args: ToolArgs, key: string): string[] {
  // 小模型很常換名字或換形狀（9.1 e2e 實測：claim 送了 `handoff_id` 單數、
  // 或 `{"handoffs": {"item": [...]}}`，結果 CLI 收到 0 個 id）。這裡吸收掉
  // 常見的換法：別的鍵名、單一字串、JSON 字串、只含一個清單的包裝物件。
  const cands: unknown[] = [args[key]]
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
  aistorage_whoami: {
    description: "我在哪個 Session：{session_id, parent_id, is_main}。",
    args: () => [],
  },
  aistorage_split: {
    description:
      "分裂（1→n）：同步自己，為每一份工作各寫一張交接單，一起提交並等到全部可見。" +
      "parts: [{title, summary, next_steps}]。",
    // parts 交給 CLI 從檔案讀（--parts <file>），避免在 argv 裡塞大段文字
    args: () => [],
  },
  aistorage_handoff_end: {
    description: "交出末端：同步自己，寫一張交接單，提交並等到可見。",
    args: (a) => {
      const out: string[] = []
      const summary = str(a, "summary")
      if (summary) out.push("--summary", summary)
      const next = str(a, "next_steps")
      if (next) out.push("--next-steps", next)
      return out
    },
  },
  aistorage_claim: {
    description:
      "認領交接單（多張＝統合）：同步自己與所有認領，一起提交；Link 屬於自己之後" +
      "才回傳交接單內容。任何一張被拒收就停下並回報原因。",
    mainOnly: true,
    args: (a) => {
      const out: string[] = []
      for (const id of strList(a, "handoff_ids")) out.push("--handoff", id)
      return out
    },
  },
  aistorage_find: {
    description:
      "找 Session（回傳附帶快照時間與新鮮度；新鮮度有警告時要照實轉述給使用者）。",
    args: (a) => {
      const out: string[] = []
      const query = str(a, "query") ?? ""
      out.push("--query", query)
      const c = str(a, "case_id")
      if (c) out.push("--case", c)
      return out
    },
  },
  aistorage_read: {
    description:
      "讀一個 Session（回傳附帶快照時間與新鮮度；警告要明說「可能不是最新的」）。",
    args: (a) => {
      const out: string[] = []
      const target = str(a, "session_id")
      if (target) out.push("--target", target)
      return out
    },
  },
  aistorage_reference: {
    description:
      "留下參考 Link：讀過某個 Session 之後呼叫，記下我參考它。" +
      "只上傳不觸發提交（下一輪提交流程收進去）。",
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
    description: "列出還沒被認領的交接單。",
    args: (a) => {
      const out: string[] = []
      const c = str(a, "case_id")
      if (c) out.push("--case", c)
      return out
    },
  },
  aistorage_register_artifact: {
    description:
      "登錄一件產出到 Foundry 產出目錄（只上傳，下一輪提交流程收進去）。" +
      "contained：本體是容器內的本機檔案（file_path）；link：原處產出" +
      "（必填 link，repo 與 path 可選填）。produced_by_session_id 由 plugin 帶入，不要自己填。",
    args: (a) => {
      const out: string[] = []
      const kind = str(a, "kind")
      if (kind) out.push("--kind", kind)
      const n = str(a, "name")
      if (n) out.push("--name", n)
      const ct = str(a, "content_type")
      if (ct) out.push("--content-type", ct)
      const link = str(a, "link")
      if (link) out.push("--link", link)
      const repo = str(a, "repo")
      if (repo) out.push("--repo", repo)
      const path = str(a, "path")
      if (path) out.push("--path", path)
      const file = str(a, "file_path")
      if (file) out.push("--file", file)
      const desc = str(a, "description")
      if (desc) out.push("--description", desc)
      const c = str(a, "case_id")
      if (c) out.push("--case", c)
      return out
    },
  },
  aistorage_stop: {
    description: "宣告這個 Session 停止中（設定 time.archived，然後同步並提交）。",
    mainOnly: true,
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
  command: string,
  args: string[],
  sessionId: string,
  parts?: unknown,
): Promise<string> {
  // parts 走暫存檔（argv 裡不放大段文字；JSON 用檔案傳）
  const argv = ["-m", "aistorage.skill", command, "--session", sessionId, ...args]
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
      else reject(new Error(err.trim() || `aistorage.skill ${command} 失敗 (rc=${code})`))
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
                ...(name === "aistorage_claim" ? { handoff_ids: { type: "array", items: { type: "string" } } } : {}),
                ...(name === "aistorage_find" ? { query: { type: "string" }, case_id: { type: "string" } } : {}),
                ...(name === "aistorage_read" ? { session_id: { type: "string" } } : {}),
                ...(name === "aistorage_reference"
                  ? { session_id: { type: "string" }, read_snapshot_at: { type: "string" } }
                  : {}),
                ...(name === "aistorage_list_handoffs" ? { case_id: { type: "string" } } : {}),
                ...(name === "aistorage_register_artifact"
                  ? {
                      kind: { type: "string", enum: ["link", "contained"] },
                      name: { type: "string" },
                      content_type: { type: "string" },
                      link: { type: "string" },
                      repo: { type: "string" },
                      path: { type: "string" },
                      file_path: { type: "string" },
                      description: { type: "string" },
                      case_id: { type: "string" },
                    }
                  : {}),
                ...(name === "aistorage_handoff_end"
                  ? { summary: { type: "string" }, next_steps: { type: "string" } }
                  : {}),
              },
            },
            async execute(args: ToolArgs, ctx: ToolContext) {
              const command = name.replace(/^aistorage_/, "").replace(/_/g, "-")
              const sessionId = ctx?.sessionID
              if (!sessionId) {
                throw new Error("拿不到目前的 Session id（plugin 的 context 沒有 sessionID）")
              }
              // 第一層：主 Session 限定
              if (MAIN_SESSION_ONLY.has(name) || def.mainOnly) {
                const parent = await resolveParent(sessionId)
                if (parent) {
                  throw new Error(
                    `aistorage_${command} 只能在主 Session 做（目前是子 Session，parent=${parent}）`,
                  )
                }
              }
              const parts = (args as ToolArgs).parts
              return await runCli(command, def.args(args as ToolArgs), sessionId, parts)
            },
          },
        ]),
      ),
    },
  }
}

export default AistoragePlugin
