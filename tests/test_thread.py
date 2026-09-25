import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mailtrace.parsing import build_thread_relations, split_batch_headers  # noqa: E402
from mailtrace.parsing.sample import SAMPLE_HEADERS_THREAD  # noqa: E402

try:  # Flask 相关测试在未安装 Flask 时跳过（解析核心仅需标准库）
    from app import create_app
except ImportError:  # pragma: no cover
    create_app = None


def _mail(message_id=None, in_reply_to=None, references=None, subject="demo"):
    """构造一封仅含会话关系字段的邮件头部。"""
    lines = []
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}")
    if in_reply_to is not None:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references is not None:
        lines.append(f"References: {references}")
    lines.append(f"Subject: {subject}")
    return "\n".join(lines) + "\n"


def _relation(result, index):
    return result["messages"][index - 1]["relation"]


class ExtractionTests(unittest.TestCase):
    def test_extracts_angle_bracket_ids_from_three_fields(self):
        result = build_thread_relations([
            _mail("<a@x>", in_reply_to="<p@x>", references="<r1@x> <r2@x>"),
            _mail("<b@x>"),
        ])
        msg = result["messages"][0]
        self.assertEqual(msg["message_ids"], ["<a@x>"])
        self.assertEqual(msg["in_reply_to_ids"], ["<p@x>"])
        self.assertEqual(msg["references_ids"], ["<r1@x>", "<r2@x>"])

    def test_folded_references_are_unfolded(self):
        result = build_thread_relations([
            _mail("<a@x>", references="<r1@x>\n\t<r2@x>\n\t<r3@x>"),
            _mail("<b@x>"),
        ])
        self.assertEqual(
            result["messages"][0]["references_ids"],
            ["<r1@x>", "<r2@x>", "<r3@x>"],
        )

    def test_field_names_are_case_insensitive(self):
        result = build_thread_relations([
            _mail("<a@x>"),
            "MESSAGE-ID: <B@x>\nIN-REPLY-TO: <a@x>\nSubject: y\n",
        ])
        self.assertEqual(_relation(result, 2)["status"], "linked")
        self.assertEqual(_relation(result, 2)["parent_index"], 1)

    def test_ids_inside_paren_comments_are_ignored(self):
        result = build_thread_relations([
            _mail("<a@x>"),
            _mail("<b@x>", in_reply_to="(see <fake@x>) <a@x>"),
        ])
        self.assertEqual(result["messages"][1]["in_reply_to_ids"], ["<a@x>"])
        self.assertEqual(_relation(result, 2)["parent_index"], 1)

    def test_non_angle_bracket_in_reply_to_falls_back_to_references(self):
        # In-Reply-To 无尖括号 ID 时视为不可用，取 References 末项
        result = build_thread_relations([
            _mail("<a@x>"),
            _mail("<b@x>", in_reply_to="not-an-id", references="<a@x>"),
        ])
        relation = _relation(result, 2)
        self.assertEqual(relation["basis"], "references_last")
        self.assertEqual(relation["status"], "linked")
        self.assertEqual(relation["parent_index"], 1)

    def test_same_message_id_repeated_within_one_mail_is_not_duplicate(self):
        # 同一封内重复声明同一 ID（去重），第三封引用它可正常连边
        headers = (
            "Message-ID: <a@x>\nMessage-ID: <a@x>\nSubject: x\n"
        )
        result = build_thread_relations([
            headers,
            _mail("<b@x>", in_reply_to="<a@x>"),
        ])
        self.assertEqual(result["messages"][0]["message_ids"], ["<a@x>"])
        self.assertEqual(_relation(result, 2)["status"], "linked")
        self.assertEqual(result["ambiguous_count"], 0)


