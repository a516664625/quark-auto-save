import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))
import quark_auto_save as sdk
from source_recovery import task_fingerprint


def task(name="遮天"):
    return {"taskname": name, "savepath": "/追更/" + name,
            "shareurl": "https://pan.quark.cn/s/old123456789",
            "pattern": "S01E", "source_recovery": {"enabled": True, "title": name}}


def switch(value, *_args, **_kwargs):
    before = copy.deepcopy(value)
    fingerprint = task_fingerprint(value)
    value["shareurl"] = "https://pan.quark.cn/s/new123456789"
    value.pop("shareurl_ban", None)
    if _kwargs.get("persist") and not _kwargs["persist"](value, fingerprint):
        value.clear()
        value.update(before)
        return {"changed": False, "switched": False, "status": "conflict", "message": "conflict"}
    return {"changed": True, "switched": True, "status": "switched", "message": "ok",
            "previous_fingerprint": fingerprint, "current_source": {"health": "healthy"}}


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "config.json")
        self.cfg = {"tasklist": [task()], "cookie": ["test-cookie"],
                    "plugins": {}, "webui": {"username": "test", "password": "test"},
                    "crontab": "0 8 * * *"}
        sdk.Config.write_json(self.path, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_runtime_preserves_gui_changes_order_and_deleted_tasks(self):
        before = copy.deepcopy(self.cfg)
        before["tasklist"].append(task("仙逆"))
        updated = copy.deepcopy(before)
        switch(updated["tasklist"][0])
        updated["plugins"] = {"a": {"token": "runtime"}}
        current = copy.deepcopy(before)
        current["tasklist"] = [current["tasklist"][1], current["tasklist"][0]]
        current["tasklist"][0]["pattern"] = "new rule"
        current["plugins"] = {"b": {"setting": "gui"}}
        sdk.Config.write_json(self.path, current)
        sdk.Config.merge_run(self.path, before, updated)
        actual = sdk.Config.read_json(self.path)
        self.assertEqual([t["taskname"] for t in actual["tasklist"]], ["仙逆", "遮天"])
        self.assertEqual(actual["tasklist"][0]["pattern"], "new rule")
        self.assertEqual(actual["tasklist"][1]["shareurl"], updated["tasklist"][0]["shareurl"])
        self.assertEqual(actual["plugins"], {"a": {"token": "runtime"}, "b": {"setting": "gui"}})
        actual["tasklist"].pop()
        sdk.Config.write_json(self.path, actual)
        sdk.Config.merge_run(self.path, before, updated)
        self.assertEqual(len(sdk.Config.read_json(self.path)["tasklist"]), 1)

    def test_recovery_conflict_stops_and_cannot_reappear_at_final_save(self):
        before = copy.deepcopy(self.cfg)
        runtime = copy.deepcopy(before)
        current = copy.deepcopy(before)
        current["tasklist"][0]["pattern"] = "new rule"
        sdk.Config.write_json(self.path, current)
        with patch("source_recovery.resolve_task", side_effect=switch):
            result = sdk.Quark("test").recover_source(runtime["tasklist"][0],
                                                     config=runtime, config_path=self.path)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(runtime, before)
        sdk.Config.merge_run(self.path, before, runtime)
        self.assertEqual(sdk.Config.read_json(self.path), current)

    def test_single_task_environment_persists_replacement(self):
        env_task = copy.deepcopy(self.cfg["tasklist"][0])
        def save(account, tasks):
            account.recover_source(tasks[0])
        with patch.dict(os.environ, {"TASKLIST": json.dumps([env_task])}), \
                patch.object(sys, "argv", ["qas", self.path]), \
                patch.object(sdk.Config, "breaking_change_update"), \
                patch.object(sdk, "verify_account", side_effect=lambda a: setattr(a, "is_active", True)), \
                patch.object(sdk, "do_save", side_effect=save), \
                patch("source_recovery.resolve_task", side_effect=switch):
            sdk.main()
        self.assertIn("new123456789", sdk.Config.read_json(self.path)["tasklist"][0]["shareurl"])

    def test_sdk_hook_precedes_banned_link_early_return(self):
        value = task()
        value["shareurl_ban"] = "old failure"
        account = sdk.Quark("test")
        def recover(t, **_kwargs):
            t.pop("shareurl_ban")
            return {"status": "healthy", "message": "ok", "current_source": {"health": "healthy"}}
        with patch.object(account, "recover_source", side_effect=recover) as hook, \
                patch.object(account, "get_stoken", return_value={"status": 500}) as stoken:
            account.do_save_task(value)
        hook.assert_called_once()
        stoken.assert_called_once()

    def test_atomic_failure_leaves_original_file(self):
        with patch.object(os, "replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                sdk.Config.write_json(self.path, {"bad": True})
        self.assertEqual(sdk.Config.read_json(self.path), self.cfg)

    def test_enabled_policy_transfers_only_target_season_and_start_episode(self):
        value = task()
        value["source_recovery"].update(season=1, min_episode=183)
        account = sdk.Quark("test")
        account.savepath_fid[value["savepath"]] = "target"
        names = ["S01E182.mp4", "S02E184.mp4", "S01E184.trailer.mp4", "S01E184.mp4"]
        rows = [{"fid": str(i), "file_name": n, "dir": False, "share_fid_token": "test"}
                for i, n in enumerate(names)]
        with patch.object(account, "get_detail", return_value={"data": {"list": rows}}), \
                patch.object(account, "ls_dir", return_value={"data": {"list": []}}), \
                patch.object(account, "save_file", return_value={"code": 0, "data": {"task_id": "test"}}) as save, \
                patch.object(account, "query_task", return_value={"code": 0, "data": {"save_as": {"save_as_top_fids": ["new"]}}}):
            account.dir_check_and_save(value, "test", "test", "source")
        self.assertEqual(save.call_args.args[0], ["3"])

    def test_web_revision_and_authenticated_async_status(self):
        run = importlib.import_module("run")
        class Deferred:
            def submit(self, fn):
                self.fn = fn
        deferred = Deferred()
        with patch.object(run, "CONFIG_PATH", self.path), \
                patch.object(run, "config_data", copy.deepcopy(self.cfg)), \
                patch.object(run, "recovery_jobs", {}), \
                patch.object(run, "recovery_executor", deferred), \
                patch.object(run, "reload_tasks", return_value=True):
            client = run.app.test_client()
            self.assertEqual(client.post("/source_recovery", json={"task_index": 0, "mode": "search"}).status_code, 401)
            with client.session_transaction() as session:
                session["token"] = run.get_login_token()
            revision = client.get("/data").json["data"]["config_revision"]
            updated = copy.deepcopy(self.cfg)
            switch(updated["tasklist"][0])
            sdk.Config.write_json(self.path, updated)
            response = client.post("/update", json=self.cfg, headers={"X-Config-Revision": revision})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(sdk.Config.read_json(self.path), updated)
            body = {"task_index": 0, "mode": "search"}
            revision = client.get("/data").json["data"]["config_revision"]
            headers = {"X-Config-Revision": revision}
            self.assertEqual(client.post("/source_recovery", json=body, headers=headers).status_code, 202)
            self.assertEqual(client.post("/source_recovery", json=body, headers=headers).status_code, 409)
            self.assertEqual(client.get("/source_recovery_status?task_index=0").json["data"]["state"], "searching")
            with patch.object(sdk.Quark, "recover_source", side_effect=ValueError("credential must never leak")):
                deferred.fn()
            status = client.get("/source_recovery_status?task_index=0")
            self.assertEqual(status.json["data"]["state"], "error")
            self.assertNotIn("credential must never leak", status.text)


if __name__ == "__main__":
    unittest.main()
