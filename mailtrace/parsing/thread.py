"""多封邮件会话关系：按 Message-ID / In-Reply-To / References 还原会话树。

设计要点：
- 复用 :func:`mailtrace.parsing.parser.parse_header_blocks` 的折行展开解析，
  逐封提取 Message-ID、In-Reply-To、References 字段中的尖括号消息 ID
  （提取前先去掉圆括号注释，字段名大小写不敏感）。
- 父邮件判定：In-Reply-To 恰好提取到一个消息 ID 时以它为父；缺少
  In-Reply-To 字段（或其中没有尖括号 ID）时取 References 的**末项**。
- 以下关联一律标为**歧义、不强行连边**：父 ID 在批次内被多封邮件声明
  （重复 Message-ID）、In-Reply-To 含多个消息 ID、关联形成环（含自引用）。
- 父 ID 不在本批次内时不连边，记为**外部引用**并单独展示。
- 输出会话树（根为无批次内父、但有子节点或外部父引用的邮件）、
  孤立邮件（无父边、无子节点、无外部引用）与逐封关联依据。
"""

from __future__ import annotations

import re

from mailtrace.parsing.parser import _strip_paren_comments, parse_header_blocks

# 尖括号消息 ID：<...> 内不含空白与尖括号
_MSG_ID_RE = re.compile(r"<[^<>\s]+>")

# 关联状态
STATUS_LINKED = "linked"        # 已关联批次内父邮件
STATUS_EXTERNAL = "external"    # 父 ID 不在本批次：外部引用
STATUS_AMBIGUOUS = "ambiguous"  # 歧义，不强行连边
STATUS_NONE = "none"            # 无父邮件线索

# 歧义原因
REASON_IN_REPLY_TO_MULTIPLE = "in_reply_to_multiple"
REASON_DUPLICATE_MESSAGE_ID = "duplicate_message_id"
REASON_CYCLE = "cycle"

_BASIS_LABELS = {
    "in_reply_to": "In-Reply-To 唯一消息 ID",
    "references_last": "缺少 In-Reply-To，取 References 末项",
    "none": "无可用父字段",
}

_STATUS_LABELS = {
    STATUS_LINKED: "已关联批次内父邮件",
    STATUS_EXTERNAL: "外部引用（父邮件不在本批次）",
    STATUS_AMBIGUOUS: "歧义（未连边）",
    STATUS_NONE: "无父邮件线索",
}

_REASON_LABELS = {
    REASON_IN_REPLY_TO_MULTIPLE: "In-Reply-To 含多个消息 ID",
    REASON_DUPLICATE_MESSAGE_ID: "重复 Message-ID",
    REASON_CYCLE: "环状引用",
}


def _extract_msg_ids(value: str) -> list[str]:
    """从字段值中提取尖括号消息 ID（先去掉圆括号注释，保持出现顺序）。"""
    return _MSG_ID_RE.findall(_strip_paren_comments(value))


def _collect_message(raw: str, index: int) -> dict:
    """解析一封邮件的原始头部，收集会话关系所需的字段。"""
    blocks = parse_header_blocks(raw)
    message_ids: list[str] = []
    in_reply_to_ids: list[str] = []
    references_ids: list[str] = []
    subject: str | None = None
    for block in blocks:
        name = block.name.lower()
        if name == "message-id":
            message_ids.extend(_extract_msg_ids(block.value))
        elif name == "in-reply-to":
            in_reply_to_ids.extend(_extract_msg_ids(block.value))
        elif name == "references":
            references_ids.extend(_extract_msg_ids(block.value))
        elif name == "subject" and subject is None:
            subject = block.value or None
    # 同一封内重复声明同一 ID 不算“重复 Message-ID”（跨封重复才算），去重保序
    unique_ids = list(dict.fromkeys(message_ids))
    return {
        "index": index,
        "message_ids": unique_ids,
        "message_id": unique_ids[0] if unique_ids else None,
        "subject": subject,
        "in_reply_to_ids": in_reply_to_ids,
        "references_ids": references_ids,
    }