class ParentResolutionTests(unittest.TestCase):
    def test_in_reply_to_single_id_is_parent(self):
        result = build_thread_relations([
            _mail("<a@x>"),
            _mail("<b@x>", in_reply_to="<a@x>"),
        ])
        relation = _relation(result, 2)
        self.assertEqual(relation["basis"], "in_reply_to")
        self.assertEqual(relation["status"], "linked")
        self.assertEqual(relation["parent_index"], 1)
        self.assertEqual(result["linked_count"], 1)

    def test_references_last_used_when_in_reply_to_missing(self):
        result = build_thread_relations([
            _mail("<a@x>"),
            _mail("<b@x>", in_reply_to="<a@x>"),
            _mail("<c@x>", references="<a@x> <b@x>"),
        ])
        relation = _relation(result, 3)
        self.assertEqual(relation["basis"], "references_last")
        self.assertEqual(relation["parent_id"], "<b@x>")
        self.assertEqual(relation["parent_index"], 2)

    def test_in_reply_to_wins_over_references(self):
        # In-Reply-To 唯一 ID 时优先，不看 References 末项
        result = build_thread_relations([
            _mail("<a@x>"),
            _mail("<b@x>"),
            _mail("<c@x>", in_reply_to="<a@x>", references="<a@x> <b@x>"),
        ])
        relation = _relation(result, 3)
        self.assertEqual(relation["basis"], "in_reply_to")
        self.assertEqual(relation["parent_index"], 1)

    def test_external_parent_is_marked_external_not_linked(self):
        result = build_thread_relations([
            _mail("<a@x>", in_reply_to="<gone@elsewhere>"),
            _mail("<b@x>"),
        ])
        relation = _relation(result, 1)
        self.assertEqual(relation["status"], "external")
        self.assertIsNone(relation["parent_index"])
        self.assertEqual(result["external_ref_count"], 1)
        # 外部引用的邮件作为单节点树根，并标注外部父 ID
        self.assertEqual(result["thread_count"], 1)
        self.assertEqual(
            result["threads"][0]["external_parent_id"], "<gone@elsewhere>"
        )

    def test_no_parent_fields_is_orphan(self):
        result = build_thread_relations([_mail("<a@x>"), _mail("<b@x>")])
        for index in (1, 2):
            relation = _relation(result, index)
            self.assertEqual(relation["status"], "none")
            self.assertEqual(relation["basis"], "none")
        self.assertEqual(result["orphans"], [1, 2])
        self.assertEqual(result["orphan_count"], 2)
        self.assertEqual(result["threads"], [])


class AmbiguityTests(unittest.TestCase):
    def test_in_reply_to_multiple_ids_is_ambiguous(self):
        result = build_thread_relations([
            _mail("<a@x>"),
            _mail("<b@x>"),
            _mail("<c@x>", in_reply_to="<a@x> <b@x>"),
        ])
        relation = _relation(result, 3)
        self.assertEqual(relation["status"], "ambiguous")
        self.assertEqual(relation["ambiguous_reason"], "in_reply_to_multiple")
        self.assertIsNone(relation["parent_index"])
        self.assertEqual(result["linked_count"], 0)
        self.assertEqual(result["ambiguous_count"], 1)
        self.assertIn("不强行连边", relation["detail"])

    def test_duplicate_message_id_parent_is_ambiguous(self):
        # 第 1、2 封声明同一 Message-ID，第 3 封引用它 → 无法确定父
        result = build_thread_relations([
            _mail("<dup@x>"),
            _mail("<dup@x>"),
            _mail("<c@x>", in_reply_to="<dup@x>"),
        ])
        relation = _relation(result, 3)
        self.assertEqual(relation["status"], "ambiguous")
        self.assertEqual(relation["ambiguous_reason"], "duplicate_message_id")
        self.assertIsNone(relation["parent_index"])
        self.assertEqual(result["linked_count"], 0)
        self.assertIn("重复 Message-ID", relation["detail"])

    def test_two_message_cycle_is_ambiguous(self):
        result = build_thread_relations([
            _mail("<a@x>", in_reply_to="<b@x>"),
            _mail("<b@x>", in_reply_to="<a@x>"),
        ])
        for index in (1, 2):
            relation = _relation(result, index)
            self.assertEqual(relation["status"], "ambiguous")
            self.assertEqual(relation["ambiguous_reason"], "cycle")
            self.assertIsNone(relation["parent_index"])
        self.assertEqual(result["linked_count"], 0)
        self.assertEqual(result["ambiguous_count"], 2)
        self.assertEqual(result["threads"], [])

    def test_self_reference_is_cycle(self):
        result = build_thread_relations([
            _mail("<a@x>", in_reply_to="<a@x>"),
            _mail("<b@x>"),
        ])
        relation = _relation(result, 1)
        self.assertEqual(relation["status"], "ambiguous")
        self.assertEqual(relation["ambiguous_reason"], "cycle")
        self.assertEqual(result["linked_count"], 0)

    def test_cycle_breaks_only_cycle_edges(self):
        # 1 <-> 2 成环；第 3 封以第 2 封为父（该边不在环上，保留）
        result = build_thread_relations([
            _mail("<a@x>", in_reply_to="<b@x>"),
            _mail("<b@x>", in_reply_to="<a@x>"),
            _mail("<c@x>", in_reply_to="<b@x>"),
        ])
        self.assertEqual(_relation(result, 1)["ambiguous_reason"], "cycle")
        self.assertEqual(_relation(result, 2)["ambiguous_reason"], "cycle")
        relation3 = _relation(result, 3)
        self.assertEqual(relation3["status"], "linked")
        self.assertEqual(relation3["parent_index"], 2)
        # 第 2 封断开父边后成为第 3 封所在树的根
        self.assertEqual(result["thread_count"], 1)
        tree = result["threads"][0]
        self.assertEqual(tree["root_index"], 2)
        self.assertEqual(tree["size"], 2)


