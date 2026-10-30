"""Persistent JSON-RPC client for the installed Codex App Server protocol."""

import json
import queue
import shutil
import subprocess
import threading
import time

from . import __version__
from .ai_contract import RESULT_SCHEMA, validate_result
from .prompts import SYSTEM_INSTRUCTIONS


class AppServer:
    def __init__(
        self, state, command=None, display_name="本人", group_name="关注群", executable=None
    ):
        self.context = {"current_user": display_name, "watched_group": group_name}
        state.mkdir(parents=True, exist_ok=True)
        self.cwd = state
        self.lock = threading.RLock()
        self.pending = {}
        self.events = queue.Queue()
        self.seq = 0
        self.loaded_threads = set()
        self.log = (state / "appserver.log").open("a", encoding="utf-8")
        cmd = command or [
            executable or shutil.which("codex") or "codex",
            "app-server",
            "--stdio",
            "-c",
            "features.shell_tool=false",
            "-c",
            "features.apply_patch_freeform=false",
            "-c",
            "features.apps=false",
            "-c",
            "mcp_servers={}",
        ]
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            universal_newlines=True,
            bufsize=1,
        )
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        try:
            self.call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "dingtalk-ears",
                        "title": "钉钉消息助手",
                        "version": __version__,
                    }
                },
            )
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def send(self, value):
        with self.lock:
            self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def read(self):
        try:
            for line in self.process.stdout:
                value = json.loads(line)
                if "method" in value and "id" in value:
                    method = value["method"]
                    if method == "item/permissions/requestApproval":
                        result = {"permissions": {}, "scope": "turn"}
                    elif method.endswith("requestApproval"):
                        result = {"decision": "decline"}
                    elif method == "mcpServer/elicitation/request":
                        result = {"action": "decline", "content": None}
                    else:
                        self.send(
                            {
                                "id": value["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "Tools and permission grants are not enabled in the draft generator",
                                },
                            }
                        )
                        continue
                    self.send({"id": value["id"], "result": result})
                elif "id" in value:
                    with self.lock:
                        target = self.pending.get(value["id"])
                    if target:
                        target.put(value)
                else:
                    self.events.put(value)
        except Exception as error:
            self.events.put({"method": "client/error", "params": {"error": str(error)}})
        finally:
            with self.lock:
                for target in self.pending.values():
                    target.put({"error": {"message": "App Server 连接已关闭"}})

    def call(self, method, params, timeout=45):
        with self.lock:
            self.seq += 1
            ident = self.seq
            target = queue.Queue()
            self.pending[ident] = target
            self.send({"id": ident, "method": method, "params": params})
        try:
            value = target.get(timeout=timeout)
            if "error" in value:
                raise RuntimeError(str(value["error"].get("message", "App Server error"))[:400])
            return value["result"]
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def thread(self, existing):
        if existing in self.loaded_threads:
            return existing
        params = {
            "cwd": str(self.cwd),
            "sandbox": "read-only",
            "approvalPolicy": "untrusted",
            "baseInstructions": SYSTEM_INSTRUCTIONS,
            "developerInstructions": SYSTEM_INSTRUCTIONS,
            "config": {
                "features.shell_tool": False,
                "features.apply_patch_freeform": False,
                "features.apps": False,
                "mcp_servers": {},
            },
        }
        if existing:
            params["threadId"] = existing
        try:
            result = self.call("thread/resume" if existing else "thread/start", params)
        except RuntimeError as error:
            if not existing or "already has an active writer" not in str(error):
                raise
            # Preserve committed conversation history instead of starting a blank session.
            result = self.call("thread/fork", params)
        ident = result["thread"]["id"]
        self.loaded_threads.add(ident)
        return ident

    def generate(self, thread_id, data, timeout=180):
        result = self.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": json.dumps(dict(data, context=self.context), ensure_ascii=False),
                    }
                ],
                "outputSchema": RESULT_SCHEMA,
            },
        )
        turn_id = result["turn"]["id"]
        end = time.monotonic() + timeout
        text = ""
        while time.monotonic() < end:
            if self.process.poll() is not None:
                raise RuntimeError("App Server 进程已退出")
            try:
                event = self.events.get(timeout=1)
            except queue.Empty:
                continue
            params = event.get("params", {})
            if event.get("method") == "client/error":
                raise RuntimeError(params["error"])
            if params.get("threadId") != thread_id:
                continue
            if event.get("method") == "item/completed" and params.get("turnId") == turn_id:
                item = params.get("item", {})
                if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                    text = item.get("text", "")
            if (
                event.get("method") == "turn/completed"
                and params.get("turn", {}).get("id") == turn_id
            ):
                turn = params["turn"]
                if turn.get("status") != "completed":
                    raise RuntimeError(
                        "Codex 本轮未完成：" + str(turn.get("error") or turn.get("status"))[:300]
                    )
                if not text:
                    for item in turn.get("items", []):
                        if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                            text = item.get("text", "")
                return validate_result(text)
        try:
            self.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10)
        except Exception:
            pass
        raise RuntimeError("Codex 生成超时，原消息已保留，可手动重试")

    def is_alive(self):
        """检查底层子进程是否仍在正常运行"""
        return self.process.poll() is None

    def close(self):
        if self.process.poll() is None:
            try:
                for thread_id in list(self.loaded_threads):
                    self.seq += 1
                    self.send(
                        {
                            "id": self.seq,
                            "method": "thread/unsubscribe",
                            "params": {"threadId": thread_id},
                        }
                    )
                self.process.stdin.close()
                self.process.wait(timeout=8)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.log.close()
        self.reader.join(timeout=2)
        for stream in [self.process.stdin, self.process.stdout]:
            if stream:
                stream.close()
