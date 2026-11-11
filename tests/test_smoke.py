import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB
from tools.memory_tool import load_on_disk_store, memory_tool
from tools.skill_manager_tool import skill_manage


ROOT = Path(__file__).resolve().parents[1]


class SmokeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="memdiffs-smoke-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        env = patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        token = set_hermes_home_override(self.home)
        self.addCleanup(reset_hermes_home_override, token)
        spec = importlib.util.spec_from_file_location("memdiffs_smoke", ROOT / "__init__.py")
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        self.backend = package._core(reload=True)
        self.capture = self.backend.MemDiffs()
        self.db = SessionDB(db_path=self.home / "state.db")
        self.addCleanup(self.db.close)
        self.db.create_session("chat", "cli")

    def save_memory(self, text="Prefers tea.", turn="save"):
        calls = []
        def execute(args):
            calls.append(memory_tool(store=load_on_disk_store(), **args))
            return calls[-1]
        result = self.capture.tool_execution(
            next_call=execute, tool_name="memory",
            args={"action": "add", "target": "memory", "content": text},
            session_id="chat", turn_id=turn, tool_call_id=turn)
        self.assertEqual(len(calls), 1)
        self.assertTrue(json.loads(result)["success"], result)
        return result, calls[0]

    def test_memory_diff_and_request_cleanup(self):
        result, original = self.save_memory()
        data = json.loads(result)
        self.assertIn("+Prefers tea.", data["inline_diff"])
        status, body = self.backend.api_detail(data["mem_diff"], "chat")
        self.assertEqual(status, 200)
        self.assertIn("+Prefers tea.", body["receipt"]["files"][0]["diff"])
        request = {"messages": [{"role": "tool", "content": result}],
                   "keep": {"value": "unchanged"}}
        clean = self.capture.llm_request(request=request)["request"]
        self.assertEqual(clean, {"messages": [{"role": "tool", "content": original}],
                                 "keep": {"value": "unchanged"}})
        self.assertEqual(request["messages"][0]["content"], result)

    def test_skill_batch_captures_both_files(self):
        content = "---\nname: smoke\ndescription: Smoke test skill.\n---\n\n# Smoke\n"
        args = {"operations": [
            {"action": "create", "name": "smoke", "content": content},
            {"action": "write_file", "name": "smoke", "file_path": "references/example.md",
             "file_content": "Example text.\n"}]}
        result = self.capture.tool_execution(
            next_call=lambda actual: skill_manage(action="", name="", **actual),
            tool_name="skill_manage", args=args,
            session_id="chat", turn_id="skill", tool_call_id="skill")
        data = json.loads(result)
        self.assertTrue(data["success"], result)
        status, body = self.backend.api_detail(data["mem_diff"], "chat")
        self.assertEqual(status, 200)
        files = body["receipt"]["files"]
        self.assertEqual(len(files), 2)
        self.assertTrue(any(f["path"].endswith("/SKILL.md") for f in files))
        self.assertTrue(any(f["path"].endswith("/references/example.md") for f in files))
        self.assertIn("+Example text.", data["inline_diff"])

    def test_receipts_do_not_cross_scope_or_survive_deletion(self):
        result, _ = self.save_memory()
        receipt_id = json.loads(result)["mem_diff"]
        self.assertEqual(self.backend.api_detail(receipt_id, "other-chat")[0], 404)
        with tempfile.TemporaryDirectory(prefix="memdiffs-other-") as other:
            token = set_hermes_home_override(Path(other))
            try:
                self.assertEqual(self.backend.api_detail(receipt_id, "chat")[0], 404)
            finally:
                reset_hermes_home_override(token)
        self.capture.on_session_delete(session_id="chat", reason="delete")
        self.assertEqual(self.backend.api_detail(receipt_id, "chat")[0], 404)
        late, original = self.save_memory("Late write.", turn="late")
        self.assertEqual(late, original)
        self.assertNotIn("mem_diff", json.loads(late))

    def test_review_notice_waits_for_foreground_and_replays_once(self):
        result, _ = self.save_memory(turn="background")
        receipt_id = json.loads(result)["mem_diff"]
        self.assertIsNone(self.capture.transform_llm_output(
            response_text="Review finished.", session_id="chat", turn_id="background"))
        self.capture.pre_llm_call(session_id="chat", turn_id="foreground")
        reply = self.capture.transform_llm_output(
            response_text="Done.", session_id="chat", turn_id="foreground")
        self.assertEqual(reply.count("::mem-diffs-review{"), 1)
        group_id = reply.split('::mem-diffs-review{id="')[1].split('"}')[0]
        status, body = self.backend.api_review_group(group_id, "chat")
        self.assertEqual(status, 200)
        self.assertEqual(body["group"]["receipt_ids"], [receipt_id])
        self.assertIsNone(self.capture.transform_llm_output(
            response_text=reply, session_id="chat", turn_id="foreground"))
        self.capture.pre_llm_call(session_id="chat", turn_id="next")
        self.assertIsNone(self.capture.transform_llm_output(
            response_text="Next reply.", session_id="chat", turn_id="next"))


if __name__ == "__main__":
    unittest.main()