class ThreadTreeTests(unittest.TestCase):
    def test_three_level_thread_tree(self):
        result = build_thread_relations([
            _mail("<a@x>"),
            _mail("<b@x>", in_reply_to="<a@x>"),
            _mail("<c@x>", references="<a@x> <b@x>"),
        ])
        self.assertEqual(result["thread_count"], 1)
        thread = result["threads"][0]
        self.assertEqual(thread["root_index"], 1)
        self.assertEqual(thread["size"], 3)
        self.assertIsNone(thread["external_parent_id"])
        root = thread["tree"]
        self.assertEqual(root["index"], 1)
        self.assertEqual(root["message_id"], "<a@x>")
        self.assertEqual([c["index"] for c in root["children"]], [2])
        self.assertEqual(
            [c["index"] for c in root["children"][0]["children"]], [3]
        )
        self.assertEqual(result["orphans"], [])

    def test_children_are_grouped_on_parent(self):
        result = build_thread_relations([
            _mail("<a@x>"),
            _mail("<b@x>", in_reply_to="<a@x>"),
            _mail("<c@x>", in_reply_to="<a@x>"),
        ])
        self.assertEqual(result["messages"][0]["children"], [2, 3])
        thread = result["threads"][0]
        self.assertEqual(thread["size"], 3)
        self.assertEqual(
            [c["index"] for c in thread["tree"]["children"]], [2, 3]
        )

    def test_larger_thread_sorts_first(self):
        result = build_thread_relations([
            _mail("<solo@x>", in_reply_to="<ext@y>"),  # 外部引用单节点树
            _mail("<a@x>"),
            _mail("<b@x>", in_reply_to="<a@x>"),
        ])
        self.assertEqual(result["thread_count"], 2)
        self.assertEqual(result["threads"][0]["root_index"], 2)
        self.assertEqual(result["threads"][0]["size"], 2)
        self.assertEqual(result["threads"][1]["root_index"], 1)
        self.assertEqual(result["threads"][1]["size"], 1)

    def test_subject_is_collected_for_display(self):
        result = build_thread_relations([
            _mail("<a@x>", subject="周会纪要"),
            _mail("<b@x>", in_reply_to="<a@x>"),
        ])
        self.assertEqual(result["messages"][0]["subject"], "周会纪要")
        self.assertEqual(
            result["threads"][0]["tree"]["subject"], "周会纪要"
        )