def _basis_desc(relation: dict) -> str:
    if relation["basis"] == "in_reply_to":
        return "In-Reply-To 唯一消息 ID"
    return "缺少 In-Reply-To 字段，取 References 末项"


def _relation_detail(msg: dict, relation: dict) -> str:
    """生成逐封关联依据的中文说明。"""
    parent_id = relation["parent_id"]
    status = relation["status"]
    reason = relation["ambiguous_reason"]

    if status == STATUS_NONE:
        return "未提取到 In-Reply-To / References 尖括号消息 ID，无父邮件线索"
    if reason == REASON_IN_REPLY_TO_MULTIPLE:
        count = len(msg["in_reply_to_ids"])
        return (
            f"In-Reply-To 含 {count} 个消息 ID，无法唯一确定父邮件，"
            "标为歧义，不强行连边"
        )

    basis = _basis_desc(relation)
    if status == STATUS_LINKED:
        parent_index = relation["parent_index"]
        return (
            f"{basis} {parent_id} 与第 {parent_index} 封的 Message-ID "
            f"匹配，以第 {parent_index} 封为父"
        )
    if status == STATUS_EXTERNAL:
        return f"{basis} {parent_id} 不在本批次内，记为外部引用（不连边）"
    if reason == REASON_DUPLICATE_MESSAGE_ID:
        holders = "、".join(str(i) for i in relation["duplicate_holders"])
        return (
            f"{basis} {parent_id} 被批次内第 {holders} 封同时声明"
            "（重复 Message-ID），无法确定唯一父邮件，标为歧义，不强行连边"
        )
    # REASON_CYCLE
    return f"{basis} {parent_id} 的关联形成环状引用，标为歧义，不强行连边"


