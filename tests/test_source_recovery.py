import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import source_recovery as recovery
from quark_auto_save import Quark


OLD_URL = "https://pan.quark.cn/s/old12345"
NEW_URL = "https://pan.quark.cn/s/new12345"
NOW = 1_900_000_000


def success(files=None):
    return {"status": 200, "code": 0, "data": {"list": files or []}}


def video(episode=185, season=1, name=None):
    return {"file_name": name or f"S{season:02d}E{episode}.4K.mp4", "dir": False, "fid": "a" * 32, "size": 1024, "obj_category": "video"}


class Account:
    extract_url = Quark.extract_url

    def __init__(self, shares=None, folder_name="遮天"):
        self.shares = shares or {
            "old12345": {"status": 400, "code": 41002, "message": "分享已取消"},
            "new12345": [video()],
        }
        self.calls = []
        self.folder_name = folder_name

    def get_stoken(self, share, password):
        self.calls.append(("token", share))
        files = self.shares.get(share)
        if isinstance(files, dict) and "status" in files:
            return files
        return {"status": 200, "code": 0, "data": {"stoken": "SECRET_SHARE_TOKEN"}}

    def get_detail(self, share, token, fid, **kwargs):
        self.calls.append(("detail", share, fid))
        files = self.shares.get(share, [])
        if share == "new12345" and str(fid) == "0" and isinstance(files, list):
            return success([{"file_name": self.folder_name, "fid": "b" * 32, "dir": True}])
        result = success(files.get(str(fid), []) if isinstance(files, dict) else files)
        if share == "new12345" and str(fid) == "b" * 32:
            result["data"]["full_path"] = [{"fid": "b" * 32, "file_name": self.folder_name}]
        return result


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.temp.name) / "recovery.json")
        self.task = {
            "taskname": "遮天", "shareurl": OLD_URL, "shareurl_ban": "分享已取消",
            "savepath": "/自动追更/遮天/Season 1",
            "pattern": r"(?i)^(S\d{2}E\d{1,4})\.(4K|2160p|1080p)\.(mp4|mkv)$",
            "replace": r"遮天.\1.\2.\3", "update_subdir": "", "other": {"keep": True},
            "source_recovery": {"enabled": True, "title": "遮天", "season": 1, "min_episode": 184, "stale_hours": 24},
        }
        self.config = {}
        self.clock = patch.object(recovery.time, "time", return_value=NOW)
        self.clock.start()
        self.search = patch.object(recovery, "search_sources", return_value=([{"shareurl": NEW_URL, "taskname": "遮天 4K 更新至185集", "source": "CloudSaver"}], []))
        self.searcher = self.search.start()

    def tearDown(self):
        self.search.stop()
        self.clock.stop()
        self.temp.cleanup()

    def resolve(self, account=None, **kwargs):
        return recovery.resolve_task(self.task, self.config, account or Account(), self.state, **kwargs)

    def test_cancelled_share_switches_only_source_and_restores_ban(self):
        original = copy.deepcopy(self.task)
        result = self.resolve(expected_episode=185)
        self.assertTrue(result["switched"])
        self.assertTrue(self.task["shareurl"].startswith(NEW_URL + "#/list/share/"))
        self.assertNotIn("shareurl_ban", self.task)
        self.assertEqual(result["previous_fingerprint"], recovery.task_fingerprint(original))
        for key, value in original.items():
            if key not in {"shareurl", "shareurl_ban"}:
                self.assertEqual(self.task[key], value)
        self.assertEqual(result["current_source"]["health"], "healthy")
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o600)

    def test_transient_or_permission_errors_never_trigger_search(self):
        for response, expected in [
            ({"status": 500, "message": "timeout"}, "network_error"),
            ({"status": 403, "message": "分享已取消"}, "permission_error"),
            ({"status": 401, "message": "Cookie invalid"}, "account_error"),
            ({"status": 429}, "rate_limited"),
            ({"status": 400, "message": "空间不足"}, "space_error"),
        ]:
            with self.subTest(expected=expected):
                result = self.resolve(Account({"old12345": response}))
                self.assertEqual(result["status"], expected)
                self.assertEqual(self.task["shareurl"], OLD_URL)
        self.searcher.assert_not_called()

    def test_healthy_source_clears_old_ban_without_unnecessary_search(self):
        result = self.resolve(Account({"old12345": [video(184)]}), force=True)
        self.assertEqual(result["status"], "healthy")
        self.assertTrue(result["changed"])
        self.assertFalse(result["switched"])
        self.assertNotIn("shareurl_ban", self.task)
        self.searcher.assert_not_called()

    def test_wrong_season_movie_or_incompatible_rule_is_rejected(self):
        for title, files, reason in [
            ("遮天", [video(season=2)], "季号"),
            ("遮天 剧场版", [video()], "剧场版"),
            ("遮天", [video(name="第185集.mp4")], "保存规则"),
        ]:
            with self.subTest(title=title, reason=reason):
                self.searcher.return_value = ([{"shareurl": NEW_URL, "taskname": title, "source": "PanSou"}], [])
                result = self.resolve(Account({"old12345": {"status": 400, "message": "分享已取消"}, "new12345": files}), force=True)
                self.assertEqual(result["status"], "no_source")
                self.assertIn(reason, result["candidates"][0]["reason"])
                self.assertEqual(self.task["shareurl"], OLD_URL)

    def test_missing_episode_cannot_be_satisfied_by_an_older_or_incomplete_source(self):
        account = Account({"old12345": [video(184)], "new12345": [video(183), video(186)]})
        result = self.resolve(account, expected_episode=185)
        self.assertEqual(result["status"], "no_source")
        self.assertIn("目标集", result["candidates"][0]["reason"])
        self.assertEqual(self.task["shareurl"], OLD_URL)

    def test_stale_source_requires_a_strictly_newer_episode(self):
        account = Account({"old12345": [video(184)], "new12345": [video(184)]})
        self.resolve(account)
        state = recovery._read_state(self.state)
        entry = state["tasks"][recovery.task_key(self.task)]
        entry["_progress_at"] = NOW - 25 * 3600
        recovery._write_state(self.state, state)
        result = self.resolve(account, force=True)
        self.assertEqual(result["status"], "no_source")
        account.shares["new12345"] = [video(185)]
        result = self.resolve(account, force=True)
        self.assertTrue(result["switched"])

    def test_empty_search_does_not_change_source_or_files(self):
        self.searcher.return_value = ([], [])
        result = self.resolve()
        self.assertEqual(result["status"], "no_source")
        self.assertEqual(self.task["shareurl"], OLD_URL)

    def test_network_search_failure_is_distinguished_from_no_results(self):
        self.searcher.return_value = ([], ["CloudSaver 暂不可用"])
        self.assertEqual(self.resolve()["status"], "search_error")

    def test_uncertain_alias_requires_confirmation_without_ai(self):
        self.searcher.return_value = ([{"shareurl": NEW_URL, "taskname": "Ztian 4K", "source": "CloudSaver"}], [])
        result = self.resolve(Account(folder_name="Ztian"))
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(self.task["shareurl"], OLD_URL)

    def test_ai_sees_no_credentials_or_urls_and_cannot_select_out_of_range(self):
        self.task["source_recovery"]["allow_ai"] = True
        self.config["source_recovery_ai"] = {"enabled": True, "base_url": "https://model.example/v1", "model": "small", "api_key": "SECRET_API_KEY"}
        self.searcher.return_value = ([{"shareurl": NEW_URL, "taskname": "Ztian Cookie=SECRET_COOKIE https://pan.quark.cn/s/new12345", "source": "CloudSaver"}], [])
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"choices": [{"message": {"content": '{"candidate_index": 999, "reason": "trust me"}'}}]}).encode()
        with patch.object(recovery.requests, "post", return_value=response) as post:
            result = self.resolve(Account(folder_name="Ztian"))
        payload = json.dumps(post.call_args.kwargs["json"], ensure_ascii=False)
        for secret in ("SECRET_COOKIE", "SECRET_API_KEY", "SECRET_SHARE_TOKEN", OLD_URL, NEW_URL):
            self.assertNotIn(secret, payload)
            self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(self.task["shareurl"], OLD_URL)

    def test_ai_cannot_override_wrong_season_or_year(self):
        self.task["source_recovery"].update(allow_ai=True, year=2023)
        self.config["source_recovery_ai"] = {"enabled": True, "base_url": "https://model.example/v1", "model": "small"}
        self.searcher.return_value = ([{"shareurl": NEW_URL, "taskname": "Ztian 2020", "source": "PanSou"}], [])
        with patch.object(recovery.requests, "post") as post:
            result = self.resolve(Account({"old12345": {"status": 400, "message": "分享已取消"}, "new12345": [video(season=2)]}))
        post.assert_not_called()
        self.assertEqual(result["status"], "no_source")

    def test_search_is_a_preview_and_apply_revalidates_the_files(self):
        account = Account()
        result = self.resolve(account, mode="search")
        candidate = result["candidates"][0]["id"]
        self.assertEqual(result["status"], "candidates_ready")
        self.assertEqual(self.task["shareurl"], OLD_URL)
        account.shares["new12345"] = [video(season=2)]
        result = self.resolve(account, mode="apply", candidate_id=candidate)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(self.task["shareurl"], OLD_URL)

    def test_apply_validated_candidate_succeeds(self):
        result = self.resolve(mode="search")
        candidate = result["candidates"][0]["id"]
        result = self.resolve(mode="apply", candidate_id=candidate)
        self.assertTrue(result["switched"])

    def test_url_fragment_name_cannot_fake_actual_work_identity(self):
        spoof = NEW_URL + "#/list/share/" + "b" * 32 + "-遮天"
        self.searcher.return_value = ([{"shareurl": spoof, "taskname": "遮天 4K", "source": "CloudSaver"}], [])
        result = self.resolve(Account(folder_name="凡人修仙传"))
        self.assertEqual(result["status"], "no_source")
        self.assertIn("其他作品", result["candidates"][0]["reason"])

    def test_directory_label_without_api_evidence_requires_confirmation(self):
        class MissingPathAccount(Account):
            def get_detail(self, *args, **kwargs):
                result = super().get_detail(*args, **kwargs)
                result["data"].pop("full_path", None)
                return result
        spoof = NEW_URL + "#/list/share/" + "b" * 32 + "-遮天"
        self.searcher.return_value = ([{"shareurl": spoof, "taskname": "遮天 4K", "source": "CloudSaver"}], [])
        result = self.resolve(MissingPathAccount(folder_name="Ztian"))
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(self.task["shareurl"], OLD_URL)

    def test_apply_rejects_a_stale_fingerprint(self):
        result = self.resolve(mode="search")
        candidate = result["candidates"][0]["id"]
        self.task["savepath"] = "/changed/by/user"
        result = self.resolve(mode="apply", candidate_id=candidate)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(self.task["shareurl"], OLD_URL)

    def test_concurrent_edit_cancels_switch(self):
        def search(*args):
            self.task["savepath"] = "/changed/during/search"
            return [{"shareurl": NEW_URL, "taskname": "遮天", "source": "PanSou"}], []
        self.searcher.side_effect = search
        result = self.resolve()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(self.task["shareurl"], OLD_URL)
        self.assertEqual(self.task["savepath"], "/changed/during/search")

    def test_state_lock_prevents_api_and_cron_duplicate_work(self):
        with recovery.state_lock(self.state):
            result = self.resolve()
        self.assertEqual(result["status"], "busy")
        self.searcher.assert_not_called()

    def test_share_url_rejects_external_hosts_credentials_ports_and_queries(self):
        for url in ("http://pan.quark.cn/s/new12345", "https://127.0.0.1/s/new12345", "https://pan.quark.cn.evil/s/new12345", "https://user@pan.quark.cn/s/new12345", "https://pan.quark.cn:443/s/new12345", NEW_URL + "?url=http://192.168.1.1", NEW_URL + "\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                recovery.validate_share_url(url)

    def test_number_only_names_do_not_guess_season_mapping(self):
        self.assertEqual(recovery.parse_episode("184.mp4"), (None, 184))
        self.assertEqual(recovery.parse_episode("第十二集.mp4", "第一季"), (1, 12))
        self.assertEqual(recovery.parse_episode("S02E003.mp4"), (2, 3))

    def test_invalid_account_prevents_search_even_when_source_is_cancelled(self):
        account = Account()
        account.is_active = False
        account.get_account_info = lambda: False
        result = self.resolve(account)
        self.assertEqual(result["status"], "account_error")
        self.searcher.assert_not_called()

    def test_default_inactive_flag_does_not_block_a_valid_account(self):
        account = Account()
        account.is_active = False
        account.get_account_info = lambda: {"nickname": "valid"}
        self.assertTrue(self.resolve(account)["switched"])

    def test_request_budget_counts_sdk_pagination_and_sets_timeout(self):
        class PagedAccount(Account):
            def _send_request(self, method, url, **kwargs):
                self.calls.append(kwargs)
                response = requests.Response()
                response.status_code = 200
                response._content = b'{"status":200,"code":0,"data":{"stoken":"hidden"}}'
                return response

            def get_stoken(self, share, password):
                return self._send_request("POST", "https://drive-pc.quark.cn/token").json()

            def get_detail(self, share, token, fid, **kwargs):
                for _ in range(100):
                    self._send_request("GET", "https://drive-pc.quark.cn/detail")
                return success([video()])

        account = PagedAccount()
        result = self.resolve(account)
        self.assertEqual(result["status"], "inspection_limit")
        self.assertEqual(len(account.calls), recovery.MAX_VISITS)
        self.assertTrue(all(call["timeout"] == recovery.TIMEOUT and call["allow_redirects"] is False for call in account.calls))
        self.searcher.assert_not_called()

    def test_search_caption_cannot_disguise_a_different_actual_work(self):
        self.task["pattern"] = r"(?i)^.*?(S\d{2}E\d{1,4})\.(4K)\.(mp4)$"
        self.searcher.return_value = ([{"shareurl": NEW_URL, "taskname": "遮天 4K 更新至200集", "source": "CloudSaver"}], [])
        account = Account({"old12345": {"status": 400, "message": "分享已取消"}, "new12345": [video(name="凡人修仙传.S01E200.4K.mp4")]}, folder_name="凡人修仙传")
        result = self.resolve(account)
        self.assertEqual(result["status"], "no_source")
        self.assertIn("其他作品", result["candidates"][0]["reason"])
        self.assertEqual(self.task["shareurl"], OLD_URL)

    def test_correct_directory_does_not_override_a_different_two_character_video_title(self):
        self.task["pattern"] = r"(?i)^.*?(S\d{2}E\d{1,4})\.(4K)\.(mp4)$"
        account = Account({"old12345": {"status": 400, "message": "分享已取消"}, "new12345": [video(name="仙逆.S01E185.4K.mp4")]}, folder_name="遮天")
        result = self.resolve(account)
        self.assertEqual(result["status"], "no_source")
        self.assertIn("其他作品", result["candidates"][0]["reason"])

    def test_failed_config_cas_does_not_commit_high_water_mark_or_history(self):
        account = Account({"old12345": {"status": 400, "message": "分享已取消"}, "new12345": [video(200)]})
        result = self.resolve(account, persist=lambda task, fingerprint: False)
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(result["changed"])
        self.assertFalse(result["switched"])
        self.assertEqual(result["history"], [])
        self.assertEqual(self.task["shareurl"], OLD_URL)
        self.assertEqual(self.task["shareurl_ban"], "分享已取消")
        state = recovery._read_state(self.state)["tasks"][recovery.task_key(self.task)]
        self.assertNotEqual(state.get("_latest_seen"), 200)
        account.shares["new12345"] = [video(185)]
        self.assertTrue(self.resolve(account, force=True, persist=lambda task, fingerprint: True)["switched"])

    def test_healthy_ban_cleanup_respects_failed_config_cas(self):
        result = self.resolve(Account({"old12345": [video(184)]}), persist=lambda task, fingerprint: False)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(self.task["shareurl_ban"], "分享已取消")
        self.assertFalse(result["changed"])

    def test_elapsed_deadline_prevents_new_network_requests(self):
        with patch.object(recovery.time, "monotonic", return_value=10):
            budget = recovery._Budget()
        with patch.object(recovery.time, "monotonic", return_value=131), self.assertRaises(RuntimeError):
            budget.visit()
        self.assertEqual(budget.used, 0)

    def test_search_caption_cannot_cancel_actual_directory_season_conflict(self):
        self.searcher.return_value = ([{"shareurl": NEW_URL, "taskname": "遮天 第一季 第二季", "source": "CloudSaver"}], [])
        result = self.resolve(Account(folder_name="遮天 Season 2"))
        self.assertEqual(result["status"], "no_source")
        self.assertIn("季号", result["candidates"][0]["reason"])

    def test_cached_ai_decision_cannot_survive_changed_actual_identity(self):
        self.task["source_recovery"]["allow_ai"] = True
        self.config["source_recovery_ai"] = {"enabled": True, "base_url": "https://model.example/v1", "model": "small"}
        self.searcher.return_value = ([{"shareurl": NEW_URL, "taskname": "Ztian", "source": "PanSou"}], [])
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"choices": [{"message": {"content": '{"candidate_index": 0, "reason": "Alias fits"}'}}]}).encode()
        account = Account(folder_name="Ztian")
        with patch.object(recovery.requests, "post", return_value=response):
            result = self.resolve(account, mode="search")
        self.assertEqual(result["status"], "candidates_ready")
        candidate = result["candidates"][0]["id"]
        account.folder_name = "Zother"
        result = self.resolve(account, mode="apply", candidate_id=candidate)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(self.task["shareurl"], OLD_URL)

    def test_empty_source_is_confirmed_before_recovery(self):
        account = Account({"old12345": [], "new12345": [video()]})
        self.assertTrue(self.resolve(account)["switched"])
        self.assertEqual(sum(call[:2] == ("detail", "old12345") for call in account.calls), 2)

    def test_empty_source_followed_by_timeout_is_not_marked_invalid(self):
        class TemporaryEmptyAccount(Account):
            def get_detail(self, share, token, fid, **kwargs):
                if share == "old12345":
                    prior = sum(call[:2] == ("detail", "old12345") for call in self.calls)
                    if prior:
                        return {"status": 500, "message": "timeout"}
                return super().get_detail(share, token, fid, **kwargs)
        account = TemporaryEmptyAccount({"old12345": [], "new12345": [video()]})
        result = self.resolve(account)
        self.assertEqual(result["status"], "network_error")
        self.searcher.assert_not_called()
        self.assertEqual(self.task["shareurl"], OLD_URL)

    def test_deleted_leaf_can_recover_from_verified_same_share_root(self):
        class RebuiltAccount(Account):
            def get_detail(self, share, token, fid, **kwargs):
                self.calls.append(("detail", share, fid))
                if str(fid) == "c" * 32:
                    return {"status": 404, "message": "文件已删除"}
                if str(fid) == "0":
                    return success([{"file_name": "遮天", "dir": True, "fid": "d" * 32}])
                return success([video(185)])
        self.task["shareurl"] = OLD_URL + "#/list/share/" + "c" * 32 + "-旧目录"
        account = RebuiltAccount({"old12345": [video(185)]})
        self.searcher.return_value = ([], [])
        result = self.resolve(account)
        self.assertTrue(result["switched"])
        self.assertIn("d" * 32, self.task["shareurl"])
        self.assertEqual(result["candidates"][0]["source"], "原分享根目录")


if __name__ == "__main__":
    unittest.main()