class SampleThreadTests(unittest.TestCase):
    def test_sample_splits_into_six(self):
        parts = split_batch_headers(SAMPLE_HEADERS_THREAD)
        self.assertEqual(len(parts), 6)

    def test_sample_thread_relations(self):
        parts = split_batch_headers(SAMPLE_HEADERS_THREAD)
        result = build_thread_relations(parts)
        self.assertEqual(result["message_count"], 6)
        # 第 1→2→3 封一棵树；第 4 封外部引用单节点树
        self.assertEqual(result["thread_count"], 2)
        self.assertEqual(result["linked_count"], 2)
        self.assertEqual(result["external_ref_count"], 1)
        self.assertEqual(result["ambiguous_count"], 1)
        self.assertEqual(result["orphans"], [5, 6])

        main = result["threads"][0]
        self.assertEqual(main["root_index"], 1)
        self.assertEqual(main["size"], 3)
        self.assertEqual(
            [c["index"] for c in main["tree"]["children"]], [2]
        )
        # 第 3 封缺少 In-Reply-To，取折行 References 末项 <m2@example.net>
        relation3 = _relation(result, 3)
        self.assertEqual(relation3["basis"], "references_last")
        self.assertEqual(relation3["parent_id"], "<m2@example.net>")
        self.assertEqual(relation3["parent_index"], 2)
        # 第 4 封外部引用
        self.assertEqual(_relation(result, 4)["status"], "external")
        external = result["threads"][1]
        self.assertEqual(
            external["external_parent_id"], "<outside-2026@elsewhere.example>"
        )
        # 第 5 封 In-Reply-To 两个 ID → 歧义；第 6 封无父字段
        self.assertEqual(
            _relation(result, 5)["ambiguous_reason"], "in_reply_to_multiple"
        )
        self.assertEqual(_relation(result, 6)["status"], "none")


@unittest.skipIf(create_app is None, "未安装 Flask，跳过 API 测试")
class ThreadApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    def test_api_thread_ok(self):
        parts = split_batch_headers(SAMPLE_HEADERS_THREAD)
        resp = self.client.post("/api/thread-relations", json={"headers": parts})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["message_count"], 6)
        self.assertEqual(data["thread_count"], 2)
        self.assertEqual(data["ambiguous_count"], 1)

    def test_api_thread_requires_json(self):
        resp = self.client.post(
            "/api/thread-relations",
            data={"headers": "x"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_api_thread_rejects_non_list(self):
        resp = self.client.post(
            "/api/thread-relations", json={"headers": "not-a-list"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("字符串数组", resp.get_json()["error"])

    def test_api_thread_rejects_non_string_items(self):
        resp = self.client.post(
            "/api/thread-relations", json={"headers": ["a", 123]}
        )
        self.assertEqual(resp.status_code, 400)

    def test_api_thread_requires_at_least_two(self):
        resp = self.client.post(
            "/api/thread-relations", json={"headers": ["Message-ID: <a@x>"]}
        )
        self.assertEqual(resp.status_code, 400)

    def test_api_thread_rejects_blank_items(self):
        resp = self.client.post(
            "/api/thread-relations",
            json={"headers": ["Message-ID: <a@x>", "   "]},
        )
        self.assertEqual(resp.status_code, 400)

    def test_api_thread_oversize_returns_400(self):
        app = create_app()
        app.config["MAX_INPUT_BYTES"] = 64
        client = app.test_client()
        resp = client.post(
            "/api/thread-relations",
            json={"headers": ["Message-ID: <a@x>", "Subject: " + "x" * 200]},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("上限", resp.get_json()["error"])

    def test_thread_form_page(self):
        resp = self.client.post(
            "/thread-relations", data={"headers_thread": SAMPLE_HEADERS_THREAD}
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("会话关系结果", html)
        self.assertIn("会话树", html)
        self.assertIn("孤立邮件", html)
        self.assertIn("逐封关联依据", html)
        self.assertIn("外部引用", html)

    def test_thread_form_empty_shows_error(self):
        resp = self.client.post("/thread-relations", data={"headers_thread": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("未识别到任何邮件头部", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