def build_thread_relations(headers_list: list[str]) -> dict:
    """对多封邮件原始头部还原会话关系。

    参数为头部字符串列表（页面粘贴文本可先用
    :func:`mailtrace.parsing.batch.split_batch_headers` 切分）。
    返回会话树、孤立邮件与逐封关联依据，供页面 / API 使用。
    """
    messages = [
        _collect_message(raw, i) for i, raw in enumerate(headers_list, start=1)
    ]
    by_index = {m["index"]: m for m in messages}

    # Message-ID -> 声明它的邮件序号列表（同一 ID 被多封声明即重复 Message-ID）
    id_holders: dict[str, list[int]] = {}
    for msg in messages:
        for mid in msg["message_ids"]:
            id_holders.setdefault(mid, []).append(msg["index"])

    # 1) 逐封判定候选父 ID 与依据（In-Reply-To 唯一 ID 优先，
    #    否则取 References 末项；In-Reply-To 多 ID 直接标歧义）
    for msg in messages:
        irt = msg["in_reply_to_ids"]
        refs = msg["references_ids"]
        relation = {
            "basis": "none",
            "parent_id": None,
            "status": STATUS_NONE,
            "ambiguous_reason": None,
            "parent_index": None,
        }
        if irt:
            relation["basis"] = "in_reply_to"
            if len(irt) == 1:
                relation["parent_id"] = irt[0]
            else:
                relation["status"] = STATUS_AMBIGUOUS
                relation["ambiguous_reason"] = REASON_IN_REPLY_TO_MULTIPLE
        elif refs:
            relation["basis"] = "references_last"
            relation["parent_id"] = refs[-1]
        msg["relation"] = relation

    # 2) 把候选父 ID 解析到批次内邮件 / 外部引用
    for msg in messages:
        relation = msg["relation"]
        parent_id = relation["parent_id"]
        if parent_id is None or relation["status"] == STATUS_AMBIGUOUS:
            continue
        holders = id_holders.get(parent_id, [])
        if len(holders) > 1:
            relation["status"] = STATUS_AMBIGUOUS
            relation["ambiguous_reason"] = REASON_DUPLICATE_MESSAGE_ID
            relation["duplicate_holders"] = holders
        elif len(holders) == 1:
            relation["status"] = STATUS_LINKED
            relation["parent_index"] = holders[0]
        else:
            relation["status"] = STATUS_EXTERNAL

    # 3) 环检测：每封邮件至多一个父指针（函数图），沿父链走，
    #    回到本路径中的节点即环；环上邮件的父关联标为歧义并断开（含自引用）
    parent_of: dict[int, int] = {
        m["index"]: m["relation"]["parent_index"]
        for m in messages
        if m["relation"]["parent_index"] is not None
    }
    confirmed: set[int] = set()
    for start in list(parent_of):
        if start in confirmed:
            continue
        path: list[int] = []
        node: int | None = start
        while node is not None and node not in confirmed:
            if node in path:
                for idx in path[path.index(node):]:
                    relation = by_index[idx]["relation"]
                    relation["status"] = STATUS_AMBIGUOUS
                    relation["ambiguous_reason"] = REASON_CYCLE
                    relation["parent_index"] = None
                break
            path.append(node)
            node = parent_of.get(node)
        confirmed.update(path)

    # 4) 子节点列表（按邮件序号排序）与逐封关联依据文案
    children_of: dict[int, list[int]] = {}
    for msg in messages:
        parent_index = msg["relation"]["parent_index"]
        if parent_index is not None:
            children_of.setdefault(parent_index, []).append(msg["index"])
    for msg in messages:
        msg["children"] = children_of.get(msg["index"], [])
        relation = msg["relation"]
        relation["basis_label"] = _BASIS_LABELS[relation["basis"]]
        relation["status_label"] = _STATUS_LABELS[relation["status"]]
        reason = relation["ambiguous_reason"]
        relation["ambiguous_label"] = _REASON_LABELS.get(reason) if reason else None
        relation["detail"] = _relation_detail(msg, relation)
        relation.pop("duplicate_holders", None)

    # 5) 组装会话树：根 = 无批次内父、但有子节点或外部父引用的邮件
    def build_node(index: int) -> dict:
        msg = by_index[index]
        return {
            "index": index,
            "message_id": msg["message_id"],
            "subject": msg["subject"],
            "children": [build_node(c) for c in msg["children"]],
        }

    def tree_size(node: dict) -> int:
        return 1 + sum(tree_size(child) for child in node["children"])

    threads: list[dict] = []
    for msg in messages:
        relation = msg["relation"]
        if relation["parent_index"] is not None:
            continue  # 非根：挂在其他节点下
        is_external_root = relation["status"] == STATUS_EXTERNAL
        if not msg["children"] and not is_external_root:
            continue  # 孤立邮件，单独列出
        node = build_node(msg["index"])
        threads.append(
            {
                "root_index": msg["index"],
                "external_parent_id": (
                    relation["parent_id"] if is_external_root else None
                ),
                "size": tree_size(node),
                "tree": node,
            }
        )
    # 大树在前，同规模按根邮件序号保证顺序稳定
    threads.sort(key=lambda t: (-t["size"], t["root_index"]))

    orphans = [
        msg["index"]
        for msg in messages
        if msg["relation"]["parent_index"] is None
        and not msg["children"]
        and msg["relation"]["status"] != STATUS_EXTERNAL
    ]

    return {
        "message_count": len(messages),
        "thread_count": len(threads),
        "linked_count": sum(
            1 for m in messages if m["relation"]["status"] == STATUS_LINKED
        ),
        "external_ref_count": sum(
            1 for m in messages if m["relation"]["status"] == STATUS_EXTERNAL
        ),
        "ambiguous_count": sum(
            1 for m in messages if m["relation"]["status"] == STATUS_AMBIGUOUS
        ),
        "orphan_count": len(orphans),
        "threads": threads,
        "orphans": orphans,
        "messages": messages,
    }
