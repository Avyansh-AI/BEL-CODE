/**
 * BEL Code <-> OpenCode Official TypeScript/JS SDK Adapter (`bel_code/opencode_bridge/sdk_adapter.mjs`)
 *
 * Bridges `@opencode-ai/sdk` (`vendor/opencode/packages/sdk/js` & `vendor/opencode/packages/sdk`)
 * to BEL Code's Layer 1 OpenCode Server endpoint.
 */

import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const PROJECT_ROOT = path.resolve(__dirname, "..", "..")
const VENDOR_OPENCODE_DIR = path.join(PROJECT_ROOT, "vendor", "opencode")
const SDK_JS_PACKAGE_JSON = path.join(VENDOR_OPENCODE_DIR, "packages", "sdk", "js", "package.json")
const SDK_V2_PACKAGE_JSON = path.join(VENDOR_OPENCODE_DIR, "packages", "sdk", "package.json")

export const OPENCODE_ROUTES = [
  "/global/health",
  "/project",
  "/project/current",
  "/session",
  "/session/{id}",
  "/session/{id}/message",
  "/session/{id}/children",
  "/session/{id}/todo",
  "/session/{id}/diff",
  "/session/{id}/abort",
  "/session/{id}/revert",
  "/session/{id}/permissions/{permissionID}",
  "/config",
  "/config/providers",
  "/agent",
  "/command",
  "/file",
  "/file/content",
  "/file/status",
  "/find",
  "/find/file",
  "/find/symbol",
  "/mcp",
  "/event"
]

export function describeSdkBridge(baseUrl = "http://127.0.0.1:4096") {
  let sdkMeta = { name: "@opencode-ai/sdk", version: "2.3.0" }
  if (fs.existsSync(SDK_JS_PACKAGE_JSON)) {
    try {
      sdkMeta = JSON.parse(fs.readFileSync(SDK_JS_PACKAGE_JSON, "utf-8"))
    } catch {
      // ignore parse errors
    }
  } else if (fs.existsSync(SDK_V2_PACKAGE_JSON)) {
    try {
      sdkMeta = JSON.parse(fs.readFileSync(SDK_V2_PACKAGE_JSON, "utf-8"))
    } catch {
      // ignore parse errors
    }
  }

  return {
    sdk: "@opencode-ai/sdk",
    package_name: sdkMeta.name || "@opencode-ai/sdk",
    version: sdkMeta.version || "2.3.0",
    vendor_sdk_js_path: path.join(VENDOR_OPENCODE_DIR, "packages", "sdk", "js"),
    vendor_sdk_path: path.join(VENDOR_OPENCODE_DIR, "packages", "sdk"),
    baseUrl,
    routes: OPENCODE_ROUTES,
    status: "ready"
  }
}

export function createOpencodeClient(options = {}) {
  const baseUrl = String(options.baseUrl || "http://127.0.0.1:4096").replace(/\/+$/, "")
  const headers = {
    "Content-Type": "application/json",
    ...(options.directory ? { "x-opencode-directory": String(options.directory) } : {}),
    ...(options.headers || {})
  }

  async function request(method, routePath, body) {
    const response = await fetch(`${baseUrl}${routePath}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined
    })
    return await response.json()
  }

  return {
    baseUrl,
    global: {
      health: () => request("GET", "/global/health")
    },
    project: {
      list: () => request("GET", "/project"),
      current: () => request("GET", "/project/current")
    },
    session: {
      list: () => request("GET", "/session"),
      create: (payload = {}) => request("POST", "/session", payload),
      get: (id) => request("GET", `/session/${id}`),
      delete: (id) => request("DELETE", `/session/${id}`),
      prompt: (id, payload = {}) => request("POST", `/session/${id}/message`, payload),
      message: (id, payload = {}) => request("POST", `/session/${id}/message`, payload),
      children: (id) => request("GET", `/session/${id}/children`),
      todo: (id) => request("GET", `/session/${id}/todo`),
      diff: (id) => request("GET", `/session/${id}/diff`),
      abort: (id) => request("POST", `/session/${id}/abort`, {}),
      revert: (id, payload = {}) => request("POST", `/session/${id}/revert`, payload),
      respondPermission: (id, permissionID, payload = {}) =>
        request("POST", `/session/${id}/permissions/${permissionID}`, payload)
    },
    config: {
      get: () => request("GET", "/config"),
      providers: () => request("GET", "/config/providers")
    },
    agent: {
      list: () => request("GET", "/agent")
    },
    command: {
      list: () => request("GET", "/command")
    },
    file: {
      list: (dirPath = ".") => request("GET", `/file?path=${encodeURIComponent(dirPath)}`),
      read: (filePath) => request("GET", `/file/content?path=${encodeURIComponent(filePath)}`),
      status: () => request("GET", "/file/status")
    },
    find: {
      text: (pattern) => request("GET", `/find?pattern=${encodeURIComponent(pattern)}`),
      files: (query) => request("GET", `/find/file?query=${encodeURIComponent(query)}`),
      symbols: (query) => request("GET", `/find/symbol?query=${encodeURIComponent(query)}`)
    },
    mcp: {
      status: () => request("GET", "/mcp")
    },
    event: {
      subscribe: () => request("GET", "/event")
    }
  }
}

export async function createOpencode(options = {}) {
  const client = createOpencodeClient(options)
  return {
    client,
    server: {
      url: client.baseUrl,
      close: () => {}
    }
  }
}

// CLI execution entrypoint when invoked via `node sdk_adapter.mjs --action describe`
if (process.argv[1] && path.resolve(process.argv[1]) === __filename) {
  const args = process.argv.slice(2)
  const baseUrlIdx = args.indexOf("--base-url")
  const baseUrl = baseUrlIdx >= 0 && args[baseUrlIdx + 1] ? args[baseUrlIdx + 1] : "http://127.0.0.1:4096"
  const info = describeSdkBridge(baseUrl)
  process.stdout.write(JSON.stringify(info) + "\n")
}
